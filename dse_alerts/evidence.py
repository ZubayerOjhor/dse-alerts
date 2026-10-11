"""'What is true right now' for one symbol: today's numbers set against the stock's own
history. Every item is a description with the numbers that produced it; none is a
forecast or a recommendation.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from dse_alerts.data.store import MarketStore


def _f(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)


def _pct(x, d=1):
    return "–" if x is None else f"{100 * x:+.{d}f}%"


def _num(x, d=1):
    return "–" if x is None else f"{x:,.{d}f}"


def _date(x) -> str:
    try:
        return "–" if x is None or pd.isna(x) else pd.Timestamp(x).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(x)


def sector_pe_median(store: MarketStore, sector: str | None) -> float | None:
    if not sector:
        return None
    with store.connect(read_only=True) as con:
        row = con.execute(
            """
            SELECT median(f.pe) FROM (
                SELECT symbol, pe, row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) AS rn FROM fundamentals
            ) f JOIN instruments i USING (symbol)
            WHERE f.rn = 1 AND i.sector = ? AND i.tradeable AND f.pe > 0 AND f.pe < 200
            """,
            [sector],
        ).fetchone()
    return _f(row[0]) if row else None


def symbol_snapshot(store: MarketStore, symbol: str, sessions: int = 300) -> pd.DataFrame:
    with store.connect(read_only=True) as con:
        df = con.execute(
            """
            SELECT f.date, f.open, f.high, f.low, f.close, f.volume, f.ret_1, f.ret_20, f.vol_ratio_20, f.sma_20, f.sma_50,
                   f.high_250, f.low_250, f.hit_upper, f.hit_lower, f.zero_vol_10, f.dsex_ret_1
            FROM features f WHERE f.symbol = ? ORDER BY f.date DESC LIMIT ?
            """,
            [symbol, sessions],
        ).df()
    return df.iloc[::-1].reset_index(drop=True)


def index_return(store: MarketStore, start: dt.date, end: dt.date) -> float | None:
    with store.connect(read_only=True) as con:
        rows = con.execute("SELECT dsex FROM index_daily WHERE date IN (?, ?) ORDER BY date", [start, end]).fetchall()
    if len(rows) == 2 and rows[0][0]:
        return float(rows[1][0] / rows[0][0] - 1)
    return None


def _trend(close, sma20, sma50) -> str:
    """One of above | below | between | na; the UI translates it (``trend.<key>``)."""
    if close is None or sma20 is None or sma50 is None:
        return "na"
    if close > sma20 and close > sma50:
        return "above"
    if close < sma20 and close < sma50:
        return "below"
    return "between"


def fact_text(fact: dict, lang: str) -> dict:
    """Resolve one fact's catalog keys into strings for ``lang`` (used by the symbol page)."""
    from dse_alerts.i18n import t

    args = dict(fact.get("args") or {})
    for k in ("where", "rel"):
        if k in args and isinstance(args[k], str) and args[k].startswith("fact."):
            args[k] = t(lang, args[k], **(args.pop(k + "_args", None) or {}))
    note = t(lang, fact["note"], **args) if fact.get("note") else fact.get("text", "")
    if fact.get("note2"):
        note += " " + t(lang, fact["note2"], **(fact.get("args2") or {}))
    value = t(lang, fact["value_key"], **(fact.get("value_args") or {})) if fact.get("value_key") else fact.get("value")
    return {"label": t(lang, fact["label"]), "value": value, "tone": fact.get("tone", ""), "note": note}


def evidence(store: MarketStore, symbol: str, stats: pd.Series | None, fund: pd.Series | None,
             sector: str | None, news: pd.DataFrame, dividends: pd.DataFrame) -> dict:
    hist = symbol_snapshot(store, symbol)
    if hist.empty:
        return {"facts": [], "ret_20": None, "ret_60": None, "ret_250": None, "dsex_20": None, "range_pos": None,
                "high_250": None, "low_250": None, "vol_ratio_p95": None, "sector_pe": None, "trend": "na"}
    last = hist.iloc[-1]
    close = _f(last["close"])

    def ret(n):
        return _f(close / hist["close"].iloc[-1 - n] - 1) if len(hist) > n and hist["close"].iloc[-1 - n] else None

    r20, r60, r250 = ret(20), ret(60), ret(250)
    dsex_20 = index_return(store, hist["date"].iloc[-21].date(), hist["date"].iloc[-1].date()) if len(hist) > 20 else None
    sma20, sma50 = _f(last["sma_20"]), _f(last["sma_50"])
    hi, lo = _f(last["high_250"]), _f(last["low_250"])
    range_pos = (close - lo) / (hi - lo) if hi is not None and lo is not None and hi > lo else None
    st = (lambda k: _f(stats[k]) if stats is not None and k in stats else None)
    sd, p95v = st("ret1_sd"), st("vol_ratio_p95")
    ret1 = _f(last["ret_1"])
    z = ret1 / sd if ret1 is not None and sd else None
    last60 = hist.tail(60)
    ups, dns = int(last60["hit_upper"].fillna(False).astype(bool).sum()), int(last60["hit_lower"].fillna(False).astype(bool).sum())
    zero60 = int((last60["volume"] == 0).sum())
    pe = _f(fund["pe"]) if fund is not None and "pe" in fund else None
    sector_pe = sector_pe_median(store, sector)

    trend = _trend(close, sma20, sma50)

    # Each fact is language-neutral: ``label`` and ``note`` are catalog keys (see i18n/en.json),
    # ``args`` fills the note's placeholders, and ``value`` is either a formatted number or,
    # when ``value_key`` is set, a catalog key. The templates translate at render time.
    facts = []
    facts.append({"id": "trend", "label": "fact.trend", "value": None, "value_key": "trend." + trend,
                  "tone": "up" if trend == "above" else "down" if trend == "below" else "",
                  "note": "fact.trend_note", "args": {"close": _num(close), "sma20": _num(sma20), "sma50": _num(sma50)}})
    p05, p95 = st("ret20_p05"), st("ret20_p95")
    if r20 is not None and p05 is not None and p95 is not None:
        where = "fact.where_above" if r20 > p95 else "fact.where_below" if r20 < p05 else "fact.where_inside"
        facts.append({"id": "ret20", "label": "fact.ret20", "value": _pct(r20), "tone": "up" if r20 > 0 else "down" if r20 < 0 else "",
                      "note": "fact.ret20_note", "args": {"where": where, "p05": _pct(p05), "p95": _pct(p95), "dsex": _pct(dsex_20)}})
    if z is not None:
        facts.append({"id": "today", "label": "fact.today", "value": _pct(ret1, 2), "tone": "up" if ret1 > 0 else "down" if ret1 < 0 else "",
                      "note": "fact.today_note", "args": {"z": f"{abs(z):.1f}", "sd": _pct(sd, 2)}})
    vr = _f(last["vol_ratio_20"])
    if vr is not None:
        facts.append({"id": "volume", "label": "fact.volume", "value": f"{vr:.1f}x", "tone": "up" if p95v and vr >= p95v else "",
                      "note": "fact.volume_note_p95" if p95v else "fact.volume_note", "args": {"p95": _num(p95v)}})
    if range_pos is not None:
        facts.append({"id": "range", "label": "fact.range", "value": f"{100 * range_pos:.0f}%", "tone": "",
                      "note": "fact.range_note", "args": {"low": _num(lo), "high": _num(hi)}})
    facts.append({"id": "circuit", "label": "fact.circuit", "value": None, "value_key": "fact.circuit_value", "value_args": {"ups": ups, "dns": dns},
                  "tone": "down" if dns >= 3 else "up" if ups >= 3 else "",
                  "note": "fact.circuit_note", "args": {"zero": zero60},
                  "note2": "fact.circuit_note_avg" if st("upper_hits_per_250") is not None else None,
                  "args2": {"up": _num(st("upper_hits_per_250")), "down": _num(st("lower_hits_per_250"))}})
    if pe is not None:
        rel = ("fact.rel_below" if sector_pe and pe < sector_pe else "fact.rel_above" if sector_pe and pe > sector_pe else "fact.rel_at") if sector_pe else "fact.rel_none"
        pnav = _num(_f(fund.get("price")) / _f(fund.get("nav")), 2) if _f(fund.get("nav")) else "–"
        facts.append({"id": "valuation", "label": "fact.valuation", "value": f"P/E {pe:.1f}", "tone": "",
                      "note": "fact.valuation_note", "args": {"rel": rel, "rel_args": {"pe": _num(sector_pe)}, "dy": _num(_f(fund.get("dividend_yield")), 2), "pnav": pnav}})
    if news is not None and not news.empty:
        n = news.iloc[0]
        facts.append({"id": "filing", "label": "fact.filing", "value": _date(n["date"]), "tone": "", "note": None, "text": str(n["title"])[:120]})
    if dividends is not None and not dividends.empty:
        d = dividends.iloc[0]
        facts.append({"id": "dividend", "label": "fact.dividend", "value": str(d["dividend"])[:40], "tone": "",
                      "note": "fact.dividend_note", "args": {"declared": _date(d.get("declared_on")), "record": _date(d.get("record_date")), "agm": _date(d.get("agm_date"))}})

    return {"facts": facts, "ret_20": r20, "ret_60": r60, "ret_250": r250, "dsex_20": dsex_20, "range_pos": range_pos,
            "high_250": hi, "low_250": lo, "vol_ratio_p95": p95v, "sector_pe": sector_pe, "trend": trend, "pe": pe}


def target_rows(store: MarketStore, symbols: list[str]) -> list[dict]:
    """Compact evidence per symbol for the targets table."""
    out = []
    if not symbols:
        return out
    ph = ",".join("?" * len(symbols))
    with store.connect(read_only=True) as con:
        base = con.execute(
            f"""
            SELECT f.symbol, i.name, i.category, i.sector, f.close, f.ret_20, f.vol_ratio_20, f.sma_20, f.sma_50, f.high_250, f.low_250,
                   f.close / lag(f.close, 60) OVER (PARTITION BY f.symbol ORDER BY f.date) - 1 AS ret_60,
                   f.close / lag(f.close, 250) OVER (PARTITION BY f.symbol ORDER BY f.date) - 1 AS ret_250,
                   list(f.close) OVER (PARTITION BY f.symbol ORDER BY f.date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS closes
            FROM features f JOIN instruments i USING (symbol) WHERE f.symbol IN ({ph})
            QUALIFY row_number() OVER (PARTITION BY f.symbol ORDER BY f.date DESC) = 1
            """,
            symbols,
        ).df()
        fund = con.execute(
            f"SELECT symbol, pe, dividend_yield FROM fundamentals WHERE symbol IN ({ph}) QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1",
            symbols,
        ).df()
        news = con.execute(
            f"SELECT symbol, date, title FROM news WHERE symbol IN ({ph}) QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1",
            symbols,
        ).df()
    fund_m = fund.set_index("symbol") if not fund.empty else None
    news_m = news.set_index("symbol") if not news.empty else None
    sector_cache: dict[str, float | None] = {}
    for r in base.itertuples():
        close, sma20, sma50, hi, lo = _f(r.close), _f(r.sma_20), _f(r.sma_50), _f(r.high_250), _f(r.low_250)
        trend = _trend(close, sma20, sma50)
        if r.sector not in sector_cache:
            sector_cache[r.sector] = sector_pe_median(store, r.sector)
        out.append({
            "symbol": r.symbol, "name": r.name, "category": r.category, "sector": r.sector, "close": close,
            "ret_20": _f(r.ret_20), "ret_60": _f(r.ret_60), "ret_250": _f(r.ret_250), "vol_ratio_20": _f(r.vol_ratio_20),
            "high_250": hi, "low_250": lo, "range_pos": (close - lo) / (hi - lo) if hi and lo and hi > lo else None, "trend": trend,
            "pe": _f(fund_m.loc[r.symbol, "pe"]) if fund_m is not None and r.symbol in fund_m.index else None,
            "dividend_yield": _f(fund_m.loc[r.symbol, "dividend_yield"]) if fund_m is not None and r.symbol in fund_m.index else None,
            "sector_pe": sector_cache[r.sector],
            "last_news_date": _date(news_m.loc[r.symbol, "date"]) if news_m is not None and r.symbol in news_m.index else None,
            "last_news_title": str(news_m.loc[r.symbol, "title"]) if news_m is not None and r.symbol in news_m.index else None,
            "closes": list(r.closes) if r.closes is not None else [],
        })
    return out
