"""Per-request CSP nonce (v1.0.13).

The `_security_headers` middleware generates a fresh nonce on every
request, stores it in a contextvar, and injects it into both the CSP
header (`'nonce-{nonce}'`) and every inline `<script>` / `<style>` tag the
admin / driver / map / dashboard templates render. Dropping `'unsafe-inline'`
from the CSP closes the broadest XSS-amplification vector — a successful
HTML injection can no longer execute inline `<script>` or apply inline
`<style>` because the browser refuses anything that doesn't carry the
nonce, and the attacker can't guess a 128-bit random per request.

Inline-tag helpers (`nonce_attr`) deliberately fail open to empty-string
when no nonce is set (e.g. during direct render calls in unit tests that
bypass the middleware). The resulting tag will then be blocked by the CSP
nonce check — which is the safe default.
"""

from __future__ import annotations

import contextvars
import secrets


_nonce_var: contextvars.ContextVar[str] = contextvars.ContextVar("_csp_nonce", default="")


def generate_nonce() -> str:
    """128 bits of randomness, URL-safe base64 (~22 chars). One per request."""
    return secrets.token_urlsafe(16)


def set_nonce(nonce: str) -> None:
    _nonce_var.set(nonce)


def get_nonce() -> str:
    return _nonce_var.get()


def nonce_attr() -> str:
    """Return ` nonce="xxx"` ready to slot into a `<script>` or `<style>`
    opening tag — with a leading space so it can be concatenated directly:
    `f"<script{nonce_attr()}>"`. Empty string when no nonce is set."""
    n = _nonce_var.get()
    return f' nonce="{n}"' if n else ''


# Placeholder marker for module-load-time template strings (e.g. map_view's
# `_SCRIPTS`, admin_pages's `_TRIP_DETAIL_JS`) that can't evaluate the
# contextvar at build time. Call `inject_nonce(html)` at render time to
# substitute the per-request nonce attribute.
NONCE_PLACEHOLDER = "__CSP_NONCE_ATTR__"


def inject_nonce(html: str) -> str:
    """Replace `__CSP_NONCE_ATTR__` markers in pre-built HTML with the
    per-request `nonce="..."` attribute. Idempotent on strings without
    the marker; safe to call on any HTML response body."""
    return html.replace(NONCE_PLACEHOLDER, nonce_attr())
