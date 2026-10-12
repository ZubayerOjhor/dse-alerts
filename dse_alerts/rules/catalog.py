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
import re
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


def _prev_row(ctx: RuleContext) -> pd.Series | None:
    """The session before the one being evaluated, if the context has it."""
    return ctx.recent.iloc[-2] if len(ctx.recent) >= 2 else None


def _prev_session(ctx: RuleContext) -> dt.date | None:
    """The date of the session before ``as_of``, when the context has it and it is earlier."""
    prev = _prev_row(ctx)
    if prev is None:
        return None
    d = prev.get("date")
    if d is None or (not isinstance(d, (dt.date, pd.Timestamp)) and pd.isna(d)):
        return None
    d = d.date() if isinstance(d, pd.Timestamp) else d
    return d if isinstance(d, dt.date) and d < ctx.as_of else None


def _unexplained_jump(ctx: RuleContext, sessions: int) -> bool:
    """True when one of the last ``sessions`` rows moved beyond the price limit for no known
    reason: a bonus issue or rights adjustment we have no record of, so the return is not real."""
    if "limit_break" not in ctx.recent.columns:
        return False
    return bool(ctx.recent["limit_break"].tail(sessions).fillna(False).astype(bool).any())


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
        if _unexplained_jump(ctx, days):
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
        # the range is kept on adjusted prices; today's adjusted close equals the printed one
        # except on the session of a corporate action, when the factor says how to compare
        close, ref = ctx.val("close"), ctx.val("high_250" if side == "high" else "low_250")
        factor = ctx.val("adj_factor") or 1.0
        if close is None or ref is None:
            return None
        aclose = close * factor
        if (side == "high" and aclose <= ref) or (side == "low" and aclose >= ref):
            return None
        if _unexplained_jump(ctx, 1):
            return None
        # once per episode: not again while the stock stays beyond the previous session's range
        prev = _prev_row(ctx)
        if prev is not None:
            pref, pclose = prev.get("high_250" if side == "high" else "low_250"), prev.get("close")
            pf = prev.get("adj_factor")
            pf = 1.0 if pf is None or pd.isna(pf) else float(pf)
            if (pref is not None and pclose is not None and not pd.isna(pref) and not pd.isna(pclose)
                    and ((side == "high" and pclose * pf > pref) or (side == "low" and pclose * pf < pref))):
                return None
        ref_shown = ref / factor
        return Alert(
            symbol=ctx.row["symbol"], rule_id=f"new_250d_{side}", date=ctx.as_of,
            headline=f"{ctx.row['symbol']} closed at a 250-session {side}: {close:.1f}",
            detail=f"Previous 250-session {side} was {ref_shown:.1f}.",
            severity="notable",
            evidence={"close": close, "previous_extreme": ref_shown},
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


# What a filing is about, most consequential first. The headline leads with the top one.
# Whole words (or word stems, marked with a trailing *), so "land" does not match "Bangladesh"
# and "nav" does not match "Navana".
NEWS_PRIORITY = [
    ("dividend*", "dividend"), ("bonus", "dividend"), ("right*", "rights"), ("eps", "results"), ("earning*", "results"),
    ("financial*", "results"), ("profit*", "results"), ("loss*", "results"), ("nav", "results"),
    ("acqui*", "deal"), ("purchase*", "deal"), ("land", "deal"), ("agreement*", "deal"), ("contract*", "deal"),
    ("merger*", "deal"), ("agm", "meeting"), ("egm", "meeting"), ("record date", "meeting"),
    ("board", "meeting"), ("credit rating", "rating"), ("price limit", "routine"), ("bank", "routine"),
    ("update of information", "routine"), ("spot", "routine"), ("suspen*", "routine"), ("resum*", "routine"),
]
# filings that change what a holder owns or knows; everything else (meetings, routine notices)
# is recorded as "info"
NOTABLE_NEWS = {"dividend", "rights", "results", "deal", "rating"}
_NEWS_PATTERNS = [re.compile(r"\b" + re.escape(n[:-1]) + r"\w*" if n.endswith("*") else r"\b" + re.escape(n) + r"\b")
                  for n, _kind in NEWS_PRIORITY]


def news_rank(title: str) -> int:
    t = str(title).lower()
    for i, pat in enumerate(_NEWS_PATTERNS):
        if pat.search(t):
            return i
    return len(NEWS_PRIORITY) - 1     # unknown: just above the routine notices


def _psi_news(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    """Filings since the previous session, each alerted once; the headline leads with the
    filing that matters most (dividends and results before routine notices)."""
    if ctx.news.empty:
        return None
    dates = pd.to_datetime(ctx.news["date"], errors="coerce")
    prev = _prev_session(ctx)
    since = pd.Timestamp(prev) if prev else pd.Timestamp(ctx.as_of - dt.timedelta(days=int(p.get("lookback_days", 2))))
    recent = ctx.news[(dates > since) & (dates <= pd.Timestamp(ctx.as_of))] if prev else ctx.news[dates >= since]
    if recent.empty:
        return None
    recent = recent.assign(_rank=recent["title"].map(news_rank)).sort_values(["_rank", "date"], ascending=[True, False])
    first = recent.iloc[0]
    # the title repeats the trading code as a prefix; the headline already names the stock
    title = str(first["title"])
    if title.upper().startswith(ctx.row["symbol"].upper() + ":"):
        title = title.split(":", 1)[1].strip()
    more = f" (+{len(recent) - 1} more)" if len(recent) > 1 else ""
    rank = news_rank(first["title"])
    kind = NEWS_PRIORITY[rank][1] if rank < len(NEWS_PRIORITY) else "routine"
    return Alert(
        symbol=ctx.row["symbol"], rule_id="psi_news", date=ctx.as_of,
        headline=f"{ctx.row['symbol']} filed price-sensitive information: {title[:80]}{more}",
        detail=str(first.get("body", ""))[:600],
        severity="notable" if kind in NOTABLE_NEWS else "info",
        evidence={"count": len(recent), "titles": recent["title"].astype(str).head(5).tolist()},
    )


def _record_date(ctx: RuleContext, p: dict[str, Any]) -> Alert | None:
    if ctx.dividends.empty:
        return None
    within = int(p.get("within_days", 3))
    # compare as Timestamps: the column may arrive as datetime64 or as text, and pandas refuses
    # to compare datetime64 with a plain date
    rd = pd.to_datetime(ctx.dividends["record_date"], errors="coerce")
    soon = ctx.dividends[(rd >= pd.Timestamp(ctx.as_of)) & (rd <= pd.Timestamp(ctx.as_of + dt.timedelta(days=within)))]
    prev = _prev_session(ctx)
    if prev is not None:   # once: only record dates that were not already within reach last session
        soon = soon[pd.to_datetime(soon["record_date"], errors="coerce") > pd.Timestamp(prev + dt.timedelta(days=within))]
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
         {}, "aclose > high_250 AND NOT coalesce(prev_aclose > high_250_prev, false)", _range_break("high"), "notable"),
    Rule("new_250d_low", "New 250-session low", "Closed below the lowest low of the previous 250 sessions",
         {}, "aclose < low_250 AND NOT coalesce(prev_aclose < low_250_prev, false)", _range_break("low"), "notable"),
    Rule("vs_index", "Diverged from DSEX", "Moved at least 5 points more or less than the index on the day",
         {"pct": 0.05}, "abs(ret_1 - dsex_ret_1) >= 0.05", _vs_index, "notable"),
    Rule("psi_news", "Price-sensitive news", "The company filed price-sensitive information since the previous session",
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
