import datetime as dt

import pandas as pd

from dse_alerts import engine
from dse_alerts.appdb import AppDB
from dse_alerts.data.store import MarketStore


def _prices(symbol: str, closes: list[float], start=dt.date(2025, 1, 1)) -> pd.DataFrame:
    dates = []
    d = start
    while len(dates) < len(closes):
        if d.weekday() not in (4, 5):   # DSE closes Fri/Sat
            dates.append(d)
        d += dt.timedelta(days=1)
    return pd.DataFrame({
        "symbol": symbol, "date": dates, "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes, "volume": [1000] * len(closes),
    })


def test_end_to_end_alert_run(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    flat = [100.0] * 80
    crash = flat + [100 * (1 - 0.099) ** k for k in range(1, 4)]       # three sessions at the 10% lower circuit
    store.upsert_prices(_prices("AAA", crash), source="live")
    store.upsert_prices(_prices("BBB", flat + [100.0] * 3), source="live")
    store.refresh_instruments()
    store.upsert_instrument_details(pd.DataFrame([
        {"symbol": "AAA", "name": "A", "category": "A", "sector": "Bank", "instrument_type": "Equity", "listing_year": 2000, "tradeable": True},
        {"symbol": "BBB", "name": "B", "category": "A", "sector": "Bank", "instrument_type": "Equity", "listing_year": 2000, "tradeable": True},
    ]))
    from dse_alerts.analysis import features
    features.build(store)

    db = AppDB(tmp_path / "app.sqlite")
    uid = db.ensure_user("u@example.com")
    wl = db.ensure_watchlist(uid)
    db.add_item(wl, "AAA", {"big_drop": {"pct": 0.09}})
    db.add_item(wl, "BBB")
    db.add_item(wl, "ZZZ")                                               # unknown symbol
    dates = _prices("AAA", crash)["date"]

    # first crash session: the 10-session drop crosses -9% and the stock closes at the lower circuit
    session, results = engine.run(store, db, as_of=dates.iloc[-3])
    assert session == dates.iloc[-3]
    r = results[0]
    assert r.missing_symbols == ["ZZZ"]
    ids = {(a.symbol, a.rule_id) for a in r.new_alerts}
    assert ("AAA", "lower_circuit") in ids and ("AAA", "big_drop") in ids
    assert all(a.symbol != "BBB" for a in r.new_alerts)

    # third crash session: circuit streak is high severity; the drop episode does not re-fire
    session, results = engine.run(store, db)
    assert session == dates.iloc[-1]
    r = results[0]
    lc = next(a for a in r.new_alerts if a.rule_id == "lower_circuit")
    assert lc.severity == "high" and "3rd session in a row" in lc.headline
    assert all(a.rule_id != "big_drop" for a in r.alerts)

    # same session again: nothing new
    _, again = engine.run(store, db)
    assert again[0].new_alerts == [] and len(again[0].alerts) == len(r.alerts)
