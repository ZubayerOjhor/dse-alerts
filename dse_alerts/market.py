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


def latest_rows(store: MarketStore, symbols: list[str], spark_sessions: int = 60) -> pd.DataFrame:
    """Latest session per symbol with name/category/sector and the last N closes (for sparklines)."""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "date", "close", "prev_close", "ret_1", "ret_5", "ret_20", "vol_ratio_20", "hit_upper",
                                     "hit_lower", "upper_streak", "lower_streak", "name", "category", "sector", "closes"])
    ph = ",".join("?" * len(symbols))
    with store.connect(read_only=True) as con:
        df = con.execute(
            f"""
            SELECT f.symbol, f.date, f.close, f.prev_close, f.ret_1, f.ret_5, f.ret_20, f.vol_ratio_20, f.hit_upper, f.hit_lower,
                   f.upper_streak, f.lower_streak, i.name, i.category, i.sector,
                   list(f.close) OVER (PARTITION BY f.symbol ORDER BY f.date ROWS BETWEEN {spark_sessions - 1} PRECEDING AND CURRENT ROW) AS closes
            FROM features f JOIN instruments i USING (symbol)
            WHERE f.symbol IN ({ph})
            QUALIFY row_number() OVER (PARTITION BY f.symbol ORDER BY f.date DESC) = 1
            ORDER BY f.symbol
            """,
            symbols,
        ).df()
    df["closes"] = df["closes"].apply(lambda v: list(v) if v is not None else [])
    df["spark"] = df["closes"].apply(spark_path)
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
            "SELECT date, open, high, low, close, volume, sma_20, sma_50, prev_close FROM features WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            [symbol, n],
        ).df()
    df = df.iloc[::-1]
    candles, volume, s20, s50 = [], [], [], []
    for r in df.itertuples():
        t = _d(r.date)
        c = _f(r.close)
        if c is None or c <= 0:
            continue
        # sessions with no trades come through as open/high/low = 0; draw them as a flat candle at the close
        o = _f(r.open) if _f(r.open) else c
        hi = _f(r.high) if _f(r.high) else c
        lo = _f(r.low) if _f(r.low) else c
        hi, lo = max(hi, o, c), min(lo, o, c)
        candles.append({"time": t, "open": o, "high": hi, "low": lo, "close": c})
        volume.append({"time": t, "value": _f(r.volume) or 0.0, "up": c >= (_f(r.prev_close) or o)})
        if _f(r.sma_20) is not None:
            s20.append({"time": t, "value": round(float(r.sma_20), 2)})
        if _f(r.sma_50) is not None:
            s50.append({"time": t, "value": round(float(r.sma_50), 2)})
    return {"symbol": symbol, "range": rng, "candles": candles, "volume": volume, "sma20": s20, "sma50": s50}


def relative_series(store: MarketStore, symbol: str, sessions: int = 250) -> dict:
    """Symbol and DSEX indexed to 100 at the first common session."""
    with store.connect(read_only=True) as con:
        df = con.execute(
            """
            SELECT f.date, f.close, i.dsex FROM features f JOIN index_daily i USING (date)
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
