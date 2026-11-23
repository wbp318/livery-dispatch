"""Shared Fireworks-aesthetic CSS tokens used by dashboard, admin pages, and map.

Each module concatenates BASE_CSS + its own layout-specific CSS block.
Root variables, resets, badge/status/kind/tier colors, tags, and footer
live here so a palette change hits one file, not three.

Also hosts the small shared server-render formatting helpers (date, fare,
status-label, truncate) so the three page modules don't each re-implement —
and silently drift — them.
"""

from __future__ import annotations

from datetime import datetime

BASE_CSS = """
:root{
  --ink:#0a0c12; --ink-2:#10131a; --paper:#fbf6e8; --rule:#3a3527;
  --rule-soft:rgba(58,53,39,0.55);
  --brass-hi:#f3d684; --brass:#c9a961; --brass-mid:#a98944; --brass-deep:#6b5224;
  --brass-glow:rgba(243,214,132,0.45);
  --ok:#9ad08a; --warn:#f0a050; --bad:#e3795f; --info:#88baea;
  --row-alt:rgba(255,255,255,0.025);
  --bg:var(--ink); --text:var(--paper);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--ink);color:var(--paper);
  font:13px/1.45 "Lucida Grande","Helvetica Neue",Helvetica,sans-serif;}

/* Status badges — shared across dashboard, admin, map sidebar */
.badge{font-size:10.5px;letter-spacing:0.1em;text-transform:uppercase;
  padding:3px 8px;border-radius:2px;display:inline-block;
  background:rgba(58,53,39,0.4);color:rgba(251,246,232,0.7);}
.status{font-size:10.5px;letter-spacing:0.1em;text-transform:uppercase;
  padding:2px 6px;border-radius:2px;display:inline-block;}
.status-requested{background:rgba(136,186,234,0.18);color:#a3c8e8;}
.status-broadcasting{background:rgba(201,169,97,0.25);color:var(--brass-hi);}
.status-accepted{background:rgba(90,140,74,0.25);color:#a5d499;}
.status-en_route_pickup{background:rgba(168,80,58,0.25);color:#f0a899;}
.status-on_scene{background:rgba(201,122,26,0.25);color:#f3c178;}
.status-in_transit{background:rgba(56,116,160,0.25);color:#a3c8e8;}
.status-completed{background:rgba(90,140,74,0.15);color:#7ea96e;}
.status-cancelled{background:rgba(58,53,39,0.5);color:rgba(251,246,232,0.55);}
.status-no_drivers{background:rgba(168,80,58,0.18);color:#dc8a7a;}
.status-available{background:rgba(90,140,74,0.22);color:#a5d499;}
.status-on_trip{background:rgba(201,122,26,0.25);color:#f3c178;}
.status-offline{background:rgba(58,53,39,0.5);color:rgba(251,246,232,0.55);}
.status-off_duty{background:rgba(58,53,39,0.5);color:rgba(251,246,232,0.6);}
.status-pending_review{background:rgba(201,169,97,0.22);color:var(--brass-hi);}
.status-confirmed{background:rgba(90,140,74,0.22);color:#a5d499;}
.status-declined{background:rgba(168,80,58,0.22);color:#dc8a7a;}
.status-converted{background:rgba(56,116,160,0.22);color:#a3c8e8;}
.status-spam{background:rgba(58,53,39,0.5);color:rgba(251,246,232,0.45);}
.status-responded{background:rgba(56,116,160,0.22);color:#a3c8e8;}
.status-closed{background:rgba(58,53,39,0.4);color:rgba(251,246,232,0.55);}

/* Kind badges (inquiry types) */
.kind{font-size:10.5px;letter-spacing:0.08em;padding:2px 6px;
  border-radius:2px;text-transform:uppercase;}
.kind-wedding{background:rgba(201,169,97,0.25);color:var(--brass-hi);}
.kind-vendor{background:rgba(90,140,74,0.22);color:#a5d499;}
.kind-early_access{background:rgba(56,116,160,0.22);color:#a3c8e8;}

/* Tier badges (subcontractor tiers) */
.tier{font:600 10px/1 "Lucida Grande",sans-serif;letter-spacing:0.1em;
  padding:2px 6px;border-radius:2px;display:inline-block;}
.tier-1{background:rgba(243,214,132,0.22);color:var(--brass-hi);}
.tier-2{background:rgba(168,137,68,0.18);color:#d4b76e;}
.tier-3{background:rgba(58,53,39,0.5);color:rgba(251,246,232,0.6);}

/* Broadcast response badges */
.response-pending{background:rgba(201,169,97,0.18);color:var(--brass);}
.response-accepted{background:rgba(90,140,74,0.22);color:#a5d499;}
.response-declined{background:rgba(168,80,58,0.18);color:#dc8a7a;}
.response-expired{background:rgba(58,53,39,0.4);color:rgba(251,246,232,0.5);}

/* Call outcome badges */
.call-answered{background:rgba(90,140,74,0.22);color:#a5d499;}
.call-voicemail{background:rgba(201,169,97,0.22);color:var(--brass-hi);}
.call-missed{background:rgba(168,80,58,0.22);color:#dc8a7a;}
.call-no_answer{background:rgba(168,80,58,0.18);color:#dc8a7a;}
.call-failed{background:rgba(168,80,58,0.18);color:#dc8a7a;}

/* Assignment tags */
.tag{font-size:10.5px;letter-spacing:0.08em;padding:2px 6px;border-radius:2px;
  display:inline-block;}
.tag.own{background:rgba(90,140,74,0.22);color:#a5d499;}
.tag.sub{background:rgba(56,116,160,0.22);color:#a3c8e8;}
.tag.none{color:rgba(251,246,232,0.4);}

/* Empty state */
.empty{padding:18px;text-align:center;color:rgba(251,246,232,0.45);
  font-style:italic;border:1px dashed var(--rule);border-radius:3px;
  background:rgba(255,255,255,0.01);}

/* Footer */
footer{padding:14px 22px;border-top:1px solid var(--brass-deep);margin-top:32px;
  font:10px/1.4 "Lucida Grande",sans-serif;letter-spacing:0.04em;
  color:rgba(251,246,232,0.45);text-align:center;background:rgba(0,0,0,0.3);}
"""


def _fmt_dt(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a datetime for display, or an em-dash when None."""
    if dt is None:
        return "—"
    return dt.strftime(fmt)


def _fmt_fare(cents: int | None, *, grouped: bool = False) -> str:
    """Render cents as a dollar string, or an em-dash when None. `grouped=True`
    adds a thousands separator (the driver-surface convention)."""
    if cents is None:
        return "—"
    return f"${cents / 100:,.2f}" if grouped else f"${cents / 100:.2f}"


def _status_label(s) -> str:
    """Humanize an enum (or its .value, or any string) for display:
    SCREAMING_SNAKE -> 'Title Case Words'. None-safe."""
    if s is None:
        return "—"
    v = s.value if hasattr(s, "value") else str(s)
    return v.replace("_", " ").title()


def _truncate(s: str | None, n: int) -> str:
    """Truncate to n chars with an ellipsis; empty string for falsy input."""
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"
