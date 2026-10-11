"""Render a delivery as plain text and as HTML email, in the user's language.

The HTML is table-based with inline styles (what email clients support): a navy header
with the session and DSEX, stat tiles, one card per alert coloured by severity, the
portfolio snapshot, the watched symbols' day, the optional written summary, and the
standing notice that this is information, not advice.
"""

from __future__ import annotations

import datetime as dt
import html as h
import math

from dse_alerts.i18n import plural, t
from dse_alerts.i18n.alerts import localize
from dse_alerts.notify.context import DeliveryContext
from dse_alerts.rules import Alert

DISCLAIMER = t("en", "email.disclaimer")

_ORDER = {"high": 0, "notable": 1, "info": 2}
# email-safe palette (light surfaces; explicit colours so dark-mode clients do not invert meaning)
C = {"bg": "#eef1f5", "card": "#ffffff", "navy": "#0b1220", "navy2": "#172238", "ink": "#1d2229", "ink2": "#4b5563",
     "muted": "#6b7280", "line": "#e5e7eb", "up": "#1a7f37", "down": "#c62828", "warn": "#b45309", "info": "#4b5563",
     "accent": "#2a78d6", "tile": "#f5f7fa"}
FONT = "-apple-system,'Segoe UI','Noto Sans Bengali','Nirmala UI',Roboto,Helvetica,Arial,sans-serif"


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


def _signed(x, d=0):
    return ("+" if (x or 0) >= 0 else "") + _num(x or 0, d)


def subject(session: dt.date, alerts: list[Alert], lang: str = "en") -> str:
    n = len(alerts)
    high = sum(a.severity == "high" for a in alerts)
    syms = sorted({a.symbol for a in alerts})
    head = plural(lang, n, "email.n_alerts_one", "email.n_alerts_many") + (", " + t(lang, "email.n_high", n=high) if high else "")
    tail = ", ".join(syms[:4]) + (" +" + str(len(syms) - 4) if len(syms) > 4 else "")
    if not n:
        return t(lang, "email.subject_none", date=session.isoformat())
    return t(lang, "email.subject_some", date=session.isoformat(), head=head, tail=tail)


def text(ctx: DeliveryContext) -> str:
    L = ctx.lang
    lines = [t(L, "email.title", date=ctx.session.isoformat()), "=" * 40, ""]
    if ctx.index.get("close"):
        lines += [t(L, "email.dsex_line", close=_num(ctx.index["close"], 2), r1=_pct(ctx.index.get("ret_1"), 2), r20=_pct(ctx.index.get("ret_20"))), ""]
    if ctx.digest:
        s = t(L, "email.summary")
        lines += [s, "-" * len(s), ctx.digest.strip(), ""]
    if not ctx.alerts:
        lines.append(t(L, "email.nothing", n=len(ctx.symbols)))
    for a in sort_alerts(ctx.alerts):
        head, det = localize(a.rule_id, a.symbol, a.evidence, a.headline, a.detail, L)
        lines += [f"[{t(L, 'sev.' + a.severity)}] {head}", f"    {det}", ""]
    if ctx.portfolio:
        p = ctx.portfolio
        s = t(L, "email.portfolio")
        lines += [s, "-" * len(s),
                  t(L, "email.portfolio_line", value=_num(p["value"]), dpct=_pct(p.get("day_change_pct"), 2), dchg=_signed(p.get("day_change")),
                    pnl=_signed(p.get("pnl")), pnlpct=_pct(p.get("pnl_pct")), cost=_num(p["cost"])), ""]
    if ctx.watch_rows:
        s = t(L, "email.your_symbols")
        lines += [s, "-" * len(s)]
        for r in ctx.watch_rows:
            lines.append(f"{r['symbol']:12s} {_num(r['close'], 1):>9s} {_pct(r['ret_1']):>8s} {t(L, 'email.row_today')} {_pct(r['ret_20']):>8s} {t(L, 'email.row_20')}")
        lines.append("")
    lines += ["-" * 40, t(L, "email.disclaimer")]
    return "\n".join(lines)


def _tile(label: str, value: str, sub: str, sub_color: str) -> str:
    return (f"<td style=\"padding:0 6px\"><table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:{C['tile']};border-radius:8px\">"
            f"<tr><td style=\"padding:12px 14px\"><div style=\"font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{C['muted']};font-weight:700\">{h.escape(label)}</div>"
            f"<div style=\"font-size:22px;font-weight:700;color:{C['ink']};margin:3px 0 2px\">{h.escape(value)}</div>"
            f"<div style=\"font-size:12.5px;font-weight:600;color:{sub_color}\">{h.escape(sub)}</div></td></tr></table></td>")


def _label(txt: str, margin: str = "4px 0 8px") -> str:
    return f"<div style=\"font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{C['muted']};font-weight:700;margin:{margin}\">{h.escape(txt)}</div>"


def html_body(ctx: DeliveryContext) -> str:
    L = ctx.lang
    sev_color = {"high": C["down"], "notable": C["warn"], "info": C["info"]}
    idx = ctx.index or {}
    parts = [
        f"<body style=\"margin:0;padding:0;background:{C['bg']};font-family:{FONT};color:{C['ink']}\">",
        f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:{C['bg']}\"><tr><td align=\"center\" style=\"padding:20px 10px\">",
        "<table role=\"presentation\" width=\"640\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:640px;width:100%\">",
        # header
        f"<tr><td style=\"background:{C['navy']};border-radius:12px 12px 0 0;padding:20px 24px\">",
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\"><tr>",
        f"<td style=\"color:#ffffff;font-size:20px;font-weight:700\">&#9650; {h.escape(t(L, 'email.header'))}<div style=\"font-size:12.5px;font-weight:500;color:#b7c2d9;margin-top:3px\">{h.escape(t(L, 'email.session', date=ctx.session.isoformat()))}</div></td>",
        (f"<td align=\"right\" style=\"color:#ffffff\"><div style=\"font-size:11px;letter-spacing:.06em;color:#b7c2d9;text-transform:uppercase\">DSEX</div>"
         f"<div style=\"font-size:20px;font-weight:700\">{_num(idx.get('close'), 2)}</div>"
         f"<div style=\"font-size:12.5px;font-weight:600;color:{'#35c26b' if (idx.get('ret_1') or 0) >= 0 else '#ff6b6b'}\">{h.escape(t(L, 'email.today_20', r1=_pct(idx.get('ret_1'), 2), r20=_pct(idx.get('ret_20'))))}</div></td>"
         if idx.get("close") else "<td></td>"),
        "</tr></table></td></tr>",
        # body card
        f"<tr><td style=\"background:{C['card']};padding:18px 18px 8px\">",
    ]
    # tiles
    n_high = sum(a.severity == "high" for a in ctx.alerts)
    n_notable = sum(a.severity == "notable" for a in ctx.alerts)
    tiles = [_tile(t(L, "email.tile_alerts"), str(len(ctx.alerts)), t(L, "email.tile_alerts_sub", high=n_high, notable=n_notable), C["muted"])]
    if ctx.portfolio:
        p = ctx.portfolio
        tiles.append(_tile(t(L, "email.tile_portfolio"), f"Tk {_num(p['value'])}",
                           f"{_signed(p.get('day_change'))} ({_pct(p.get('day_change_pct'), 2)}) {t(L, 'email.row_today')}", _tone(p.get("day_change"))))
        tiles.append(_tile(t(L, "email.tile_pnl"), _signed(p.get("pnl")), t(L, "email.on_cost", pct=_pct(p.get("pnl_pct")), cost=_num(p["cost"])), _tone(p.get("pnl"))))
    else:
        tiles.append(_tile(t(L, "email.tile_watched"), str(len(ctx.symbols)), t(L, "email.tile_watched_sub"), C["muted"]))
    parts.append("<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin:0 -6px 14px\"><tr>" + "".join(tiles) + "</tr></table>")
    # summary
    if ctx.digest:
        parts.append(f"<div style=\"margin:0 0 14px;padding:12px 14px;background:{C['tile']};border-left:4px solid {C['accent']};border-radius:6px;font-size:14px;line-height:1.5;white-space:pre-wrap\">{h.escape(ctx.digest.strip())}</div>")
    # alerts
    parts.append(_label(t(L, "email.what_happened")))
    if not ctx.alerts:
        parts.append(f"<p style=\"margin:0 0 14px;color:{C['ink2']}\">{h.escape(t(L, 'email.nothing', n=len(ctx.symbols)))}</p>")
    for a in sort_alerts(ctx.alerts):
        col = sev_color.get(a.severity, C["info"])
        head, det = localize(a.rule_id, a.symbol, a.evidence, a.headline, a.detail, L)
        parts.append(
            f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin:0 0 10px\"><tr>"
            f"<td style=\"width:4px;background:{col};border-radius:4px 0 0 4px\"></td>"
            f"<td style=\"background:{C['tile']};padding:10px 12px;border-radius:0 6px 6px 0\">"
            f"<div style=\"font-size:11px;font-weight:700;color:{col};letter-spacing:.04em\">{h.escape(t(L, 'sev.' + a.severity))} &middot; {h.escape(a.symbol)}</div>"
            f"<div style=\"font-size:15px;font-weight:650;color:{C['ink']};margin:2px 0\">{h.escape(head)}</div>"
            f"<div style=\"font-size:13px;color:{C['ink2']};line-height:1.45\">{h.escape(det)}</div>"
            f"<div style=\"font-size:11px;color:{C['muted']};margin-top:4px\">{h.escape(t(L, 'email.rule', id=a.rule_id))}</div></td></tr></table>"
        )
    # portfolio movers
    if ctx.portfolio and (ctx.portfolio.get("best") or ctx.portfolio.get("worst")):
        p = ctx.portfolio
        parts.append(_label(t(L, "email.movers"), "14px 0 8px"))
        parts.append("<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\"><tr>")
        for title, rows in ((t(L, "email.up"), p.get("best", [])), (t(L, "email.down"), p.get("worst", []))):
            cells = "".join(f"<div style=\"font-size:13px;padding:3px 0\"><b>{h.escape(r['symbol'])}</b> <span style=\"color:{_tone(r['day_change'])}\">{_pct(r['ret_1'])}</span> <span style=\"color:{C['muted']}\">({_signed(r['day_change'])})</span></div>" for r in rows if r["day_change"] == r["day_change"])
            parts.append(f"<td valign=\"top\" style=\"width:50%;padding:0 6px 0 0\"><div style=\"font-size:12px;color:{C['muted']}\">{h.escape(title)}</div>{cells}</td>")
        parts.append("</tr></table>")
    # watch rows
    if ctx.watch_rows:
        parts.append(_label(t(L, "email.your_symbols"), "14px 0 6px"))
        parts.append("<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"font-size:13px\">")
        th = f"padding:4px 6px;border-bottom:1px solid {C['line']}"
        parts.append(f"<tr style=\"color:{C['muted']};font-size:11px;text-transform:uppercase\"><td style=\"{th}\">{h.escape(t(L, 'email.col_symbol'))}</td>"
                     f"<td align=\"right\" style=\"{th}\">{h.escape(t(L, 'email.col_close'))}</td><td align=\"right\" style=\"{th}\">{h.escape(t(L, 'email.col_today'))}</td>"
                     f"<td align=\"right\" style=\"{th}\">{h.escape(t(L, 'email.col_20'))}</td></tr>")
        for r in ctx.watch_rows[:25]:
            tag = f" <span style=\"font-size:10px;color:{C['accent']};border:1px solid {C['accent']};border-radius:8px;padding:0 5px\">{h.escape(t(L, 'common.target'))}</span>" if r.get("kind") in ("target",) else ""
            parts.append(f"<tr><td style=\"padding:5px 6px;border-bottom:1px solid {C['line']}\"><b>{h.escape(r['symbol'])}</b>{tag} <span style=\"color:{C['muted']}\">{h.escape(str(r.get('name') or ''))[:28]}</span></td>"
                         f"<td align=\"right\" style=\"padding:5px 6px;border-bottom:1px solid {C['line']}\">{_num(r['close'], 1)}</td>"
                         f"<td align=\"right\" style=\"padding:5px 6px;border-bottom:1px solid {C['line']};color:{_tone(r['ret_1'])};font-weight:600\">{_pct(r['ret_1'])}</td>"
                         f"<td align=\"right\" style=\"padding:5px 6px;border-bottom:1px solid {C['line']};color:{_tone(r['ret_20'])}\">{_pct(r['ret_20'])}</td></tr>")
        parts.append("</table>")
    parts.append("</td></tr>")
    # footer
    link = f" &middot; <a href=\"{h.escape(ctx.base_url)}\" style=\"color:{C['accent']}\">{h.escape(t(L, 'email.open'))}</a>" if ctx.base_url else ""
    parts.append(f"<tr><td style=\"background:{C['card']};border-radius:0 0 12px 12px;padding:12px 18px 18px;border-top:1px solid {C['line']}\">"
                 f"<div style=\"font-size:11.5px;color:{C['muted']};line-height:1.5\">{h.escape(t(L, 'email.disclaimer'))}{link}</div></td></tr>")
    parts.append("</table></td></tr></table></body>")
    return "".join(parts)
