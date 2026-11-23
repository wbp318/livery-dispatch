"""Same-origin OSRM route proxy — ``GET /osrm/route/v1/driving/{coords}`` (v1.1.15).

Why this exists:
the three browser maps (dispatcher ``/map``, ``site/js/map.js``,
``site/js/map-resort.js``) hardcoded the public OSRM demo
(``router.project-osrm.org``), which is non-commercial-only, ~1 req/s, no
SLA. When it rate-limits, the maps silently keep their straight-line
fallback. The fare backend already honoured ``OSRM_BASE_URL``; the maps did
not. Routing every browser route request through this proxy means **one env
var (``OSRM_BASE_URL``) drives both the map geometry and fare distance**, and
pointing it at the self-hosted container (``osrm/``) retires the
public demo everywhere at once.

Design:

- **Nothing user-supplied is forwarded verbatim.** Coordinates are parsed to
  floats, range- and corridor-checked, and re-serialised; query params are an
  allow-list of OSRM's own enums. The upstream URL is rebuilt from scratch,
  so path traversal / host injection / param smuggling have nothing to ride.
- **Corridor-bounded.** Waypoints must fall inside ``CORRIDOR_BBOX`` — the same
  box ``osrm/Dockerfile`` clips the Geofabrik extract to. That keeps
  the public endpoint from being a free general-purpose routing relay and
  matches what the self-hosted graph can actually answer.
- **Cached.** Route geometry for fixed corridor endpoints is stable; BSD-2 +
  ODbL permit caching. A small in-process LRU (TTL ``OSRM_PROXY_CACHE_TTL_S``,
  default 24h; ``OSRM_PROXY_CACHE_MAX`` entries, default 512) absorbs the
  marketing-site maps' repeat requests, and successful responses carry
  ``Cache-Control: public, max-age=…`` for browsers / CDNs.
- **Fails soft.** Upstream timeout → 504, any other upstream failure → 502,
  routing disabled (``OSRM_BASE_URL=""``) → 503. Every client already treats a
  non-2xx as "keep the straight-line skeleton", so an outage degrades exactly
  as before — never worse. OSRM's own 400s (``NoRoute``, ``NoSegment``) pass
  through as 400 with only the ``code`` field, so clients can tell "no road
  route" from "router down" without seeing upstream internals.

Public + unauthenticated by design (the static marketing site calls it
cross-origin, governed by ``CORS_ORIGINS``); per-IP rate limited in
``app.py`` (``OSRM_PROXY_RATE_*``). GET-only, so the admin CSRF contract
doesn't apply.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx

import fare as _fare

# (min_lon, min_lat, max_lon, max_lat). Covers SAV + the Statesboro / GSU
# Tier-2 catchment in the north through JAX and St. Augustine in the south.
# Keep in sync with the BBOX build arg in osrm/Dockerfile.
CORRIDOR_BBOX: tuple[float, float, float, float] = (-82.8, 29.8, -80.6, 32.9)
# The site's corridor map walks 8 stops (SAV → … → JAX) in one request.
MAX_WAYPOINTS = 10
MAX_UPSTREAM_BYTES = 2_000_000

# OSRM route-service options the clients actually use, with OSRM's own enums.
_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "overview": frozenset({"full", "simplified", "false"}),
    "geometries": frozenset({"geojson", "polyline", "polyline6"}),
    "steps": frozenset({"true", "false"}),
    "alternatives": frozenset({"true", "false"}),
}

# lon,lat in plain decimal notation. Up to 17 fractional digits because an
# unrounded JS double (e.g. a Leaflet map-click latlng) stringifies with ~15;
# clients round to 6 anyway. Ranges are checked numerically after.
_COORD_RE = re.compile(r"-?\d{1,3}(?:\.\d{1,17})?,-?\d{1,2}(?:\.\d{1,17})?")


@dataclass
class ProxyResult:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class ProxyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail

    def to_result(self) -> ProxyResult:
        return ProxyResult(
            self.status,
            json.dumps({"detail": self.detail}).encode("utf-8"),
            {"Cache-Control": "no-store", "X-Cache": "BYPASS"},
        )


def parse_coords(coords: str) -> list[tuple[float, float]]:
    """``"lon,lat;lon,lat[;…]"`` → validated ``[(lon, lat), …]``."""
    parts = coords.split(";")
    if not 2 <= len(parts) <= MAX_WAYPOINTS:
        raise ProxyError(400, f"Expected 2-{MAX_WAYPOINTS} waypoints")
    min_lon, min_lat, max_lon, max_lat = CORRIDOR_BBOX
    out: list[tuple[float, float]] = []
    for part in parts:
        if not _COORD_RE.fullmatch(part):
            raise ProxyError(400, "Malformed coordinate; expected lon,lat")
        lon_s, lat_s = part.split(",")
        lon, lat = float(lon_s), float(lat_s)
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise ProxyError(400, "Coordinate out of range")
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            raise ProxyError(400, "Coordinate outside the service corridor")
        out.append((lon, lat))
    return out


def parse_params(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Allow-list OSRM options; reject unknown keys, bad values, duplicates."""
    seen: dict[str, str] = {}
    for key, value in items:
        allowed = _ALLOWED_PARAMS.get(key)
        if allowed is None:
            raise ProxyError(400, f"Unsupported parameter: {key[:40]}")
        if value not in allowed:
            raise ProxyError(400, f"Invalid value for {key}")
        if key in seen:
            raise ProxyError(400, f"Duplicate parameter: {key}")
        seen[key] = value
    return sorted(seen.items())


def build_upstream_url(base: str, coords: list[tuple[float, float]], params: list[tuple[str, str]]) -> str:
    path = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)
    query = "&".join(f"{k}={v}" for k, v in params)
    return f"{base}/route/v1/driving/{path}" + (f"?{query}" if query else "")


class _TTLCache:
    """Tiny thread-safe LRU with per-entry expiry. Sync endpoints run in the
    threadpool, so concurrent requests can touch this at once."""

    def __init__(self) -> None:
        self._data: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _ttl() -> float:
        try:
            return float(os.getenv("OSRM_PROXY_CACHE_TTL_S", "86400"))
        except ValueError:
            return 86400.0

    @staticmethod
    def _max() -> int:
        try:
            return int(os.getenv("OSRM_PROXY_CACHE_MAX", "512"))
        except ValueError:
            return 512

    def get(self, key: str) -> bytes | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            expires, body = hit
            if time.monotonic() >= expires:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return body

    def put(self, key: str, body: bytes) -> None:
        ttl, cap = self._ttl(), self._max()
        if ttl <= 0 or cap <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, body)
            self._data.move_to_end(key)
            while len(self._data) > cap:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


cache = _TTLCache()


def _fetch_upstream(url: str) -> ProxyResult:
    try:
        resp = httpx.get(url, timeout=_fare._OSRM_TIMEOUT_S, follow_redirects=False)
    except httpx.TimeoutException:
        raise ProxyError(504, "Routing upstream timed out")
    except httpx.HTTPError:
        raise ProxyError(502, "Routing upstream unavailable")

    if len(resp.content) > MAX_UPSTREAM_BYTES:
        raise ProxyError(502, "Routing upstream response too large")

    if resp.status_code == 400:
        # OSRM's own "can't route this" answer (NoRoute / NoSegment /
        # InvalidQuery). Pass the code through, nothing else.
        try:
            code = str(resp.json().get("code", "InvalidQuery"))[:40]
        except (ValueError, AttributeError):
            code = "InvalidQuery"
        return ProxyResult(400, json.dumps({"code": code}).encode("utf-8"),
                           {"Cache-Control": "no-store", "X-Cache": "MISS"})

    if resp.status_code != 200:
        raise ProxyError(502, "Routing upstream unavailable")

    try:
        data = resp.json()
    except ValueError:
        raise ProxyError(502, "Routing upstream returned invalid JSON")
    if not isinstance(data, dict) or data.get("code") != "Ok":
        raise ProxyError(502, "Routing upstream returned an error")
    return ProxyResult(200, resp.content, {"X-Cache": "MISS"})


def proxy_route(coords: str, query_items: list[tuple[str, str]]) -> ProxyResult:
    """Validate, serve from cache, or fetch from ``OSRM_BASE_URL``."""
    try:
        parsed = parse_coords(coords)
        params = parse_params(query_items)
        base = _fare._OSRM_BASE
        if not base:
            raise ProxyError(503, "Routing is not configured")
        url = build_upstream_url(base, parsed, params)

        cached = cache.get(url)
        if cached is not None:
            result = ProxyResult(200, cached, {"X-Cache": "HIT"})
        else:
            result = _fetch_upstream(url)
            if result.status == 200:
                cache.put(url, result.body)
    except ProxyError as exc:
        return exc.to_result()

    if result.status == 200:
        result.headers["Cache-Control"] = f"public, max-age={int(_TTLCache._ttl())}"
    return result


def using_public_demo() -> bool:
    return "router.project-osrm.org" in (_fare._OSRM_BASE or "")
