"""Read helpers shared by the web UI and the email renderer: latest rows per symbol,
index series, OHLC series for charts, sparkline paths."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from dse_alerts.data.store import MarketStore

RANGE_SESSIONS = {"1m": 22, "3m": 65, "6m": 130, "1y": 250, "3y": 750, "5y": 1250, "all": 100000}


def _f(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)


def _d(v) -> str:
    return pd.Timestamp(v).strftime("%Y-%m-%d")


def spark_path(values: list[float], w: int = 110, h: int = 30) -> str:
    vals = [v for v in values if v is not None and not pd.isna(v)]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    return "M" + " L".join(f"{i * step:.1f},{h - (v - lo) / span * (h - 4) - 2:.1f}" for i, v in enumerate(vals))


def spark_dir(values: list[float]) -> str:
    """The colour of a sparkline: the sign of the change over the sessions it actually draws."""
    vals = [v for v in values if v is not None and not pd.isna(v)]
    if len(vals) < 2:
        return ""
    return "up" if vals[-1] > vals[0] else "down" if vals[-1] < vals[0] else ""


def scaled_closes(closes, factor) -> list[float]:
    """Adjusted closes put back on today's printed scale, so the line ends at the printed close
    and a bonus issue is not a cliff."""
    f = factor if factor is not None and not pd.isna(factor) and factor else 1.0
    return [float(c) / f for c in (list(closes) if closes is not None else [])]


def latest_rows(store: MarketStore, symbols: list[str], spark_sessions: int = 60) -> pd.DataFrame:
    """Latest session per symbol with name/category/sector and the last N closes (for sparklines)."""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "date", "close", "prev_close", "ret_1", "ret_5", "ret_20", "vol_ratio_20", "hit_upper",
                                     "hit_lower", "upper_streak", "lower_streak", "name", "category", "sector", "closes", "spark", "spark_dir"])
    ph = ",".join("?" * len(symbols))
    with store.connect(read_only=True) as con:
        df = con.execute(
            f"""
            SELECT f.symbol, f.date, f.close, f.prev_close, f.ret_1, f.ret_5, f.ret_20, f.vol_ratio_20, f.hit_upper, f.hit_lower,
                   f.upper_streak, f.lower_streak, f.adj_factor, i.name, i.category, i.sector,
                   list(f.aclose) OVER (PARTITION BY f.symbol ORDER BY f.date ROWS BETWEEN {spark_sessions - 1} PRECEDING AND CURRENT ROW) AS closes
            FROM features f JOIN instruments i USING (symbol)
            WHERE f.symbol IN ({ph})
            QUALIFY row_number() OVER (PARTITION BY f.symbol ORDER BY f.date DESC) = 1
            ORDER BY f.symbol
            """,
            symbols,
        ).df()
    df["closes"] = [scaled_closes(v, f) for v, f in zip(df["closes"], df["adj_factor"])]
    df["spark"] = df["closes"].apply(spark_path)
    df["spark_dir"] = df["closes"].apply(spark_dir)
    return df


def index_series(store: MarketStore, sessions: int = 130) -> list[dict]:
    with store.connect(read_only=True) as con:
        df = con.execute("SELECT date, dsex FROM index_daily WHERE dsex IS NOT NULL ORDER BY date DESC LIMIT ?", [sessions]).df()
    df = df.iloc[::-1]
    return [{"time": _d(r.date), "value": round(float(r.dsex), 2)} for r in df.itertuples()]


def index_last(store: MarketStore) -> dict:
    with store.connect(read_only=True) as con:
        df = con.execute("SELECT date, dsex FROM index_daily WHERE dsex IS NOT NULL ORDER BY date DESC LIMIT 21").df()
    if df.empty:
        return {"date": None, "close": None, "ret_1": None, "ret_20": None}
    close = float(df["dsex"].iloc[0])
    r1 = close / float(df["dsex"].iloc[1]) - 1 if len(df) > 1 else None
    r20 = close / float(df["dsex"].iloc[-1]) - 1 if len(df) > 20 else None
    return {"date": df["date"].iloc[0], "close": close, "ret_1": r1, "ret_20": r20}


def ohlc(store: MarketStore, symbol: str, rng: str = "1y") -> dict:
    n = RANGE_SESSIONS.get(rng, 250)
    with store.connect(read_only=True) as con:
        df = con.execute(
            "SELECT date, open, high, low, close, volume, sma_20, sma_50, prev_close, adj_factor, ex_session FROM features WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            [symbol, n],
        ).df()
    df = df.iloc[::-1]
    candles, volume, s20, s50, markers = [], [], [], [], []
    prev_k = None
    for r in df.itertuples():
        t = _d(r.date)
        c = _f(r.close)
        if c is None or c <= 0:
            continue
        # the first session after a record date: when the factor steps, bonus shares explain the step
        k_now = _f(r.adj_factor) or 1.0
        if bool(r.ex_session) and prev_k:
            bonus = round(100 * (k_now / prev_k - 1))
            markers.append({"time": t, "bonus": bonus if bonus > 0 else None})
        prev_k = k_now
        # sessions with no trades come through as open/high/low = 0; draw them as a flat candle at the close
        o = _f(r.open) if _f(r.open) else c
        hi = _f(r.high) if _f(r.high) else c
        lo = _f(r.low) if _f(r.low) else c
        hi, lo = max(hi, o, c), min(lo, o, c)
        candles.append({"time": t, "open": o, "high": hi, "low": lo, "close": c})
        volume.append({"time": t, "value": _f(r.volume) or 0.0, "up": c >= (_f(r.prev_close) or o)})
        # averages are kept on adjusted prices; divide by the session's factor to put them on
        # the same printed scale as that session's candle
        k = _f(r.adj_factor) or 1.0
        if _f(r.sma_20) is not None:
            s20.append({"time": t, "value": round(float(r.sma_20) / k, 2)})
        if _f(r.sma_50) is not None:
            s50.append({"time": t, "value": round(float(r.sma_50) / k, 2)})
    return {"symbol": symbol, "range": rng, "candles": candles, "volume": volume, "sma20": s20, "sma50": s50, "markers": markers}


def relative_series(store: MarketStore, symbol: str, sessions: int = 250) -> dict:
    """Symbol and DSEX indexed to 100 at the first common session. The symbol's line uses the
    adjusted close, so it ends where the 250-session move on the page says it should."""
    with store.connect(read_only=True) as con:
        df = con.execute(
            """
            SELECT f.date, f.aclose AS close, i.dsex FROM features f JOIN index_daily i USING (date)
            WHERE f.symbol = ? AND i.dsex IS NOT NULL ORDER BY f.date DESC LIMIT ?
            """,
            [symbol, sessions],
        ).df()
    df = df.iloc[::-1]
    if df.empty:
        return {"series": [], "options": {}}
    c0, i0 = float(df["close"].iloc[0]), float(df["dsex"].iloc[0])
    return {
        "series": [
            {"name": symbol, "data": [{"time": _d(r.date), "value": round(100 * float(r.close) / c0, 2)} for r in df.itertuples()], "last": True},
            {"name": "DSEX", "data": [{"time": _d(r.date), "value": round(100 * float(r.dsex) / i0, 2)} for r in df.itertuples()], "last": True},
        ],
        "options": {},
    }


def value_series(hist: pd.DataFrame) -> dict:
    if hist.empty:
        return {"series": [], "options": {}}
    return {
        "series": [
            {"name": "Book value", "data": [{"time": _d(r.date), "value": round(float(r.value), 0)} for r in hist.itertuples()], "area": True, "digits": 0},
            {"name": "Cost", "data": [{"time": _d(r.date), "value": round(float(r.cost), 0)} for r in hist.itertuples()], "dashed": True, "color": "--muted", "digits": 0},
        ],
        "options": {},
    }


def latest_session_date(store: MarketStore) -> dt.date | None:
    with store.connect(read_only=True) as con:
        row = con.execute("SELECT max(date) FROM features").fetchone()
    return row[0] if row and row[0] else None


# ---- market-wide views for the overview and the finder ------------------------------------

def _recs(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    out = df.astype(object).where(pd.notna(df), None).to_dict("records")
    for r in out:
        for k, v in r.items():
            if isinstance(v, pd.Timestamp):
                r[k] = v.date()
    return out


_TRADED = "f.date = (SELECT max(date) FROM features) AND coalesce(i.tradeable, true) AND f.volume > 0 AND f.close > 0"


def sector_board(store: MarketStore) -> list[dict]:
    """One row per sector for the latest session: how many stocks traded, the median move,
    how many rose and fell, and the median 20-session move. Only stocks that traded count."""
    with store.connect(read_only=True) as con:
        df = con.execute(
            f"""
            SELECT coalesce(i.sector, 'Other') AS sector, count(*) AS n,
                   median(f.ret_1) AS ret_1, median(f.ret_20) AS ret_20,
                   sum(CASE WHEN f.ret_1 > 0 THEN 1 ELSE 0 END) AS up,
                   sum(CASE WHEN f.ret_1 < 0 THEN 1 ELSE 0 END) AS down
            FROM features f JOIN instruments i USING (symbol)
            WHERE {_TRADED}
            GROUP BY 1 HAVING count(*) >= 2
            ORDER BY ret_1 DESC, n DESC
            """
        ).df()
    return _recs(df)


def top_movers(store: MarketStore, n: int = 6) -> dict[str, list[dict]]:
    """The session's biggest risers and fallers among stocks that actually traded."""
    with store.connect(read_only=True) as con:
        base = f"""
            SELECT f.symbol, i.name, i.sector, i.category, f.close, f.ret_1, f.volume, f.vol_ratio_20
            FROM features f JOIN instruments i USING (symbol)
            WHERE {_TRADED} AND f.ret_1 IS NOT NULL
        """
        up = con.execute(base + " ORDER BY f.ret_1 DESC, f.volume DESC LIMIT ?", [n]).df()
        down = con.execute(base + " ORDER BY f.ret_1 ASC, f.volume DESC LIMIT ?", [n]).df()
    return {"up": _recs(up), "down": _recs(down)}


def upcoming_record_dates(store: MarketStore, symbols: list[str] | None = None, days: int = 45,
                          today: dt.date | None = None) -> list[dict]:
    """Dividend record dates from today onward, soonest first; optionally only for ``symbols``."""
    today = today or dt.date.today()
    params: list = [today, today + dt.timedelta(days=days)]
    where = "d.record_date >= ? AND d.record_date <= ?"
    if symbols is not None:
        if not symbols:
            return []
        where += f" AND d.symbol IN ({','.join('?' * len(symbols))})"
        params += list(symbols)
    with store.connect(read_only=True) as con:
        df = con.execute(
            f"""
            SELECT d.symbol, i.name, d.dividend, d.record_date, d.agm_date, d.declared_on
            FROM dividends d LEFT JOIN instruments i USING (symbol)
            WHERE {where}
            ORDER BY d.record_date, d.symbol
            """,
            params,
        ).df()
    return _recs(df)


# column -> (SQL expression, default direction). P/E sorts only positive values; a loss-maker's
# 0 or negative P/E goes last in either direction instead of looking like the cheapest stock.
SCREEN_SORTS = {"symbol": ("f.symbol", "asc"), "sector": ("i.sector", "asc"), "category": ("i.category", "asc"),
                "close": ("f.close", "desc"), "ret_1": ("f.ret_1", "desc"), "ret_20": ("f.ret_20", "desc"),
                "volume": ("f.vol_ratio_20", "desc"), "range": ("range_pos", "asc"),
                "pe": ("CASE WHEN fu.pe > 0 THEN fu.pe END", "asc"), "dy": ("fu.dividend_yield", "desc")}


def screen(store: MarketStore, sector: str | None = None, category: str | None = None, pe_min: float | None = None,
           pe_max: float | None = None, ret20_min: float | None = None, ret20_max: float | None = None,
           near: str | None = None, vol_min: float | None = None, price_max: float | None = None,
           sort: str = "ret_1", direction: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """The stock finder: every stock that traded in the latest session, filtered by simple
    conditions. ``ret20_*`` are fractions (0.1 = 10%); ``near`` is 'low' or 'high' for the
    bottom / top 15% of the 52-week range. The whole filtered set is sorted in SQL before
    ``limit``/``offset`` cut a page, so the first row is the true extreme. Returns
    ``{"rows": [...], "total": n}`` where ``total`` counts every match."""
    where, params = [_TRADED], []
    if sector:
        where.append("i.sector = ?"); params.append(sector)
    if category:
        where.append("i.category = ?"); params.append(category)
    if pe_min is not None:
        where.append("fu.pe >= ?"); params.append(pe_min)
    if pe_max is not None:
        where.append("fu.pe <= ? AND fu.pe > 0"); params.append(pe_max)
    if ret20_min is not None:
        where.append("f.ret_20 >= ?"); params.append(ret20_min)
    if ret20_max is not None:
        where.append("f.ret_20 <= ?"); params.append(ret20_max)
    if vol_min is not None:
        where.append("f.vol_ratio_20 >= ?"); params.append(vol_min)
    if price_max is not None:
        where.append("f.close <= ?"); params.append(price_max)
    if near == "low":
        where.append("f.high_250 > f.low_250 AND (f.close - f.low_250) / (f.high_250 - f.low_250) <= 0.15")
    elif near == "high":
        where.append("f.high_250 > f.low_250 AND (f.close - f.low_250) / (f.high_250 - f.low_250) >= 0.85")
    expr, default_dir = SCREEN_SORTS.get(sort, SCREEN_SORTS["ret_1"])
    direction = direction if direction in ("asc", "desc") else default_dir
    fu_cte = """
            WITH fu AS (
                SELECT symbol, pe, dividend_yield, market_cap FROM fundamentals
                QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
            )"""
    with store.connect(read_only=True) as con:
        total = con.execute(
            f"""{fu_cte}
            SELECT count(*) FROM features f JOIN instruments i USING (symbol) LEFT JOIN fu USING (symbol)
            WHERE {" AND ".join(where)}""", params).fetchone()[0]
        df = con.execute(
            f"""{fu_cte}
            SELECT f.symbol, i.name, i.sector, i.category, f.close, f.ret_1, f.ret_20, f.vol_ratio_20, f.volume,
                   fu.pe, fu.dividend_yield, fu.market_cap,
                   CASE WHEN f.high_250 > f.low_250 THEN (f.close - f.low_250) / (f.high_250 - f.low_250) END AS range_pos,
                   f.hit_upper, f.hit_lower
            FROM features f JOIN instruments i USING (symbol) LEFT JOIN fu USING (symbol)
            WHERE {" AND ".join(where)}
            ORDER BY {expr} {direction.upper()} NULLS LAST, f.symbol
            LIMIT {max(1, int(limit))} OFFSET {max(0, int(offset))}
            """,
            params,
        ).df()
    return {"rows": _recs(df), "total": int(total)}


def sectors(store: MarketStore) -> list[str]:
    with store.connect(read_only=True) as con:
        rows = con.execute("SELECT DISTINCT sector FROM instruments WHERE sector IS NOT NULL AND coalesce(tradeable, true) ORDER BY 1").fetchall()
    return [r[0] for r in rows]
