import datetime as dt

import pandas as pd

from dse_alerts.data.store import MarketStore


def _rows(close, source_quality=None):
    df = pd.DataFrame(
        {
            "symbol": ["GP", "GP"],
            "date": [dt.date(2025, 1, 1), dt.date(2025, 1, 2)],
            "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
            "close": [close, close], "volume": [10, 20],
        }
    )
    if source_quality:
        df["date_quality"] = source_quality
    return df


def test_live_overrides_archive_but_not_vice_versa(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")

    assert store.upsert_prices(_rows(5.0, "approx"), source="archive") == 2
    assert store.upsert_prices(_rows(7.0), source="live") == 2          # replaces both rows
    assert store.upsert_prices(_rows(9.0, "approx"), source="archive") == 0  # ignored

    df = store.prices(["GP"])
    assert df["close"].tolist() == [7.0, 7.0]
    assert df["source"].unique().tolist() == ["live"]
    assert store.last_date("live") == dt.date(2025, 1, 2)
    assert store.summary()["rows"] == 2


def test_instruments_and_fundamentals_roundtrip(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    store.upsert_prices(_rows(5.0), source="live")
    store.refresh_instruments()
    store.upsert_instrument_details(pd.DataFrame([{
        "symbol": "GP", "name": "Grameenphone", "category": "A", "sector": "Telecom",
        "instrument_type": "Equity", "listing_year": 2009, "tradeable": True,
    }]))
    inst = store.instruments()
    assert inst.loc[0, "name"] == "Grameenphone" and inst.loc[0, "first_date"] == pd.Timestamp("2025-01-01")

    n = store.upsert_fundamentals(pd.DataFrame([{
        "symbol": "GP", "as_of": dt.date(2025, 1, 2), "price": 240.0, "pe": 11.0, "eps": 21.9, "nav": 41.5,
        "dividend_yield": 8.3, "free_float_pct": 10.0, "market_cap": 3.2e11, "paid_up_capital": 1.35e10,
        "face_value": 10.0, "week_high_52": 296.0, "week_low_52": 236.0,
    }]))
    assert n == 1
