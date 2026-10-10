"""Render a user's alerts for one session as plain text and as simple HTML.

Both renderings carry the same content: a one-line headline per alert, the detail, and
the standing notice that this is information, not advice.
"""

from __future__ import annotations

import datetime as dt
import html

from dse_alerts.rules import Alert

DISCLAIMER = ("These alerts are automated observations about prices, volumes and filings on the Dhaka Stock "
              "Exchange. They are not investment advice and no buy or sell recommendation is implied.")

_ORDER = {"high": 0, "notable": 1, "info": 2}


def sort_alerts(alerts: list[Alert]) -> list[Alert]:
    return sorted(alerts, key=lambda a: (_ORDER.get(a.severity, 9), a.symbol, a.rule_id))


def subject(session: dt.date, alerts: list[Alert]) -> str:
    n = len(alerts)
    high = sum(a.severity == "high" for a in alerts)
    syms = sorted({a.symbol for a in alerts})
    head = f"{n} alert{'s' if n != 1 else ''}" + (f", {high} high" if high else "")
    tail = ", ".join(syms[:4]) + (" +" + str(len(syms) - 4) if len(syms) > 4 else "")
    return f"DSE alerts {session.isoformat()}: {head} ({tail})" if n else f"DSE alerts {session.isoformat()}: nothing fired"


def text(session: dt.date, alerts: list[Alert], symbols: list[str], digest: str | None = None) -> str:
    lines = [f"DSE alerts for session {session.isoformat()}", "=" * 40, ""]
    if digest:
        lines += ["Summary", "-" * 7, digest.strip(), ""]
    if not alerts:
        lines.append(f"Nothing fired for your {len(symbols)} watched symbols.")
    for a in sort_alerts(alerts):
        lines += [f"[{a.severity.upper()}] {a.headline}", f"    {a.detail}", ""]
    lines += ["-" * 40, DISCLAIMER]
    return "\n".join(lines)


def html_body(session: dt.date, alerts: list[Alert], symbols: list[str], digest: str | None = None) -> str:
    color = {"high": "#b00020", "notable": "#8a5a00", "info": "#555"}
    parts = [
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;color:#222\">",
        f"<h2 style=\"margin:16px 0 4px\">DSE alerts</h2><div style=\"color:#666\">Session {session.isoformat()}</div>",
    ]
    if digest:
        parts.append(f"<div style=\"margin:16px 0;padding:12px 14px;background:#f5f5f5;border-radius:6px;white-space:pre-wrap\">{html.escape(digest.strip())}</div>")
    if not alerts:
        parts.append(f"<p>Nothing fired for your {len(symbols)} watched symbols.</p>")
    for a in sort_alerts(alerts):
        parts.append(
            "<div style=\"margin:14px 0;padding:10px 12px;border-left:4px solid "
            f"{color.get(a.severity, '#555')};background:#fafafa\">"
            f"<div style=\"font-weight:600\">{html.escape(a.headline)}</div>"
            f"<div style=\"font-size:13px;color:#444;margin-top:4px\">{html.escape(a.detail)}</div>"
            f"<div style=\"font-size:11px;color:#888;margin-top:4px\">{a.severity} · {a.rule_id}</div></div>"
        )
    parts.append(f"<p style=\"font-size:11px;color:#888;margin-top:24px\">{html.escape(DISCLAIMER)}</p></div>")
    return "".join(parts)
