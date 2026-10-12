import pandas as pd

from dse_alerts import portfolio as pf
from dse_alerts.appdb import AppDB
from dse_alerts.config import Settings
from dse_alerts.data.store import MarketStore


def _store(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    dates = pd.bdate_range("2025-01-01", periods=60).date
    for sym, closes in {"GP": [100.0] * 59 + [110.0], "ROBI": [30.0] * 59 + [27.0]}.items():
        store.upsert_prices(pd.DataFrame({"symbol": sym, "date": dates, "open": closes, "high": closes, "low": closes,
                                          "close": closes, "volume": 100}), source="live")
    store.refresh_instruments()
    store.upsert_instrument_details(pd.DataFrame([
        {"symbol": "GP", "name": "Grameenphone", "category": "A", "sector": "Telecom", "instrument_type": "Equity", "listing_year": 2009, "tradeable": True},
        {"symbol": "ROBI", "name": "Robi", "category": "A", "sector": "Telecom", "instrument_type": "Equity", "listing_year": 2020, "tradeable": True},
    ]))
    with store.connect() as con:   # GP paid 10% stock dividend, record date on the 41st session
        con.execute("INSERT INTO dividends VALUES ('GP', ?, 'Dec 2024', '10% Stock Dividend', NULL, ?)", [dates[30], dates[40]])
    from dse_alerts.analysis import features
    features.build(store)
    return store


def test_import_valuation_and_history(tmp_path):
    store = _store(tmp_path)
    db = AppDB(tmp_path / "app.sqlite")
    uid = db.ensure_user("u@example.com")
    csv = tmp_path / "h.csv"
    # account A's statement predates GP's bonus issue, so its 10 shares became 11; B's does not
    csv.write_text("account,symbol,quantity,avg_cost,statement_price,statement_date,margin\n"
                   "A,GP,10,50,90,2025-01-15,marginable\nA,ROBI,100,10,29,2025-11-30,\nB,GP,5,120,90,2025-11-30,\n")
    r = pf.import_csv(db, uid, csv)
    assert r == {"rows": 3, "accounts": ["A", "B"]}
    pf.import_csv(db, uid, csv)                                   # idempotent
    assert len(db.holdings(uid)) == 3

    v = pf.valuation(store, db, uid)
    pos = v["positions"].set_index(["account", "symbol"])
    assert abs(pos.loc[("A", "GP"), "quantity"] - 11) < 1e-9 and abs(pos.loc[("A", "GP"), "bonus_shares"] - 1) < 1e-9
    assert abs(pos.loc[("A", "GP"), "value"] - 1210) < 1e-9 and abs(pos.loc[("A", "GP"), "pnl"] - 710) < 1e-9
    assert abs(pos.loc[("A", "GP"), "day_change"] - 110) < 1e-9          # 11 shares x (110 - 100)
    assert pos.loc[("B", "GP"), "quantity"] == 5 and abs(pos.loc[("B", "GP"), "pnl_pct"] - (550 / 600 - 1)) < 1e-9
    assert abs(v["total"]["cost"] - (500 + 1000 + 600)) < 1e-9 and abs(v["total"]["value"] - (1210 + 2700 + 550)) < 1e-9
    assert v["accounts"].set_index("account").loc["B", "positions"] == 1
    assert abs(pos.loc[("A", "GP"), "since_statement_pct"] - (1210 / 900 - 1)) < 1e-9   # against the statement's 10 shares
    assert abs(v["total"]["bonus_shares"] - 1) < 1e-9

    sect = pf.by_group(v["positions"], "sector")
    assert sect.iloc[0]["sector"] == "Telecom" and abs(sect.iloc[0]["weight"] - 1.0) < 1e-9

    hist = pf.value_history(store, db, uid, sessions=30)
    assert len(hist) == 30 and abs(hist["value"].iloc[-1] - (1210 + 2700 + 550)) < 1e-6
    # today's 16 GP shares priced at the adjusted 90.9 before the bonus: A's 11 are its old 10 at 100,
    # B's 5 (a post-bonus statement) were 4.5 shares then
    assert abs(hist["value"].iloc[0] - (16 * 100 / 1.1 + 3000)) < 1e-6
    assert abs(hist["cost"].iloc[0] - 2100) < 1e-9


def test_ai_chain_order_and_legacy_setting():
    s = Settings(ai_providers="groq,openrouter,anthropic", groq_api_key="g", anthropic_api_key="a")
    assert [p["name"] for p in s.ai_chain()] == ["groq", "anthropic"]
    assert s.ai_enabled and s.ai_label.startswith("groq:")
    legacy = Settings(ai_providers="", ai_provider="openai_compatible", openai_compat_api_key="k", openai_compat_base_url="https://x/v1")
    assert [p["name"] for p in legacy.ai_chain()] == ["openai_compatible"]
    assert not Settings(ai_providers="groq,openrouter").ai_enabled
