"""Alert headlines and details in the reader's language.

Rules write their headline and detail in English when they fire, and that text is what
the database stores. For another language we rebuild both from the numbers the rule kept
in ``evidence`` (the same numbers the AI digest sees). Anything the evidence does not
carry (a filing's body text, a dividend wording from the exchange) stays as the exchange
published it. When the evidence is missing a field, the stored English is returned so a
reader never sees a broken sentence.
"""

from __future__ import annotations

import json
from typing import Any

from dse_alerts.i18n import t


def _pct(x, d=1) -> str:
    return "n/a" if x is None else f"{100 * float(x):+.{d}f}%"


def _num(x, d=1) -> str:
    return "–" if x is None else f"{float(x):,.{d}f}"


def localize(rule_id: str, symbol: str, evidence: dict[str, Any] | str | None, headline: str, detail: str,
             lang: str | None) -> tuple[str, str]:
    """Return ``(headline, detail)`` in ``lang``; English and unknown rules pass through."""
    if not lang or lang == "en":
        return headline, detail
    ev: dict[str, Any] = {}
    if isinstance(evidence, str):
        try:
            ev = json.loads(evidence) or {}
        except ValueError:
            ev = {}
    elif isinstance(evidence, dict):
        ev = evidence
    try:
        out = _build(rule_id, symbol, ev, headline, detail, lang)
    except (KeyError, TypeError, ValueError):
        out = None
    return out or (headline, detail)


def _build(rule_id: str, sym: str, ev: dict, headline: str, detail: str, lang: str) -> tuple[str, str] | None:
    if rule_id in ("big_drop", "big_rise"):
        if ev.get("ret") is None or ev.get("days") is None:
            return None
        head = t(lang, f"al.{rule_id}", symbol=sym, ret=_pct(ev["ret"]), days=int(ev["days"]))
        det = t(lang, "al.big_detail", close=_num(ev.get("close")), threshold=_pct(ev.get("threshold")))
        return head, det
    if rule_id in ("upper_circuit", "lower_circuit"):
        if ev.get("ret_1") is None:
            return None
        side = t(lang, "common.upper" if rule_id == "upper_circuit" else "common.lower")
        streak = int(ev.get("streak") or 1)
        nth = t(lang, "al.circuit_streak", streak=streak) if streak > 1 else ""
        head = t(lang, "al.circuit", symbol=sym, side=side, ret=_pct(ev["ret_1"]), nth=nth)
        det = t(lang, "al.circuit_detail", limit=f"{100 * float(ev.get('slab_limit') or 0):.2f}")
        if ev.get("hits_per_year") is not None:
            det += " " + t(lang, "al.circuit_detail_hits", side=side, hits=f"{float(ev['hits_per_year']):.1f}")
        return head, det
    if rule_id == "volume_spike":
        if ev.get("vol_ratio") is None or ev.get("ret_1") is None:
            return None
        head = t(lang, "al.volume_spike", symbol=sym, ratio=f"{float(ev['vol_ratio']):.1f}", ret=_pct(ev["ret_1"]))
        det = t(lang, "al.volume_detail", threshold=f"{float(ev.get('threshold') or 0):.1f}")
        return head, det
    if rule_id in ("new_250d_high", "new_250d_low"):
        if ev.get("close") is None:
            return None
        head = t(lang, f"al.{rule_id}", symbol=sym, close=_num(ev["close"]))
        det = t(lang, "al.range_detail_" + ("high" if rule_id.endswith("high") else "low"), prev=_num(ev.get("previous_extreme")))
        return head, det
    if rule_id == "vs_index":
        if ev.get("ret_1") is None or ev.get("dsex_ret_1") is None:
            return None
        head = t(lang, "al.vs_index", symbol=sym, ret=_pct(ev["ret_1"]), dsex=_pct(ev["dsex_ret_1"]))
        det = t(lang, "al.vs_index_detail", diff=_pct(ev.get("diff")))
        return head, det
    if rule_id == "psi_news":
        titles = ev.get("titles") or []
        if not titles:
            return None
        more = t(lang, "al.psi_more", n=int(ev.get("count", len(titles))) - 1) if int(ev.get("count", 1)) > 1 else ""
        head = t(lang, "al.psi_news", symbol=sym, title=str(titles[0])[:80], more=more)
        return head, detail
    if rule_id == "record_date":
        if not ev.get("record_date"):
            return None
        head = t(lang, "al.record_date", symbol=sym, date=str(ev["record_date"]), dividend=str(ev.get("dividend", ""))[:60])
        return head, detail
    if rule_id == "ma_cross":
        if ev.get("direction") is None:
            return None
        key = "al.ma_cross_above" if int(ev["direction"]) > 0 else "al.ma_cross_below"
        head = t(lang, key, symbol=sym)
        det = t(lang, "al.ma_detail", close=_num(ev.get("close")), sma50=_num(ev.get("sma_50")))
        return head, det
    if rule_id == "pe_level":
        if ev.get("pe") is None:
            return None
        pe = float(ev["pe"])
        hi, lo = ev.get("max"), ev.get("min")
        cond = t(lang, "al.pe_max", max=hi) if hi is not None and pe <= float(hi) else t(lang, "al.pe_min", min=lo)
        head = t(lang, "al.pe_level", symbol=sym, pe=f"{pe:.1f}", cond=cond)
        return head, detail
    if rule_id == "illiquid":
        if ev.get("zero_volume_sessions") is None:
            return None
        head = t(lang, "al.illiquid", symbol=sym, n=int(ev["zero_volume_sessions"]))
        return head, t(lang, "al.illiquid_detail")
    return None
