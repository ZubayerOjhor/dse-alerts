"""Live data from the Dhaka Stock Exchange website, via the ``bdshare`` library.

Everything here is a thin, defensive wrapper: bdshare scrapes dse.com.bd (with old.dsebd.org
as fallback), and the site changes without notice. Each function returns a normalised
DataFrame with lower-case columns, or an empty frame on failure, and never raises on a
site hiccup - the caller decides whether a missing day is fatal.

Verified 2026-10-10: the live site serves daily history back to roughly mid-October 2024 only.
Older history has to come from an archive (see ``archive.py``).
"""

from __future__ import annotations

import datetime as dt
import logging
import warnings

import pandas as pd

warnings.filterwarnings("ignore", category=DeprecationWarning, module="bdshare")
log = logging.getLogger(__name__)

PRICE_COLS = ["symbol", "date", "open", "high", "low", "close", "volume"]


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


def market_status() -> str:
    from bdshare import get_market_status

    try:
        return str(get_market_status()).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("market_status failed: %s", e)
        return "unknown"


def daily_prices(start: dt.date, end: dt.date, symbol: str | None = None) -> pd.DataFrame:
    """Daily OHLCV for one symbol, or every instrument when ``symbol`` is None."""
    from bdshare import get_historical_data

    try:
        df = get_historical_data(start.isoformat(), end.isoformat(), symbol or "All Instrument")
    except Exception as e:  # noqa: BLE001
        log.warning("daily_prices(%s..%s, %s) failed: %s", start, end, symbol, e)
        return _empty(PRICE_COLS)
    if df is None or df.empty:
        return _empty(PRICE_COLS)
    df = df.reset_index()
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "symbol" not in df.columns and symbol:
        df["symbol"] = symbol
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"])
    df["volume"] = df["volume"].fillna(0).astype("int64")
    df["symbol"] = df["symbol"].astype(str).str.strip()
    return df[PRICE_COLS].drop_duplicates(["symbol", "date"], keep="last")


def live_quotes() -> pd.DataFrame:
    """Current session quotes for all instruments (ltp, ycp, change, trade, value, volume)."""
    from bdshare import get_current_trade_data

    try:
        df = get_current_trade_data()
    except Exception as e:  # noqa: BLE001
        log.warning("live_quotes failed: %s", e)
        return _empty(["symbol", "ltp", "high", "low", "close", "ycp", "change", "trade", "value", "volume"])
    df.columns = [str(c).strip().lower() for c in df.columns]
    df["symbol"] = df["symbol"].astype(str).str.strip()
    return df


def psi_news() -> pd.DataFrame:
    """Price-sensitive information feed (latest ~450 items)."""
    from bdshare import get_price_sensitive_news

    try:
        df = get_price_sensitive_news()
    except Exception as e:  # noqa: BLE001
        log.warning("psi_news failed: %s", e)
        return _empty(["symbol", "date", "kind", "title", "body"])
    df = df.rename(columns={"code": "symbol", "news": "body"})
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["kind"] = "psi"
    return df[["symbol", "date", "kind", "title", "body"]]


def dividend_declarations() -> pd.DataFrame:
    from bdshare import get_dividend_declarations

    try:
        df = get_dividend_declarations()
    except Exception as e:  # noqa: BLE001
        log.warning("dividend_declarations failed: %s", e)
        return _empty(["symbol", "declared_on", "year_end", "dividend", "agm_date", "record_date"])
    out = pd.DataFrame(
        {
            "symbol": df["symbol"].astype(str).str.strip(),
            "declared_on": pd.to_datetime(df["date"]).dt.date,
            "year_end": df["yearEnd"].astype(str),
            "dividend": df["dividend"].astype(str),
            "agm_date": pd.to_datetime(df["agmDate"], format="%d.%m.%Y", errors="coerce").dt.date,
            "record_date": pd.to_datetime(df["recordDate"], format="%d.%m.%Y", errors="coerce").dt.date,
        }
    )
    return out


def trading_codes() -> list[str]:
    """Symbols DSE currently lists as tradeable (equities and mutual funds; bonds excluded)."""
    from bdshare import get_current_trading_code

    try:
        df = get_current_trading_code()
    except Exception as e:  # noqa: BLE001
        log.warning("trading_codes failed: %s", e)
        return []
    return sorted(df["symbol"].astype(str).str.strip().unique().tolist())


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def company_details(symbol: str) -> dict | None:
    """Profile + current fundamentals for one listed company, or None when DSE has no page
    for it (mutual funds, bonds and some debentures)."""
    from bdshare import get_company_details

    try:
        d = get_company_details(symbol)
    except Exception as e:  # noqa: BLE001
        log.debug("company_details(%s): %s", symbol, e)
        return None
    if not isinstance(d, dict) or not d.get("code"):
        return None
    ly = d.get("listingYear")
    return {
        "symbol": str(d.get("code")).strip(),
        "name": d.get("name"),
        "category": d.get("category"),
        "sector": d.get("sector"),
        "instrument_type": d.get("instrumentType"),
        "listing_year": int(ly) if isinstance(ly, (int, float)) or (isinstance(ly, str) and ly.isdigit()) else None,
        "price": _num(d.get("price")),
        "pe": _num(d.get("pe")),
        "eps": _num(d.get("eps")),
        "nav": _num(d.get("nav")),
        "dividend_yield": _num(d.get("dividendYield")),
        "free_float_pct": _num(d.get("freeFloat")),
        "market_cap": _num(d.get("marketCap")),
        "paid_up_capital": _num(d.get("paidUpCapital")),
        "face_value": _num(d.get("faceValue")),
        "week_high_52": _num(d.get("weekHigh52")),
        "week_low_52": _num(d.get("weekLow52")),
    }


_SUMMARY_COLS = ["date", "dsex", "dses", "ds30", "total_trade", "total_volume", "total_value_mn", "market_cap_mn"]


def _normalise_summary(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(
        columns={
            "Date": "date", "DSEX Index": "dsex", "DSES Index": "dses", "DS30 Index": "ds30",
            "Total Trade": "total_trade", "Total Volume": "total_volume",
            "Total Value (mn)": "total_value_mn", "Total Value in Taka(mn)": "total_value_mn",
            "Total Market Cap. (mn)": "market_cap_mn", "Total Market Cap. in Taka(mn)": "market_cap_mn",
        }
    )
    df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y", errors="coerce").dt.date
    for c in _SUMMARY_COLS[1:]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.reindex(columns=_SUMMARY_COLS).dropna(subset=["date"])


def market_summary(start: dt.date | None = None, end: dt.date | None = None) -> pd.DataFrame:
    """Index levels (DSEX, DSES, DS30), turnover and market cap per session.
    Without dates: the last ~30 sessions. With dates: that range (same history floor as prices)."""
    from bdshare import get_market_info, get_market_info_more_data

    try:
        df = get_market_info_more_data(start.isoformat(), end.isoformat()) if start and end else get_market_info()
    except Exception as e:  # noqa: BLE001
        log.warning("market_summary(%s, %s) failed: %s", start, end, e)
        return _empty(_SUMMARY_COLS)
    if df is None or df.empty:
        return _empty(_SUMMARY_COLS)
    return _normalise_summary(df)
