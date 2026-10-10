import datetime as dt
import json

import httpx
import pandas as pd

from dse_alerts import scorecard as sc
from dse_alerts.appdb import AppDB
from dse_alerts.config import Settings
from dse_alerts.data.store import MarketStore
from dse_alerts.notify import deliver
from dse_alerts.rules import Alert


def _store_with(tmp_path, closes_by_symbol, index=None):
    store = MarketStore(tmp_path / "m.duckdb")
    dates = pd.bdate_range("2025-01-01", periods=len(next(iter(closes_by_symbol.values())))).date
    for sym, closes in closes_by_symbol.items():
        store.upsert_prices(pd.DataFrame({"symbol": sym, "date": dates, "open": closes, "high": closes, "low": closes,
                                          "close": closes, "volume": 1000}), source="live")
    if index is not None:
        store.upsert_market_summary(pd.DataFrame({"date": dates, "dsex": index}))
    store.refresh_instruments()
    from dse_alerts.analysis import features
    features.build(store)
    return store, dates


def test_scorecard_outcomes_and_pending(tmp_path):
    closes = [100.0] * 30 + [110.0] * 30          # +10% after session 30
    idx = [5000.0] * 30 + [5100.0] * 30            # DSEX +2%
    store, dates = _store_with(tmp_path, {"GP": closes}, idx)
    db = AppDB(tmp_path / "app.sqlite")
    uid = db.ensure_user("u@example.com")
    early = Alert("GP", "big_rise", dates[29], "GP up", "d", "notable", {})       # 30 sessions follow -> scored
    late = Alert("GP", "upper_circuit", dates[55], "GP circuit", "d", "high", {})  # 4 sessions follow -> pending
    db.record_alerts(uid, [early, late])

    df = sc.outcomes(store, db, uid)
    assert len(df) == 2
    scored = df[df["rule_id"] == "big_rise"].iloc[0]
    assert scored["status"] == "scored" and abs(scored["fwd_20"] - 0.10) < 1e-9 and abs(scored["excess_20"] - 0.08) < 1e-9
    assert df[df["rule_id"] == "upper_circuit"].iloc[0]["status"] == "pending"

    agg = sc.by_rule(df, {"big_rise": {"fwd20_median": -0.021}})
    row = agg[agg["rule_id"] == "big_rise"].iloc[0]
    assert row["alerts"] == 1 and row["scored"] == 1 and row["fwd20_positive_share"] == 1.0
    assert abs(row["backtest_fwd20_median"] + 0.021) < 1e-9


def test_telegram_send_splits_long_messages_and_reports_failures():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200 if body["chat_id"] == "123" else 400, json={"ok": body["chat_id"] == "123"})

    s = Settings(telegram_bot_token="tok")
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(60))      # ~6.5k chars -> two parts
    assert deliver.send_telegram(s, "123", text, transport=httpx.MockTransport(handler))
    assert len(calls) == 2 and all(len(c["text"]) <= deliver.TELEGRAM_LIMIT for c in calls)
    assert not deliver.send_telegram(s, "999", "hi", transport=httpx.MockTransport(handler))
    assert not deliver.send_telegram(Settings(), "123", "hi")           # no token -> off


def test_telegram_updates_lists_chats():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": [
            {"message": {"chat": {"id": 42, "first_name": "Zubayer"}, "text": "hello"}},
            {"message": {"chat": {"id": 42, "first_name": "Zubayer"}, "text": "again"}},
        ]})

    ups = deliver.telegram_updates(Settings(telegram_bot_token="tok"), transport=httpx.MockTransport(handler))
    assert ups == [{"chat_id": "42", "name": "Zubayer", "text": "hello"}]


def test_telegram_text_rendering():
    a = Alert("GP", "big_drop", dt.date(2026, 10, 8), "GP down 12%", "detail here", "high", {})
    t = deliver.telegram_text(dt.date(2026, 10, 8), [a], "Quiet day.")
    assert "!! GP down 12%" in t and "Quiet day." in t and "not advice" in t
