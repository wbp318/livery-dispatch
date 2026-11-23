"""GET /osrm/route/v1/driving/{coords} — same-origin OSRM proxy (v1.1.15).

Contract: one OSRM_BASE_URL drives both the browser maps (via this proxy)
and fare distance (fare.py). Nothing user-supplied reaches the upstream URL
verbatim; waypoints are corridor-bounded; responses are cached; every
failure maps to a non-2xx so the maps keep their straight-line fallback.
No network: httpx.get is monkeypatched throughout.
"""

from __future__ import annotations

import json

import httpx
import pytest

import fare
import osrm_proxy

JAX = "-81.6879,30.4941"
CLOISTER = "-81.3438,31.1969"
ROUTE = f"/osrm/route/v1/driving/{JAX};{CLOISTER}?overview=full&geometries=geojson"

_OK_BODY = {
    "code": "Ok",
    "routes": [{"distance": 112000.0, "duration": 5400.0,
                "geometry": {"type": "LineString", "coordinates": [[-81.6879, 30.4941], [-81.3438, 31.1969]]}}],
    "waypoints": [],
}


class _FakeUpstream:
    """Records calls; returns a canned response or raises."""

    def __init__(self, status=200, body=None, raw=None, exc=None):
        self.status, self.body, self.raw, self.exc = status, body, raw, exc
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        content = self.raw if self.raw is not None else json.dumps(
            _OK_BODY if self.body is None else self.body).encode()
        return httpx.Response(self.status, content=content, request=httpx.Request("GET", url))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    osrm_proxy.cache.clear()
    monkeypatch.setattr(fare, "_OSRM_BASE", "http://osrm.internal:5000")
    yield
    osrm_proxy.cache.clear()


@pytest.fixture
def upstream(monkeypatch):
    fake = _FakeUpstream()
    monkeypatch.setattr(osrm_proxy.httpx, "get", fake)
    return fake


# --- happy path ------------------------------------------------------------


def test_proxies_route_from_osrm_base_url(client, upstream):
    r = client.get(ROUTE)
    assert r.status_code == 200, r.text
    assert r.json()["routes"][0]["geometry"]["type"] == "LineString"
    url, kwargs = upstream.calls[0]
    assert url == ("http://osrm.internal:5000/route/v1/driving/"
                   "-81.687900,30.494100;-81.343800,31.196900?geometries=geojson&overview=full")
    assert kwargs["follow_redirects"] is False
    assert kwargs["timeout"] == fare._OSRM_TIMEOUT_S
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"].startswith("public, max-age=")
    assert r.headers["x-cache"] == "MISS"


def test_same_engine_as_fare_backend(client, upstream, monkeypatch):
    """The whole point: the proxy follows the same OSRM_BASE fare.py uses."""
    monkeypatch.setattr(fare, "_OSRM_BASE", "http://self-hosted.example:5000")
    client.get(ROUTE)
    assert upstream.calls[0][0].startswith("http://self-hosted.example:5000/route/v1/driving/")


def test_second_identical_request_is_a_cache_hit(client, upstream):
    assert client.get(ROUTE).headers["x-cache"] == "MISS"
    r = client.get(ROUTE)
    assert r.status_code == 200
    assert r.headers["x-cache"] == "HIT"
    assert len(upstream.calls) == 1


def test_cache_key_is_canonical(client, upstream):
    """Param order and trailing zeros don't defeat the cache."""
    client.get(f"/osrm/route/v1/driving/{JAX};{CLOISTER}?overview=full&geometries=geojson")
    client.get("/osrm/route/v1/driving/-81.68790,30.49410;-81.3438,31.1969?geometries=geojson&overview=full")
    assert len(upstream.calls) == 1


def test_cache_disabled_with_zero_ttl(client, upstream, monkeypatch):
    monkeypatch.setenv("OSRM_PROXY_CACHE_TTL_S", "0")
    client.get(ROUTE)
    client.get(ROUTE)
    assert len(upstream.calls) == 2


def test_cache_evicts_least_recently_used(upstream, monkeypatch):
    monkeypatch.setenv("OSRM_PROXY_CACHE_MAX", "2")
    c = osrm_proxy.cache
    c.put("a", b"1"); c.put("b", b"2")
    assert c.get("a") == b"1"          # touch a → b is now LRU
    c.put("c", b"3")
    assert c.get("b") is None and c.get("a") == b"1" and c.get("c") == b"3"
    assert len(c) == 2


def test_unrounded_js_doubles_accepted(client, upstream):
    """A Leaflet map-click latlng stringifies with ~15 decimals."""
    r = client.get("/osrm/route/v1/driving/-81.68790000000001,30.494100000000003;-81.3438,31.1969")
    assert r.status_code == 200, r.text


def test_eight_stop_corridor_route_accepted(client, upstream):
    stops = ["-81.2021,32.1276", "-81.4380,31.3650", "-81.4915,31.1499", "-81.3500,31.1986",
             "-81.3884,31.1502", "-81.4187,31.0608", "-81.4622,30.6708", "-81.6879,30.4941"]
    r = client.get("/osrm/route/v1/driving/" + ";".join(stops) + "?overview=full&geometries=geojson")
    assert r.status_code == 200, r.text


# --- input validation (nothing reaches upstream) ---------------------------


@pytest.mark.parametrize("coords,detail", [
    (JAX, "Expected 2-10 waypoints"),
    (";".join([JAX] * 11), "Expected 2-10 waypoints"),
    (f"{JAX};abc,def", "Malformed coordinate"),
    (f"{JAX};-81.3438", "Malformed coordinate"),
    (f"{JAX};-81.3438,31.1969,0", "Malformed coordinate"),
    (f"{JAX};1e2,31.1969", "Malformed coordinate"),
    (f"{JAX};-181.0,31.0", "Coordinate out of range"),
    (f"{JAX};-81.0,91.5", "Coordinate out of range"),
    (f"{JAX};-84.3880,33.7490", "outside the service corridor"),      # Atlanta
    (f"{JAX};-0.1276,51.5072", "outside the service corridor"),       # London
    (f"{JAX};-80.1918,25.7617", "outside the service corridor"),      # Miami
])
def test_bad_coordinates_rejected_without_upstream_call(client, upstream, coords, detail):
    r = client.get(f"/osrm/route/v1/driving/{coords}")
    assert r.status_code == 400, r.text
    assert detail in r.json()["detail"]
    assert r.headers["cache-control"] == "no-store"
    assert upstream.calls == []


@pytest.mark.parametrize("query,detail", [
    ("overview=full&annotations=true", "Unsupported parameter"),
    ("hints=abc", "Unsupported parameter"),
    ("overview=everything", "Invalid value for overview"),
    ("geometries=geojson&geometries=polyline", "Duplicate parameter"),
    ("steps=1", "Invalid value for steps"),
])
def test_bad_params_rejected_without_upstream_call(client, upstream, query, detail):
    r = client.get(f"/osrm/route/v1/driving/{JAX};{CLOISTER}?{query}")
    assert r.status_code == 400, r.text
    assert detail in r.json()["detail"]
    assert upstream.calls == []


@pytest.mark.parametrize("path", [
    "/osrm/route/v1/driving/..%2F..%2Fadmin",
    f"/osrm/route/v1/driving/{JAX};{CLOISTER}%2F..%2F..%2Ftable",
    "/osrm/route/v1/walking/-81.6879,30.4941;-81.3438,31.1969",
    "/osrm/table/v1/driving/-81.6879,30.4941;-81.3438,31.1969",
    "/osrm/route/v1/driving/@evil.example/-81.6879,30.4941",
])
def test_path_tricks_never_reach_upstream(client, upstream, path):
    r = client.get(path)
    assert r.status_code in (400, 404), (path, r.status_code)
    assert upstream.calls == []


def test_upstream_url_is_rebuilt_not_forwarded(client, upstream):
    """Even an accepted request's upstream URL is reconstructed from parsed
    floats — the raw path string is never concatenated onto the base."""
    client.get("/osrm/route/v1/driving/-81.6879000,30.4941;-81.3438,31.19690?overview=false")
    url = upstream.calls[0][0]
    assert url.endswith("/route/v1/driving/-81.687900,30.494100;-81.343800,31.196900?overview=false")


# --- upstream failure mapping ----------------------------------------------


def test_upstream_timeout_is_504(client, monkeypatch):
    monkeypatch.setattr(osrm_proxy.httpx, "get", _FakeUpstream(exc=httpx.ReadTimeout("slow")))
    r = client.get(ROUTE)
    assert r.status_code == 504
    assert r.headers["cache-control"] == "no-store"


def test_upstream_connection_error_is_502(client, monkeypatch):
    monkeypatch.setattr(osrm_proxy.httpx, "get", _FakeUpstream(exc=httpx.ConnectError("refused")))
    r = client.get(ROUTE)
    assert r.status_code == 502
    assert "osrm.internal" not in r.text  # no upstream internals leaked


@pytest.mark.parametrize("status", [429, 500, 503])
def test_upstream_http_error_is_502_and_not_cached(client, monkeypatch, status):
    fake = _FakeUpstream(status=status, body={"message": "Too Many Requests"})
    monkeypatch.setattr(osrm_proxy.httpx, "get", fake)
    assert client.get(ROUTE).status_code == 502
    assert client.get(ROUTE).status_code == 502
    assert len(fake.calls) == 2


def test_osrm_no_route_passes_through_code_only(client, monkeypatch):
    fake = _FakeUpstream(status=400, body={"code": "NoRoute", "message": "Impossible route between points"})
    monkeypatch.setattr(osrm_proxy.httpx, "get", fake)
    r = client.get(ROUTE)
    assert r.status_code == 400
    assert r.json() == {"code": "NoRoute"}


@pytest.mark.parametrize("raw", [b"not json", json.dumps({"code": "InvalidUrl"}).encode(), b"[]"])
def test_upstream_bad_payload_is_502(client, monkeypatch, raw):
    monkeypatch.setattr(osrm_proxy.httpx, "get", _FakeUpstream(raw=raw))
    assert client.get(ROUTE).status_code == 502


def test_oversized_upstream_response_is_502(client, monkeypatch):
    big = json.dumps({"code": "Ok", "routes": [], "pad": "x" * osrm_proxy.MAX_UPSTREAM_BYTES}).encode()
    monkeypatch.setattr(osrm_proxy.httpx, "get", _FakeUpstream(raw=big))
    assert client.get(ROUTE).status_code == 502


def test_routing_disabled_is_503(client, upstream, monkeypatch):
    monkeypatch.setattr(fare, "_OSRM_BASE", "")
    r = client.get(ROUTE)
    assert r.status_code == 503
    assert upstream.calls == []


# --- surface posture -------------------------------------------------------


def test_public_without_admin_session(client, upstream):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    assert client.get(ROUTE).status_code == 200


def test_rate_limited_per_ip(client, upstream, monkeypatch):
    import app as app_module
    monkeypatch.setenv("OSRM_PROXY_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("OSRM_PROXY_RATE_MAX", "3")
    monkeypatch.setattr(app_module, "_osrm_rate_history", {})
    codes = [client.get(ROUTE).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert client.get(ROUTE).headers["retry-after"] == "60"


def test_osrm_bucket_is_separate_from_public_form_bucket(client, upstream, monkeypatch):
    """Map route calls must not eat the visitor's booking-form budget."""
    import app as app_module
    monkeypatch.setenv("OSRM_PROXY_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_FORM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_FORM_RATE_MAX", "5")
    monkeypatch.setattr(app_module, "_osrm_rate_history", {})
    monkeypatch.setattr(app_module, "_rate_history", {})
    for _ in range(10):
        client.get(ROUTE)
    r = client.post("/reservations", json={
        "name": "After Map", "email": "m@example.com", "phone": "+19045550190",
        "pickup_label": "JAX", "dropoff_label": "Cloister",
    })
    assert r.status_code == 201, r.text


def test_not_in_openapi_schema(client):
    assert "/osrm/route/v1/driving/{coords}" not in client.get("/openapi.json").json()["paths"]


def test_csp_no_longer_allows_public_osrm_from_the_browser(client):
    csp = client.get("/map").headers["content-security-policy"]
    assert "router.project-osrm.org" not in csp
    assert "connect-src 'self'" in csp


def test_map_page_fetches_routes_through_the_proxy(client):
    html = client.get("/map").text
    assert "'/osrm/route/v1/driving/'" in html
    assert "https://router.project-osrm.org/route" not in html


def test_production_warns_when_still_on_public_demo(monkeypatch, caplog):
    import app as app_module
    monkeypatch.setenv("DISPATCH_ENV", "production")
    monkeypatch.setattr(fare, "_OSRM_BASE", "https://router.project-osrm.org")
    with caplog.at_level("WARNING", logger="dispatch"):
        app_module._warn_on_public_osrm_in_production()
    assert "public OSRM demo" in caplog.text
    caplog.clear()
    monkeypatch.setattr(fare, "_OSRM_BASE", "http://livery-osrm.flycast")
    with caplog.at_level("WARNING", logger="dispatch"):
        app_module._warn_on_public_osrm_in_production()
    assert caplog.text == ""
