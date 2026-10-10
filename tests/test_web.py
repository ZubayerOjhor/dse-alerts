import datetime as dt

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dse_alerts.appdb import AppDB
from dse_alerts.config import Settings
from dse_alerts.data.store import MarketStore
from dse_alerts.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    dates = pd.bdate_range("2025-01-01", periods=70).date
    for sym, closes in {"GP": [100 + i * 0.5 for i in range(70)], "ROBI": [30.0] * 70}.items():
        store.upsert_prices(pd.DataFrame({"symbol": sym, "date": dates, "open": closes, "high": closes, "low": closes,
                                          "close": closes, "volume": 1000}), source="live")
    store.refresh_instruments()
    store.upsert_instrument_details(pd.DataFrame([
        {"symbol": "GP", "name": "Grameenphone", "category": "A", "sector": "Telecom", "instrument_type": "Equity", "listing_year": 2009, "tradeable": True},
        {"symbol": "ROBI", "name": "Robi", "category": "A", "sector": "Telecom", "instrument_type": "Equity", "listing_year": 2020, "tradeable": True},
        {"symbol": "NONAME", "name": None, "category": None, "sector": None, "instrument_type": None, "listing_year": None, "tradeable": True},
    ]))
    from dse_alerts.analysis import features
    features.build(store)
    settings = Settings(dse_data_dir=tmp_path, app_secret_key="test-secret")
    app = create_app(settings, store, AppDB(tmp_path / "app.sqlite"))
    return TestClient(app, follow_redirects=False)


def _register(client, email="me@example.com", password="correct horse"):
    r = client.post("/register", data={"email": email, "password": password})
    assert r.status_code == 303 and "dse_session" in r.cookies
    return r


def test_first_visit_goes_to_register_then_dashboard(client):
    assert client.get("/login").headers["location"] == "/register"
    assert client.get("/").headers["location"] == "/login"
    _register(client)
    r = client.get("/")
    assert r.status_code == 200 and "Your watchlist" in r.text
    csrf = r.text.split('name="csrf" value="')[1].split('"')[0]

    assert client.post("/watchlist/add", data={"symbol": "gp", "csrf": csrf}).status_code == 303
    assert client.post("/watchlist/add", data={"symbol": "NOPE", "csrf": csrf}).status_code == 404
    assert client.post("/watchlist/add", data={"symbol": "ROBI", "csrf": "wrong"}).status_code == 400
    r = client.get("/")
    assert "Grameenphone" in r.text and "ROBI" not in r.text

    r = client.get("/symbol/GP")
    assert r.status_code == 200 and "Rules for GP" in r.text
    r = client.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on", "big_drop__days": "10", "big_drop__pct": "5",
                                              "on_upper_circuit": "on", "on_pe_level": "on", "pe_level__max": "12", "note": "core holding"})
    assert r.status_code == 303
    r = client.get("/symbol/GP")
    assert 'value="5.0"' in r.text and "core holding" in r.text

    assert client.post("/alerts/check", data={"csrf": csrf}).status_code == 303
    assert client.get("/alerts").status_code == 200
    r = client.get("/scorecard")
    assert r.status_code == 200 and "Scorecard" in r.text
    r = client.post("/settings", data={"csrf": csrf, "alert_to": "", "telegram_chat_id": "42"})
    assert r.status_code == 303 and 'value="42"' in client.get("/settings").text
    assert client.get("/rules").status_code == 200
    assert client.get("/settings").status_code == 200
    codes = client.get("/instruments.json").json()
    assert codes[0]["symbol"] == "GP" and {"symbol": "NONAME", "name": ""} in codes   # NaN name must not break JSON


def test_login_logout_and_bad_password(client):
    _register(client)
    client.post("/logout")
    assert client.get("/").headers["location"] == "/login"
    assert "Wrong email" in client.post("/login", data={"email": "me@example.com", "password": "nope"}).text
    assert client.post("/login", data={"email": "me@example.com", "password": "correct horse"}).status_code == 303
    assert client.get("/").status_code == 200


def test_rule_overrides_are_stored_as_the_engine_expects(client, tmp_path):
    _register(client)
    csrf = client.get("/").text.split('name="csrf" value="')[1].split('"')[0]
    client.post("/watchlist/add", data={"symbol": "GP", "csrf": csrf})
    client.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on", "big_drop__pct": "5", "on_volume_spike": "", "on_ma_cross": "on"})
    rules = AppDB(tmp_path / "app.sqlite").items(1)[0]["rules"]
    assert rules["big_drop"] == {"pct": 0.05}          # ticked, with an override (5% -> 0.05)
    assert rules["ma_cross"] == {}                     # opt-in rule ticked with no params
    assert rules["volume_spike"] is False              # a default-on rule left unticked is silenced
    assert rules["upper_circuit"] is False
    assert "pe_level" not in rules                     # opt-in rule left unticked is simply absent
    assert dt.date.today()  # keep import used
