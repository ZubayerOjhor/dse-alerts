"""Render a delivery as plain text and as HTML email.

The HTML is table-based with inline styles (what email clients support): a navy header
with the session and DSEX, stat tiles, one card per alert coloured by severity, the
portfolio snapshot, the watched symbols' day, the optional written summary, and the
standing notice that this is information, not advice.
"""

from __future__ import annotations

import datetime as dt
import html as h
import math

from dse_alerts.notify.context import DeliveryContext
from dse_alerts.rules import Alert

DISCLAIMER = ("These alerts are automated observations about prices, volumes and filings on the Dhaka Stock "
              "Exchange. They are not investment advice and no buy or sell recommendation is implied.")

_ORDER = {"high": 0, "notable": 1, "info": 2}
# email-safe palette (light surfaces; explicit colours so dark-mode clients do not invert meaning)
C = {"bg": "#eef1f5", "card": "#ffffff", "navy": "#0b1220", "navy2": "#172238", "ink": "#1d2229", "ink2": "#4b5563",
     "muted": "#6b7280", "line": "#e5e7eb", "up": "#1a7f37", "down": "#c62828", "warn": "#b45309", "info": "#4b5563",
     "accent": "#2a78d6", "tile": "#f5f7fa"}


def sort_alerts(alerts: list[Alert]) -> list[Alert]:
    return sorted(alerts, key=lambda a: (_ORDER.get(a.severity, 9), a.symbol, a.rule_id))


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _pct(x, d=1):
    return "–" if _missing(x) else f"{100 * float(x):+.{d}f}%"


def _num(x, d=0):
    return "–" if _missing(x) else f"{float(x):,.{d}f}"


def _tone(x):
    if _missing(x) or float(x) == 0:
        return C["muted"]
    return C["up"] if float(x) > 0 else C["down"]


def subject(session: dt.date, alerts: list[Alert]) -> str:
    n = len(alerts)
    high = sum(a.severity == "high" for a in alerts)
    syms = sorted({a.symbol for a in alerts})
    head = f"{n} alert{'s' if n != 1 else ''}" + (f", {high} high" if high else "")
    tail = ", ".join(syms[:4]) + (" +" + str(len(syms) - 4) if len(syms) > 4 else "")
    return f"DSE alerts {session.isoformat()}: {head} ({tail})" if n else f"DSE alerts {session.isoformat()}: nothing fired"


def text(ctx: DeliveryContext) -> str:
    lines = [f"DSE alerts for session {ctx.session.isoformat()}", "=" * 40, ""]
    if ctx.index.get("close"):
        lines += [f"DSEX {_num(ctx.index['close'], 2)} ({_pct(ctx.index.get('ret_1'), 2)} today, {_pct(ctx.index.get('ret_20'))} over 20 sessions)", ""]
    if ctx.digest:
        lines += ["Summary", "-" * 7, ctx.digest.strip(), ""]
    if not ctx.alerts:
        lines.append(f"Nothing fired for your {len(ctx.symbols)} watched symbols.")
    for a in sort_alerts(ctx.alerts):
        lines += [f"[{a.severity.upper()}] {a.headline}", f"    {a.detail}", ""]
    if ctx.portfolio:
        p = ctx.portfolio
        sign_d = "+" if (p.get("day_change") or 0) >= 0 else ""
        sign_p = "+" if (p.get("pnl") or 0) >= 0 else ""
        lines += ["Portfolio", "-" * 9,
                  (f"Value Tk {_num(p['value'])} ({_pct(p.get('day_change_pct'), 2)} today, {sign_d}{_num(p.get('day_change'))}); "
                   f"P&L {sign_p}{_num(p.get('pnl'))} ({_pct(p.get('pnl_pct'))}) on cost Tk {_num(p['cost'])}"), ""]
    if ctx.watch_rows:
        lines += ["Your symbols today", "-" * 18]
        for r in ctx.watch_rows:
            lines.append(f"{r['symbol']:12s} {_num(r['close'], 1):>9s} {_pct(r['ret_1']):>8s} today {_pct(r['ret_20']):>8s} / 20 sessions")
        lines.append("")
    lines += ["-" * 40, DISCLAIMER]
    return "\n".join(lines)


def _tile(label: str, value: str, sub: str, sub_color: str) -> str:
    return (f"<td style=\"padding:0 6px\"><table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:{C['tile']};border-radius:8px\">"
            f"<tr><td style=\"padding:12px 14px\"><div style=\"font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{C['muted']};font-weight:700\">{h.escape(label)}</div>"
            f"<div style=\"font-size:22px;font-weight:700;color:{C['ink']};margin:3px 0 2px\">{h.escape(value)}</div>"
            f"<div style=\"font-size:12.5px;font-weight:600;color:{sub_color}\">{h.escape(sub)}</div></td></tr></table></td>")


def html_body(ctx: DeliveryContext) -> str:
    sev_color = {"high": C["down"], "notable": C["warn"], "info": C["info"]}
    sev_label = {"high": "HIGH", "notable": "NOTABLE", "info": "INFO"}
    idx = ctx.index or {}
    parts = [
        f"<body style=\"margin:0;padding:0;background:{C['bg']};font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:{C['ink']}\">",
        f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:{C['bg']}\"><tr><td align=\"center\" style=\"padding:20px 10px\">",
        "<table role=\"presentation\" width=\"640\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:640px;width:100%\">",
        # header
        f"<tr><td style=\"background:{C['navy']};border-radius:12px 12px 0 0;padding:20px 24px\">",
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\"><tr>",
        f"<td style=\"color:#ffffff;font-size:20px;font-weight:700\">&#9650; DSE Alerts<div style=\"font-size:12.5px;font-weight:500;color:#b7c2d9;margin-top:3px\">Session {ctx.session.isoformat()}</div></td>",
        (f"<td align=\"right\" style=\"color:#ffffff\"><div style=\"font-size:11px;letter-spacing:.06em;color:#b7c2d9;text-transform:uppercase\">DSEX</div>"
         f"<div style=\"font-size:20px;font-weight:700\">{_num(idx.get('close'), 2)}</div>"
         f"<div style=\"font-size:12.5px;font-weight:600;color:{'#35c26b' if (idx.get('ret_1') or 0) >= 0 else '#ff6b6b'}\">{_pct(idx.get('ret_1'), 2)} today &middot; {_pct(idx.get('ret_20'))} / 20 sessions</div></td>"
         if idx.get("close") else "<td></td>"),
        "</tr></table></td></tr>",
        # body card
        f"<tr><td style=\"background:{C['card']};padding:18px 18px 8px\">",
    ]
    # tiles
    tiles = [_tile("Alerts", str(len(ctx.alerts)), f"{sum(a.severity == 'high' for a in ctx.alerts)} high, {sum(a.severity == 'notable' for a in ctx.alerts)} notable", C["muted"])]
    if ctx.portfolio:
        p = ctx.portfolio
        tiles.append(_tile("Portfolio", f"Tk {_num(p['value'])}", f"{'+' if (p.get('day_change') or 0) >= 0 else ''}{_num(p.get('day_change'))} ({_pct(p.get('day_change_pct'), 2)}) today", _tone(p.get("day_change"))))
        tiles.append(_tile("Unrealised P&L", f"{'+' if (p.get('pnl') or 0) >= 0 else ''}{_num(p.get('pnl'))}", f"{_pct(p.get('pnl_pct'))} on cost Tk {_num(p['cost'])}", _tone(p.get("pnl"))))
    else:
        tiles.append(_tile("Watched", str(len(ctx.symbols)), "symbols on your list", C["muted"]))
    parts.append("<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin:0 -6px 14px\"><tr>" + "".join(tiles) + "</tr></table>")
    # summary
    if ctx.digest:
        parts.append(f"<div style=\"margin:0 0 14px;padding:12px 14px;background:{C['tile']};border-left:4px solid {C['accent']};border-radius:6px;font-size:14px;line-height:1.5;white-space:pre-wrap\">{h.escape(ctx.digest.strip())}</div>")
    # alerts
    parts.append(f"<div style=\"font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{C['muted']};font-weight:700;margin:4px 0 8px\">What happened</div>")
    if not ctx.alerts:
        parts.append(f"<p style=\"margin:0 0 14px;color:{C['ink2']}\">Nothing fired for your {len(ctx.symbols)} watched symbols.</p>")
    for a in sort_alerts(ctx.alerts):
        col = sev_color.get(a.severity, C["info"])
        parts.append(
            f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin:0 0 10px\"><tr>"
            f"<td style=\"width:4px;background:{col};border-radius:4px 0 0 4px\"></td>"
            f"<td style=\"background:{C['tile']};padding:10px 12px;border-radius:0 6px 6px 0\">"
            f"<div style=\"font-size:11px;font-weight:700;color:{col};letter-spacing:.04em\">{sev_label.get(a.severity, a.severity.upper())} &middot; {h.escape(a.symbol)}</div>"
            f"<div style=\"font-size:15px;font-weight:650;color:{C['ink']};margin:2px 0\">{h.escape(a.headline)}</div>"
            f"<div style=\"font-size:13px;color:{C['ink2']};line-height:1.45\">{h.escape(a.detail)}</div>"
            f"<div style=\"font-size:11px;color:{C['muted']};margin-top:4px\">rule: {h.escape(a.rule_id)}</div></td></tr></table>"
        )
    # portfolio movers
    if ctx.portfolio and (ctx.portfolio.get("best") or ctx.portfolio.get("worst")):
        p = ctx.portfolio
        parts.append(f"<div style=\"font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{C['muted']};font-weight:700;margin:14px 0 8px\">Portfolio movers today</div>")
        parts.append("<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\"><tr>")
        for title, rows in (("Up", p.get("best", [])), ("Down", p.get("worst", []))):
            cells = "".join(f"<div style=\"font-size:13px;padding:3px 0\"><b>{h.escape(r['symbol'])}</b> <span style=\"color:{_tone(r['day_change'])}\">{_pct(r['ret_1'])}</span> <span style=\"color:{C['muted']}\">({'+' if (r['day_change'] or 0) >= 0 else ''}{_num(r['day_change'])})</span></div>" for r in rows if r["day_change"] == r["day_change"])
            parts.append(f"<td valign=\"top\" style=\"width:50%;padding:0 6px 0 0\"><div style=\"font-size:12px;color:{C['muted']}\">{title}</div>{cells}</td>")
        parts.append("</tr></table>")
    # watch rows
    if ctx.watch_rows:
        parts.append(f"<div style=\"font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{C['muted']};font-weight:700;margin:14px 0 6px\">Your symbols today</div>")
        parts.append("<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"font-size:13px\">")
        parts.append(f"<tr style=\"color:{C['muted']};font-size:11px;text-transform:uppercase\"><td style=\"padding:4px 6px;border-bottom:1px solid {C['line']}\">Symbol</td><td align=\"right\" style=\"padding:4px 6px;border-bottom:1px solid {C['line']}\">Close</td><td align=\"right\" style=\"padding:4px 6px;border-bottom:1px solid {C['line']}\">Today</td><td align=\"right\" style=\"padding:4px 6px;border-bottom:1px solid {C['line']}\">20 sessions</td></tr>")
        for r in ctx.watch_rows[:25]:
            tag = f" <span style=\"font-size:10px;color:{C['accent']};border:1px solid {C['accent']};border-radius:8px;padding:0 5px\">{r['kind']}</span>" if r.get("kind") in ("target",) else ""
            parts.append(f"<tr><td style=\"padding:5px 6px;border-bottom:1px solid {C['line']}\"><b>{h.escape(r['symbol'])}</b>{tag} <span style=\"color:{C['muted']}\">{h.escape(str(r.get('name') or ''))[:28]}</span></td>"
                         f"<td align=\"right\" style=\"padding:5px 6px;border-bottom:1px solid {C['line']}\">{_num(r['close'], 1)}</td>"
                         f"<td align=\"right\" style=\"padding:5px 6px;border-bottom:1px solid {C['line']};color:{_tone(r['ret_1'])};font-weight:600\">{_pct(r['ret_1'])}</td>"
                         f"<td align=\"right\" style=\"padding:5px 6px;border-bottom:1px solid {C['line']};color:{_tone(r['ret_20'])}\">{_pct(r['ret_20'])}</td></tr>")
        parts.append("</table>")
    parts.append("</td></tr>")
    # footer
    link = f" &middot; <a href=\"{h.escape(ctx.base_url)}\" style=\"color:{C['accent']}\">Open the dashboard</a>" if ctx.base_url else ""
    parts.append(f"<tr><td style=\"background:{C['card']};border-radius:0 0 12px 12px;padding:12px 18px 18px;border-top:1px solid {C['line']}\">"
                 f"<div style=\"font-size:11.5px;color:{C['muted']};line-height:1.5\">{h.escape(DISCLAIMER)}{link}</div></td></tr>")
    parts.append("</table></td></tr></table></body>")
    return "".join(parts)
