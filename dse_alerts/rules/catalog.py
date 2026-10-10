"""The rule catalog. See ``base.py`` for the shape of a rule.

Design notes
* A move is "big" relative to the stock's own history (``symbol_stats`` percentiles) unless
  the user sets an absolute percentage, in which case that wins. This is what keeps the
  alert count sane on the DSE (see docs/calibration.md).
* Rules that describe a *state* (P/E below X, illiquid) fire when the state begins, not
  every day it persists.
* ``predicate_sql`` runs against a view that joins ``features`` with ``symbol_stats``; it
  is the rule with its default parameters, used by ``dse alerts backtest``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from dse_alerts.rules.base import Alert, Rule, RuleContext, pct

_DAYS = {1: ("ret_1", "abs_ret1_p95", None), 5: ("ret_5", None, None),
         10: ("ret_10", "ret10_p05", "ret10_p95"), 20: ("ret_20", "ret20_p05", "ret20_p95")}


def _threshold(ctx: RuleContext, days: int, direction: int, abs_pct: float | None) -> tuple[float | None, str]:
    """Return (threshold as a signed return, how it was chosen)."""
    if abs_pct is not None:
        return direction * abs(abs_pct), f"your setting of {abs(abs_pct) * 100:.0f}%"
    _, lo, hi = _DAYS[days]
    if days == 1:
        v = ctx.stat("abs_ret1_p95")
        return (direction * v, "this stock's 95th-percentile daily move") if v else (None, "")
    if days == 5:  # no stored percentile: scale the 10-session one
        v = ctx.stat("ret10_p05" if direction < 0 else "ret10_p95")
        return (0.75 * v, "three quarters of this stock's 10-session 5th/95th percentile") if v else (None, "")
    v = ctx.stat(lo if direction < 0 else hi)
    return (v, f"this stock's {days}-session {'5th' if direction < 0 else '95th'} percentile") if v else (None, "")


def _usual_range(ctx: RuleContext, days: int) -> str:
    lo, hi = ctx.stat("ret10_p05" if days <= 10 else "ret20_p05"), ctx.stat("ret10_p95" if days <= 10 else "ret20_p95")
    return f"{pct(lo)} to {pct(hi)}" if lo is not None and hi is not None else "n/a"


def _big_move(direction: int):
    def evaluate(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
        days = int(p.get("days", 10))
        if days not in _DAYS:
            return None
        ret = ctx.val(_DAYS[days][0])
        thr, how = _threshold(ctx, days, direction, p.get("pct"))
        if ret is None or thr is None:
            return None
        if (direction < 0 and ret > thr) or (direction > 0 and ret < thr):
            return None
        # fire when the threshold is first crossed, not on every session of the episode
        prev = ctx.val(f"{_DAYS[days][0]}_prev") if days > 1 else None
        if prev is not None and ((direction < 0 and prev <= thr) or (direction > 0 and prev >= thr)):
            return None
        word = "down" if direction < 0 else "up"
        severe = abs(ret) >= 1.5 * abs(thr)
        return Alert(
            symbol=ctx.row["symbol"], rule_id="big_drop" if direction < 0 else "big_rise", date=ctx.as_of,
            headline=f"{ctx.row['symbol']} {word} {pct(ret)} over {days} session{'s' if days > 1 else ''}",
            detail=(f"Close {ctx.row['close']:.1f}. Threshold {pct(thr)} ({how}). "
                    f"Usual {min(days, 20) if days > 1 else 10}-session range for this stock: {_usual_range(ctx, days)}."),
            severity="high" if severe else "notable",
            evidence={"ret": ret, "threshold": thr, "days": days, "close": ctx.val("close")},
        )
    return evaluate


def _circuit(side: str):
    def evaluate(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
        hit = bool(ctx.row.get("hit_upper" if side == "upper" else "hit_lower"))
        streak = int(ctx.val("upper_streak" if side == "upper" else "lower_streak") or 0)
        need = int(p.get("streak", 1))
        if not hit or streak < need:
            return None
        lim = ctx.val("slab_limit") or 0
        nth = {1: "", 2: ", 2nd session in a row", 3: ", 3rd session in a row"}.get(streak, f", {streak}th session in a row")
        per_year = ctx.stat("upper_hits_per_250" if side == "upper" else "lower_hits_per_250")
        return Alert(
            symbol=ctx.row["symbol"], rule_id=f"{side}_circuit", date=ctx.as_of,
            headline=f"{ctx.row['symbol']} closed at its {side} circuit ({pct(ctx.val('ret_1'))}){nth}",
            detail=(f"Daily limit for a Tk {ctx.val('prev_close'):.1f} share is {lim * 100:.2f}%. "
                    + (f"This stock hits the {side} circuit about {per_year:.1f} times a year." if per_year is not None else "")),
            severity="high" if streak >= 3 else "notable",
            evidence={"ret_1": ctx.val("ret_1"), "streak": streak, "slab_limit": lim, "hits_per_year": per_year},
        )
    return evaluate


def _volume_spike(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    ratio = ctx.val("vol_ratio_20")
    r1 = ctx.val("ret_1")
    if ratio is None or r1 is None:
        return None
    thr = float(p.get("ratio", 3.0))
    if p.get("use_stock_baseline", True):
        thr = max(thr, ctx.stat("vol_ratio_p95", thr))
    if ratio < thr or abs(r1) < float(p.get("min_move", 0.03)):
        return None
    return Alert(
        symbol=ctx.row["symbol"], rule_id="volume_spike", date=ctx.as_of,
        headline=f"{ctx.row['symbol']} traded {ratio:.1f}x its 20-session average volume, price {pct(r1)}",
        detail=(f"Volume {int(ctx.val('volume') or 0):,} vs a 20-session average of "
                f"{int((ctx.val('volume') or 0) / ratio):,}. Threshold {thr:.1f}x."),
        severity="high" if ratio >= 2 * thr else "notable",
        evidence={"vol_ratio": ratio, "ret_1": r1, "threshold": thr},
    )


def _range_break(side: str):
    def evaluate(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
        close, ref = ctx.val("close"), ctx.val("high_250" if side == "high" else "low_250")
        if close is None or ref is None:
            return None
        if (side == "high" and close <= ref) or (side == "low" and close >= ref):
            return None
        return Alert(
            symbol=ctx.row["symbol"], rule_id=f"new_250d_{side}", date=ctx.as_of,
            headline=f"{ctx.row['symbol']} closed at a 250-session {side}: {close:.1f}",
            detail=f"Previous 250-session {side} was {ref:.1f}.",
            severity="notable",
            evidence={"close": close, "previous_extreme": ref},
        )
    return evaluate


def _vs_index(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    r1, idx = ctx.val("ret_1"), ctx.val("dsex_ret_1")
    if r1 is None or idx is None:
        return None
    diff = r1 - idx
    thr = float(p.get("pct", 0.05))
    if abs(diff) < thr:
        return None
    return Alert(
        symbol=ctx.row["symbol"], rule_id="vs_index", date=ctx.as_of,
        headline=f"{ctx.row['symbol']} moved {pct(r1)} while DSEX moved {pct(idx)}",
        detail=f"Difference {pct(diff)} against a threshold of {thr * 100:.0f} points.",
        severity="notable",
        evidence={"ret_1": r1, "dsex_ret_1": idx, "diff": diff},
    )


def _psi_news(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    if ctx.news.empty:
        return None
    since = ctx.as_of - dt.timedelta(days=int(p.get("lookback_days", 2)))
    recent = ctx.news[(pd.to_datetime(ctx.news["date"]).dt.date >= since)]
    if recent.empty:
        return None
    first = recent.iloc[0]
    more = f" (+{len(recent) - 1} more)" if len(recent) > 1 else ""
    return Alert(
        symbol=ctx.row["symbol"], rule_id="psi_news", date=ctx.as_of,
        headline=f"{ctx.row['symbol']} filed price-sensitive information: {str(first['title'])[:80]}{more}",
        detail=str(first.get("body", ""))[:600],
        severity="notable",
        evidence={"count": len(recent), "titles": recent["title"].astype(str).head(5).tolist()},
    )


def _record_date(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    if ctx.dividends.empty:
        return None
    within = int(p.get("within_days", 3))
    rd = pd.to_datetime(ctx.dividends["record_date"], errors="coerce").dt.date
    soon = ctx.dividends[(rd >= ctx.as_of) & (rd <= ctx.as_of + dt.timedelta(days=within))]
    if soon.empty:
        return None
    d = soon.iloc[0]
    return Alert(
        symbol=ctx.row["symbol"], rule_id="record_date", date=ctx.as_of,
        headline=f"{ctx.row['symbol']} record date on {d['record_date']}: {str(d['dividend'])[:60]}",
        detail=f"Year end {d.get('year_end', '')}; AGM {d.get('agm_date', '')}. Declared {d.get('declared_on', '')}.",
        severity="info",
        evidence={"record_date": str(d["record_date"]), "dividend": str(d["dividend"])},
    )


def _ma_cross(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    c = ctx.val("sma50_cross")
    if not c:
        return None
    want = p.get("direction", "both")
    if (want == "above" and c < 0) or (want == "below" and c > 0):
        return None
    return Alert(
        symbol=ctx.row["symbol"], rule_id="ma_cross", date=ctx.as_of,
        headline=f"{ctx.row['symbol']} crossed {'above' if c > 0 else 'below'} its 50-session average",
        detail=f"Close {ctx.val('close'):.1f}, 50-session average {ctx.val('sma_50'):.1f}, 20-session average {ctx.val('sma_20') or 0:.1f}.",
        severity="info",
        evidence={"close": ctx.val("close"), "sma_50": ctx.val("sma_50"), "direction": int(c)},
    )


def _pe(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    if ctx.fundamentals is None or pd.isna(ctx.fundamentals.get("pe")):
        return None
    pe = float(ctx.fundamentals["pe"])
    lo, hi = p.get("min"), p.get("max")
    if lo is None and hi is None:
        return None
    holds = (hi is not None and pe <= float(hi)) or (lo is not None and pe >= float(lo))
    if not holds:
        return None
    prev = ctx.fundamentals.get("prev_pe")
    if prev is not None and not pd.isna(prev):
        prev_holds = (hi is not None and float(prev) <= float(hi)) or (lo is not None and float(prev) >= float(lo))
        if prev_holds:
            return None  # state unchanged since the last snapshot
    cond = f"at or below {hi}" if hi is not None and pe <= float(hi) else f"at or above {lo}"
    return Alert(
        symbol=ctx.row["symbol"], rule_id="pe_level", date=ctx.as_of,
        headline=f"{ctx.row['symbol']} P/E is {pe:.1f}, {cond}",
        detail=f"EPS {ctx.fundamentals.get('eps')}, NAV {ctx.fundamentals.get('nav')}, as of {ctx.fundamentals.get('as_of')}.",
        severity="info",
        evidence={"pe": pe, "min": lo, "max": hi},
    )


def _illiquid(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    z = ctx.val("zero_vol_10")
    need = int(p.get("sessions", 5))
    if z is None or z < need:
        return None
    if len(ctx.recent) >= 2:
        prev = ctx.recent.iloc[-2].get("zero_vol_10")
        if prev is not None and not pd.isna(prev) and prev >= need:
            return None
    return Alert(
        symbol=ctx.row["symbol"], rule_id="illiquid", date=ctx.as_of,
        headline=f"{ctx.row['symbol']} had no trades in {int(z)} of the last 10 sessions",
        detail="Thin trading means the quoted price may not be achievable in size.",
        severity="info",
        evidence={"zero_volume_sessions": int(z)},
    )


RULES: list[Rule] = [
    Rule("big_drop", "Big drop", "Fell more than the stock's own 5th-percentile move over N sessions (or your % if set); once per episode",
         {"days": 10, "pct": None}, "ret_10 <= ret10_p05 AND coalesce(ret_10_prev, 0) > ret10_p05", _big_move(-1), "notable"),
    Rule("big_rise", "Big rise", "Rose more than the stock's own 95th-percentile move over N sessions (or your % if set); once per episode",
         {"days": 10, "pct": None}, "ret_10 >= ret10_p95 AND coalesce(ret_10_prev, 0) < ret10_p95", _big_move(+1), "notable"),
    Rule("upper_circuit", "Upper circuit", "Closed at the daily upper price limit",
         {"streak": 1}, "hit_upper", _circuit("upper"), "notable"),
    Rule("lower_circuit", "Lower circuit", "Closed at the daily lower price limit",
         {"streak": 1}, "hit_lower", _circuit("lower"), "notable"),
    Rule("volume_spike", "Volume spike", "Volume at least 3x (or the stock's 95th percentile) with a 3%+ move",
         {"ratio": 3.0, "min_move": 0.03, "use_stock_baseline": True},
         "vol_ratio_20 >= greatest(3, coalesce(vol_ratio_p95, 3)) AND abs(ret_1) >= 0.03", _volume_spike, "notable"),
    Rule("new_250d_high", "New 250-session high", "Closed above the highest high of the previous 250 sessions",
         {}, "close > high_250", _range_break("high"), "notable"),
    Rule("new_250d_low", "New 250-session low", "Closed below the lowest low of the previous 250 sessions",
         {}, "close < low_250", _range_break("low"), "notable"),
    Rule("vs_index", "Diverged from DSEX", "Moved at least 5 points more or less than the index on the day",
         {"pct": 0.05}, "abs(ret_1 - dsex_ret_1) >= 0.05", _vs_index, "notable"),
    Rule("psi_news", "Price-sensitive news", "The company filed price-sensitive information in the last 2 days",
         {"lookback_days": 2}, None, _psi_news, "notable"),
    Rule("record_date", "Record date soon", "A dividend record date falls within the next 3 days",
         {"within_days": 3}, None, _record_date, "info"),
    Rule("ma_cross", "50-session average cross", "The close crossed its 50-session moving average",
         {"direction": "both"}, "sma50_cross <> 0", _ma_cross, "info"),
    Rule("pe_level", "P/E level", "P/E at or below your maximum, or at or above your minimum (fires when it becomes true)",
         {"min": None, "max": None}, None, _pe, "info"),
    Rule("illiquid", "Thin trading", "No trades in 5 or more of the last 10 sessions (fires when it becomes true)",
         {"sessions": 5}, "zero_vol_10 >= 5", _illiquid, "info"),
]

# Rules that only make sense when the user configures them (no sensible default threshold).
OPT_IN = {"pe_level", "ma_cross", "vs_index"}

_BY_ID = {r.id: r for r in RULES}


def get_rule(rule_id: str) -> Rule:
    return _BY_ID[rule_id]
