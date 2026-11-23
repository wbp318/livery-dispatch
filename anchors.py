"""Text-label → coordinates resolver for the public /quote endpoint.

The marketing-site reserve form sends free-text pickup / dropoff labels —
"JAX Terminal", "The Cloister, Sea Island", "Jekyll Island Club". The /quote
endpoint needs lat/lon to feed the fare calculator, but Year-1 has no
geocoder. This module hand-curates the corridor anchors so the common cases (JAX/SAV → SI/JKL/
RIT/Cumberland/etc.) resolve cleanly without an external service.

Anything outside the canonical set returns `None`, and `/quote` soft-fails
with a "dispatcher will confirm by phone" message rather than guessing a
fare for an address it can't place. That matches Year-1 reality: the
dispatcher is in the loop on every booking.

When a real geocoder gets wired in (or the
anchor list grows substantially), swap the resolver's internals — the
contract (text → (canonical, lat, lon) | None) stays the same.

Coordinates are planning-grade. They drive a haversine/OSRM distance for a
fare *estimate*, not turn-by-turn navigation. Off-by-100m precision is fine.
"""

from __future__ import annotations

import re


# Canonical anchor coordinates. Keys are canonical short codes. Values are (latitude, longitude).
# 2026-11-17: SI, SSI_D, RIT, OMI and CUM moved to their OpenStreetMap positions
# (gis/data/places.csv measured them 1-2 km off). Interim: the QGIS analyst
# guide's Job 2 still wants the gate / porte-cochere points captured by eye.
# JKL is unchanged on purpose: one point serves the Club and the Westin.
ANCHORS: dict[str, tuple[float, float]] = {
    # Airports
    "JAX": (30.4941, -81.6878),      # Jacksonville International
    "SAV": (32.1276, -81.2025),      # Savannah / Hilton Head International
    "BQK": (31.2589, -81.4664),      # Brunswick Golden Isles
    "SMI": (31.1518, -81.3914),      # McKinnon St. Simons (private aviation)
    # Resorts and destination zones
    "SI": (31.1807, -81.3506),       # Sea Island: The Cloister (OSM; was 31.1969,-81.3438, 1.9 km up-island)
    "JKL": (31.0577, -81.4068),      # Jekyll Island Club + Westin Jekyll
    "SSI_D": (31.1338, -81.3967),    # St. Simons village, Pier Village (OSM; was on the McKinnon airport, 2.1 km off)
    "RIT": (30.5942, -81.4447),      # Ritz-Carlton Amelia Island (OSM; was 1.0 km off)
    "OMI": (30.5548, -81.4434),      # Omni Amelia Island Resort (OSM; was 2.0 km off)
    "AMELIA": (30.6697, -81.4626),   # Amelia Island (general)
    "CUM": (30.7204, -81.5511),      # Cumberland Island ferry, St. Marys waterfront beside the dock (OSM; was 1.2 km off)
    "OAKS": (31.3712, -81.4338),     # Oaks on the River, Darien
    "TYB": (32.0085, -80.8458),      # Tybee Island
    # Town centers
    "BWK": (31.1499, -81.4915),      # Brunswick
    "DAR": (31.3690, -81.4346),      # Darien
    "DTSAV": (32.0809, -81.0912),    # Downtown Savannah
    "DTJAX": (30.3322, -81.6557),    # Downtown Jacksonville
}


# Alias map. Keys are pre-normalized (lowercase, single-spaced). Multiple
# aliases per anchor are expected — every distinctive way a guest might
# write the place name. Longer aliases must be matched first (handled by
# the resolver), so e.g. "sea island lodge" preferentially resolves to SI
# instead of bare "sea island".
_ALIASES: dict[str, str] = {
    # JAX
    "jax": "JAX",
    "jax airport": "JAX",
    "jax intl": "JAX",
    "jax international": "JAX",
    "jacksonville airport": "JAX",
    "jacksonville international": "JAX",
    "jia": "JAX",
    "kjax": "JAX",
    # SAV
    "sav": "SAV",
    "sav airport": "SAV",
    "savannah airport": "SAV",
    "savannah hilton head airport": "SAV",
    "savannah/hilton head airport": "SAV",
    "ksav": "SAV",
    # BQK
    "bqk": "BQK",
    "brunswick airport": "BQK",
    "brunswick golden isles airport": "BQK",
    "golden isles airport": "BQK",
    # SMI
    "smi": "SMI",
    "mckinnon st simons airport": "SMI",
    "mckinnon st. simons airport": "SMI",
    "st simons airport": "SMI",
    "st. simons airport": "SMI",
    # SI — Sea Island
    "sea island": "SI",
    "the cloister": "SI",
    "cloister": "SI",
    "cloister at sea island": "SI",
    "the cloister at sea island": "SI",
    "lodge at sea island": "SI",
    "the lodge": "SI",
    "the lodge at sea island": "SI",
    "sea island lodge": "SI",
    "sea island co": "SI",
    "sea island company": "SI",
    "sea island resort": "SI",
    # JKL — Jekyll
    "jekyll": "JKL",
    "jekyll island": "JKL",
    "jekyll island club": "JKL",
    "jekyll island club resort": "JKL",
    "jekyll club": "JKL",
    "westin jekyll": "JKL",
    "westin jekyll island": "JKL",
    # SSI_D — St. Simons downtown
    "ssi": "SSI_D",
    "st simons": "SSI_D",
    "st. simons": "SSI_D",
    "saint simons": "SSI_D",
    "st simons island": "SSI_D",
    "st. simons island": "SSI_D",
    "saint simons island": "SSI_D",
    "king and prince": "SSI_D",
    "the king and prince": "SSI_D",
    # RIT — Ritz Amelia
    "ritz amelia": "RIT",
    "ritz carlton amelia": "RIT",
    "ritz-carlton amelia": "RIT",
    "ritz amelia island": "RIT",
    "ritz carlton amelia island": "RIT",
    "ritz-carlton amelia island": "RIT",
    # OMI — Omni Amelia
    "omni amelia": "OMI",
    "omni amelia island": "OMI",
    "omni amelia island resort": "OMI",
    # AMELIA — Amelia Island (general)
    "amelia": "AMELIA",
    "amelia island": "AMELIA",
    "fernandina": "AMELIA",
    "fernandina beach": "AMELIA",
    # CUM — Cumberland Island / St. Marys
    "cumberland": "CUM",
    "cumberland island": "CUM",
    "greyfield": "CUM",
    "greyfield inn": "CUM",
    "st marys": "CUM",
    "st. marys": "CUM",
    "saint marys": "CUM",
    "cumberland ferry": "CUM",
    # OAKS — Oaks on the River
    "oaks": "OAKS",
    "the oaks": "OAKS",
    "oaks on the river": "OAKS",
    "oaks on the river resort": "OAKS",
    # TYB
    "tybee": "TYB",
    "tybee island": "TYB",
    # Town centers
    "brunswick": "BWK",
    "darien": "DAR",
    "downtown savannah": "DTSAV",
    "savannah": "DTSAV",
    "city market savannah": "DTSAV",
    "downtown jacksonville": "DTJAX",
    "downtown jax": "DTJAX",
}


# Pre-sorted alias list: longest first, so substring matching prefers the
# most specific phrase. Avoids "sea island" stealing the match away from
# "the lodge at sea island" when both are in the input.
_ALIASES_BY_LEN: list[tuple[str, str]] = sorted(
    _ALIASES.items(), key=lambda kv: -len(kv[0])
)


# Pre-compiled word-boundary patterns, longest alias first. The \b guards
# against short codes (jax/sav/smi/jia/oaks/...) matching inside unrelated
# words ("tran[smi]ssion", "[sav]ory", "fu[jia]n", "[oaks]hire") — which
# would defeat the None soft-fail contract and emit a confident, wrong-corridor
# fare estimate for an address that isn't actually in the corridor.
_ALIAS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b" + re.escape(alias) + r"\b"), canonical)
    for alias, canonical in _ALIASES_BY_LEN
]


def _normalize(s: str) -> str:
    """Lowercase, strip, collapse whitespace, drop trailing punctuation."""
    if not s:
        return ""
    out = s.strip().lower()
    # Replace common separators with single spaces; collapse whitespace.
    for ch in (",", ";", "/", "\\"):
        out = out.replace(ch, " ")
    out = " ".join(out.split())
    return out


def resolve_anchor(text: str) -> tuple[str, float, float] | None:
    """Resolve a free-text label to (canonical_key, lat, lon).

    Match precedence:
      1. Exact alias hit on the normalized input.
      2. Substring match: scan aliases longest-first; return the first whose
         alias appears as a substring of the normalized input.
      3. None.

    Returns None when nothing matches — caller decides the soft-fail UX.
    """
    norm = _normalize(text)
    if not norm:
        return None

    # 1. Exact alias
    canonical = _ALIASES.get(norm)
    if canonical is not None:
        lat, lon = ANCHORS[canonical]
        return canonical, lat, lon

    # 2. Substring (longest alias first, word-boundary aware)
    for pattern, canonical in _ALIAS_PATTERNS:
        if pattern.search(norm):
            lat, lon = ANCHORS[canonical]
            return canonical, lat, lon

    return None


# Convenience: airports trigger the airport-meet surcharge.
AIRPORT_ANCHORS: frozenset[str] = frozenset({"JAX", "SAV", "BQK", "SMI"})


def is_airport(canonical: str | None) -> bool:
    return canonical in AIRPORT_ANCHORS
