"""The dispatcher map's background (v1.3.0).

CARTO's free tiles started answering "API KEY REQUIRED", so /map now draws
either our own map server (QGIS Server, gis/server/, via MAP_SERVER_URL) or,
until that's deployed, OpenStreetMap's standard tiles darkened in the browser.
MAP_SERVER_URL lands in the page (#map data-qgis-url) and in the CSP img-src,
so only a clean https origin may get through.
"""

from __future__ import annotations

import logging
import re

import pytest

import map_view


def _csp(client) -> str:
    return client.get("/health").headers["Content-Security-Policy"]


def _img_src(csp: str) -> str:
    return next(d for d in csp.split(";") if d.strip().startswith("img-src")).strip()


def _qgis_attr(html: str) -> str:
    m = re.search(r'<div id="map" data-qgis-url="([^"]*)"', html)
    assert m, "#map has no data-qgis-url"
    return m.group(1)


def test_default_map_uses_openstreetmap_not_carto(client, monkeypatch):
    monkeypatch.delenv("MAP_SERVER_URL", raising=False)
    html = client.get("/map").text
    assert _qgis_attr(html) == ""
    assert "tile.openstreetmap.org" in html
    assert "cartocdn" not in html
    csp = _csp(client)
    assert "https://tile.openstreetmap.org" in _img_src(csp)
    assert "cartocdn" not in csp


def test_map_server_url_switches_the_basemap_and_the_csp(client, monkeypatch):
    monkeypatch.setenv("MAP_SERVER_URL", "https://maps.example.test/")
    html = client.get("/map").text
    assert _qgis_attr(html) == "https://maps.example.test"
    assert "/ogc/coastal_basemap" in html          # the WMS path the page builds
    img = _img_src(_csp(client))
    assert "https://maps.example.test" in img.split()
    assert "https://tile.openstreetmap.org" in img  # still allowed: harmless, and keeps the fallback working


@pytest.mark.parametrize("raw, expected", [
    ("https://maps.example.test", "https://maps.example.test"),
    ("https://maps.example.test:8443/ogc/x?y=1#z", "https://maps.example.test:8443"),
    ("  https://livery-maps.fly.dev/  ", "https://livery-maps.fly.dev"),
    ("http://localhost:8080", "http://localhost:8080"),
    ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
    ("http://maps.example.test", ""),                 # plain http off localhost
    ("javascript:alert(1)", ""),
    ("ftp://maps.example.test", ""),
    ('https://maps.example.test"><script>alert(1)</script>', ""),
    ("https://maps.example.test; img-src *", ""),
    ("https://user:pw@maps.example.test", ""),
    ("https://", ""),
    ("", ""),
])
def test_map_server_url_accepts_only_a_clean_https_origin(monkeypatch, raw, expected):
    monkeypatch.setenv("MAP_SERVER_URL", raw)
    assert map_view.map_server_url() == expected


def test_hostile_map_server_url_reaches_neither_page_nor_csp(client, monkeypatch):
    monkeypatch.setenv("MAP_SERVER_URL", 'https://x.test"><script>alert(1)</script>')
    html = client.get("/map").text
    assert _qgis_attr(html) == ""
    assert "<script>alert(1)</script>" not in html
    assert "x.test" not in _csp(client)


def test_production_warns_without_a_map_server(monkeypatch, caplog):
    import app as app_module
    monkeypatch.setenv("DISPATCH_ENV", "production")
    monkeypatch.delenv("MAP_SERVER_URL", raising=False)
    with caplog.at_level(logging.WARNING, logger="dispatch"):
        app_module._warn_on_public_tiles_in_production()
    assert any("MAP_SERVER_URL is not set" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setenv("MAP_SERVER_URL", "https://maps.example.test")
    with caplog.at_level(logging.WARNING, logger="dispatch"):
        app_module._warn_on_public_tiles_in_production()
    assert not caplog.records
