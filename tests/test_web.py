import datetime as dt

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dse_alerts.appdb import AppDB
from dse_alerts.config import Settings
from dse_alerts.data.store import MarketStore
from dse_alerts.web.app import create_app


@pytest.fixture()
def env(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    dates = pd.bdate_range("2025-01-01", periods=70).date
    for sym, closes in {"GP": [100 + i * 0.5 for i in range(70)], "ROBI": [30.0] * 70}.items():
        store.upsert_prices(pd.DataFrame({"symbol": sym, "date": dates, "open": closes, "high": [c * 1.01 for c in closes],
                                          "low": [c * 0.99 for c in closes], "close": closes, "volume": 1000}), source="live")
    store.upsert_market_summary(pd.DataFrame({"date": dates, "dsex": [5000 + i for i in range(70)]}))
    store.refresh_instruments()
    store.upsert_instrument_details(pd.DataFrame([
        {"symbol": "GP", "name": "Grameenphone", "category": "A", "sector": "Telecom", "instrument_type": "Equity", "listing_year": 2009, "tradeable": True},
        {"symbol": "ROBI", "name": "Robi", "category": "A", "sector": "Telecom", "instrument_type": "Equity", "listing_year": 2020, "tradeable": True},
        {"symbol": "NONAME", "name": None, "category": None, "sector": None, "instrument_type": None, "listing_year": None, "tradeable": True},
    ]))
    store.upsert_fundamentals(pd.DataFrame([{"symbol": "GP", "as_of": dt.date(2025, 4, 1), "price": 134.0, "pe": 11.0, "eps": 12.0, "nav": 40.0,
                                             "dividend_yield": 8.0, "free_float_pct": 10.0, "market_cap": 1e11, "paid_up_capital": 1e10,
                                             "face_value": 10.0, "week_high_52": 140.0, "week_low_52": 90.0}]))
    from dse_alerts.analysis import features
    features.build(store)
    settings = Settings(dse_data_dir=tmp_path, app_secret_key="test-secret")
    db = AppDB(tmp_path / "app.sqlite")
    app = create_app(settings, store, db)
    return TestClient(app, follow_redirects=False), db


@pytest.fixture()
def client(env):
    return env[0]


def _register(client, email="me@example.com", password="correct horse"):
    r = client.post("/register", data={"email": email, "password": password})
    assert r.status_code == 303 and "dse_session" in r.cookies
    return r


def _csrf(client):
    return client.get("/").text.split('name="csrf" value="')[1].split('"')[0]


def test_first_visit_goes_to_register_then_dashboard(client):
    assert client.get("/login").headers["location"] == "/register"
    assert client.get("/").headers["location"] == "/login"
    _register(client)
    r = client.get("/")
    assert r.status_code == 200 and "Overview" in r.text and "DSEX" in r.text
    csrf = _csrf(client)

    assert client.post("/watchlist/add", data={"symbol": "gp", "csrf": csrf}).status_code == 303
    assert client.post("/watchlist/add", data={"symbol": "NOPE", "csrf": csrf}).status_code == 404
    assert client.post("/watchlist/add", data={"symbol": "ROBI", "csrf": "wrong"}).status_code == 400
    r = client.get("/")
    assert "Grameenphone" in r.text and "ROBI" not in r.text and 'class="spark' in r.text

    r = client.get("/symbol/GP")
    assert r.status_code == 200 and "Alert rules for GP" in r.text and "What is true right now" in r.text
    assert "price-data" in r.text and "rel-data" in r.text and "sector median" in r.text
    r = client.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on", "big_drop__days": "10", "big_drop__pct": "5",
                                              "on_upper_circuit": "on", "on_pe_level": "on", "pe_level__max": "12", "note": "core holding"})
    assert r.status_code == 303
    r = client.get("/symbol/GP")
    assert 'value="5.0"' in r.text and "core holding" in r.text

    assert client.post("/alerts/check", data={"csrf": csrf}).status_code == 303
    for path in ("/alerts", "/scorecard", "/rules", "/settings", "/portfolio", "/targets"):
        assert client.get(path).status_code == 200, path
    r = client.post("/settings", data={"csrf": csrf, "alert_to": "", "telegram_chat_id": "42"})
    assert r.status_code == 303 and 'value="42"' in client.get("/settings").text
    codes = client.get("/instruments.json").json()
    assert codes[0]["symbol"] == "GP" and {"symbol": "NONAME", "name": ""} in codes

    ohlc = client.get("/api/ohlc/GP?range=3m").json()
    assert len(ohlc["candles"]) == 65 and ohlc["candles"][-1]["close"] == 100 + 69 * 0.5 and ohlc["sma20"]
    assert client.get("/api/index?range=1m").json()["series"][0]["data"][-1]["value"] == 5069


def test_targets_and_kinds(client):
    _register(client)
    csrf = _csrf(client)
    assert client.post("/watchlist/add", data={"symbol": "ROBI", "csrf": csrf, "kind": "target"}).status_code == 303
    r = client.get("/targets")
    assert r.status_code == 200 and "ROBI" in r.text and "between the averages" in r.text or "above both" in r.text or "below both" in r.text
    assert client.post("/watchlist/kind", data={"symbol": "ROBI", "kind": "watch", "csrf": csrf}).status_code == 303
    assert "ROBI" not in client.get("/targets").text.split("<tbody>")[-1] if "<tbody>" in client.get("/targets").text else True
    assert "No targets yet" in client.get("/targets").text


def test_portfolio_page_with_holdings(env, tmp_path):
    client, db = env
    _register(client)
    uid = db.user("me@example.com")["id"]
    acc = db.ensure_account(uid, "BO 1")
    db.upsert_holding(acc, "GP", 10, 50.0, 90.0, "2025-01-01", "marginable")
    db.upsert_holding(acc, "ROBI", 100, 10.0)
    r = client.get("/portfolio")
    assert r.status_code == 200 and "BO 1" in r.text and "pv-data" in r.text and "Telecom" in r.text
    assert "Unrealised P&amp;L" in r.text
    r = client.get("/")
    assert "Portfolio value" in r.text and "owned" in r.text          # held symbols are shown as owned
    r = client.get("/symbol/GP")
    assert "you hold 10" in r.text
    csrf = _csrf(client)
    assert client.post("/accounts/rename", data={"csrf": csrf, "account_id": acc, "name": "Mine"}).status_code == 303
    assert "Mine" in client.get("/settings").text


def test_login_logout_and_bad_password(client):
    _register(client)
    client.post("/logout")
    assert client.get("/").headers["location"] == "/login"
    assert "Wrong email" in client.post("/login", data={"email": "me@example.com", "password": "nope"}).text
    assert client.post("/login", data={"email": "me@example.com", "password": "correct horse"}).status_code == 303
    assert client.get("/").status_code == 200


def test_rule_overrides_are_stored_as_the_engine_expects(env, tmp_path):
    client, db = env
    _register(client)
    csrf = _csrf(client)
    client.post("/watchlist/add", data={"symbol": "GP", "csrf": csrf})
    client.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on", "big_drop__pct": "5", "on_volume_spike": "", "on_ma_cross": "on"})
    rules = db.items(1)[0]["rules"]
    assert rules["big_drop"] == {"pct": 0.05}          # ticked, with an override (5% -> 0.05)
    assert rules["ma_cross"] == {}                     # opt-in rule ticked with no params
    assert rules["volume_spike"] is False              # a default-on rule left unticked is silenced
    assert rules["upper_circuit"] is False
    assert "pe_level" not in rules                     # opt-in rule left unticked is simply absent


def test_language_toggle_cookie_and_saved_preference(env):
    client, db = env
    _register(client)
    assert "Overview" in client.get("/").text and 'lang="en"' in client.get("/").text
    r = client.get("/lang/bn", headers={"referer": "/portfolio"})
    assert r.status_code == 303 and r.headers["location"] == "/portfolio" and client.cookies.get("dse_lang") == "bn"
    page = client.get("/").text
    assert 'lang="bn"' in page and "সারসংক্ষেপ" in page and "Overview" not in page
    csrf = _csrf(client)
    client.post("/watchlist/add", data={"symbol": "GP", "csrf": csrf})
    sym = client.get("/symbol/GP").text
    assert "এই মুহূর্তে যা সত্য" in sym and "GP-এর অ্যালার্ট নিয়ম" in sym and "বড় পতন" in sym
    assert "নিয়ম" in client.get("/rules").text and "স্কোরকার্ড" in client.get("/scorecard").text
    # the saved preference drives emails and is the fallback when the cookie is gone
    client.post("/settings", data={"csrf": csrf, "alert_to": "", "telegram_chat_id": "", "lang": "bn"})
    assert db.user("me@example.com")["lang"] == "bn"
    client.cookies.delete("dse_lang")
    assert 'lang="bn"' in client.get("/").text
    client.get("/lang/en")
    assert "Overview" in client.get("/").text


def test_demo_mode_is_read_only(tmp_path, env):
    client, db = env
    from dse_alerts.appdb import DEMO_EMAIL
    from dse_alerts.config import Settings

    # default: no demo button, /demo is a 404
    _register(client)
    client.post("/logout")
    assert "Try the demo" not in client.get("/login").text and client.post("/demo").status_code == 404

    settings = Settings(dse_data_dir=tmp_path, app_secret_key="test-secret", app_demo=True)
    import dse_alerts.web.app as webapp
    from dse_alerts.data.store import MarketStore

    app = webapp.create_app(settings, MarketStore(tmp_path / "m.duckdb"), db)
    demo = TestClient(app, follow_redirects=False)
    assert "Try the demo" not in demo.get("/login").text          # not seeded yet
    uid = db.ensure_user(DEMO_EMAIL)
    db.add_item(db.ensure_watchlist(uid), "GP", {}, kind="watch")
    assert "Try the demo" in demo.get("/login").text
    assert demo.post("/demo").status_code == 303
    page = demo.get("/").text
    assert "shared demo account" in page and "Grameenphone" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = demo.post("/watchlist/add", data={"symbol": "ROBI", "csrf": csrf})
    assert r.status_code == 403 and "read-only" in r.text
    assert demo.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on"}).status_code == 403
    assert demo.post("/settings", data={"csrf": csrf, "alert_to": "x@y.z", "lang": "bn"}).status_code == 403
    assert demo.post("/watchlist/remove", data={"symbol": "GP", "csrf": csrf}).status_code == 403
    assert [i["symbol"] for i in db.items(uid)] == ["GP"]          # nothing changed
    assert demo.post("/alerts/check", data={"csrf": csrf}).status_code == 303   # reading is fine
    # the demo address cannot be registered or logged into with a password
    assert "already has an account" in demo.post("/register", data={"email": DEMO_EMAIL, "password": "longenough"}).text
    assert "Wrong email" in demo.post("/login", data={"email": DEMO_EMAIL, "password": "anything"}).text


def test_ai_summary_card(env):
    client, db = env
    _register(client)
    page = client.get("/").text
    assert "AI summary" in page and "GROQ_API_KEY" in page            # off: tells the user how to turn it on
    uid = db.user("me@example.com")["id"]
    # store a digest for the latest session and it shows on the overview
    last = page.split("<strong>")[1].split("</strong>")[0]
    db.save_digest(uid, last, "A quiet session for your list.", "groq:llama-3.3-70b-versatile", "en")
    page = client.get("/").text
    assert "A quiet session for your list." in page and "groq:llama-3.3-70b-versatile" in page
    row = db.digest_for(uid, last)
    assert row["lang"] == "en" and row["provider"].startswith("groq")
