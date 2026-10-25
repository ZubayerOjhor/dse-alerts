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
                                             "face_value": 10.0, "week_high_52": 140.0, "week_low_52": 90.0},
                                            {"symbol": "ROBI", "as_of": dt.date(2025, 4, 1), "price": 30.0, "pe": 0.0, "eps": -1.2, "nav": 12.0,
                                             "dividend_yield": 0.0, "free_float_pct": 10.0, "market_cap": 1e10, "paid_up_capital": 5e10,
                                             "face_value": 10.0, "week_high_52": 40.0, "week_low_52": 25.0}]))
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
    assert client.get("/").headers["location"] == "/register"
    _register(client)
    r = client.get("/")
    assert r.status_code == 200 and "Overview" in r.text and "DSEX" in r.text
    csrf = _csrf(client)

    assert client.post("/watchlist/add", data={"symbol": "gp", "csrf": csrf}).status_code == 303
    r = client.post("/watchlist/add", data={"symbol": "NOPE", "csrf": csrf})
    assert r.status_code == 303 and r.headers["location"].endswith("#add")        # the page stays; the message is on it
    assert "not a DSE trading code" in client.get("/").text
    assert client.post("/watchlist/add", data={"symbol": "ROBI", "csrf": "wrong"}).status_code == 400
    r = client.get("/")
    watched = r.text.split("Market today")[0]      # the market section below lists every stock that traded
    assert "Grameenphone" in watched and "ROBI" not in watched and 'class="spark' in watched

    r = client.get("/symbol/GP")
    assert r.status_code == 200 and "Change the alert rules for GP" in r.text and "What is true right now" in r.text
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
    assert r.status_code == 303 and "Saved." in client.get("/settings?msg=settings.saved").text
    assert 'name="telegram_chat_id"' not in client.get("/settings").text     # no bot on this site: the field stays out of the way
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
    assert "Unrealised profit / loss" in r.text
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
    home = client.get("/")
    assert home.status_code == 200 and "Create a free account" in home.text and 'type="password"' not in home.text
    assert client.get("/portfolio").headers["location"] == "/login"
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
    assert "/watchlist/add" not in page and "/alerts/check" not in page    # a read-only account shows no write buttons
    csrf = demo.get("/settings").text.split('name="csrf" value="')[1].split('"')[0]
    r = demo.post("/watchlist/add", data={"symbol": "ROBI", "csrf": csrf})
    assert r.status_code == 403 and "read-only" in r.text
    assert demo.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on"}).status_code == 403
    assert demo.post("/settings", data={"csrf": csrf, "alert_to": "x@y.z", "lang": "bn"}).status_code == 403
    assert demo.post("/watchlist/remove", data={"symbol": "GP", "csrf": csrf}).status_code == 403
    assert [i["symbol"] for i in db.items(uid)] == ["GP"]          # nothing changed
    assert demo.post("/alerts/check", data={"csrf": csrf}).status_code == 403   # it records alerts, so it is a write
    assert "Recent runs" not in demo.get("/settings").text                 # operator details stay with the operator
    assert db.operator_id() == db.user("me@example.com")["id"]
    # the demo address cannot be registered or logged into with a password
    assert "already has an account" in demo.post("/register", data={"email": DEMO_EMAIL, "password": "longenough"}).text
    assert "Wrong email" in demo.post("/login", data={"email": DEMO_EMAIL, "password": "anything"}).text


def test_ai_summary_card(env):
    client, db = env
    _register(client)
    page = client.get("/").text
    assert 'class="ai-line' not in page and "AI summary" not in page   # nothing fired: no AI card, no AI line
    uid = db.user("me@example.com")["id"]
    # store a digest for the latest session and it shows on the overview
    last = page.split('<time datetime="')[1].split('"')[0]
    db.save_digest(uid, last, "A quiet session for your list.", "groq:llama-3.3-70b-versatile", "en")
    page = client.get("/").text
    assert "A quiet session for your list." in page and "groq:llama" not in page   # the model id stays out of the page
    assert "AI summary" in page and 'class="ai-line' not in page                # with a note, the card is back
    row = db.digest_for(uid, last)
    assert row["lang"] == "en" and row["provider"].startswith("groq")


def test_overview_writes_ai_note_and_follows_language(env, monkeypatch):
    import datetime as dt

    from dse_alerts import config
    from dse_alerts.ai import digest as ai
    from dse_alerts.rules import Alert

    client, db = env
    _register(client)
    uid = db.user("me@example.com")["id"]
    last = client.get("/").text.split('<time datetime="')[1].split('"')[0]
    db.record_alerts(uid, [Alert(symbol="GP", rule_id="big_rise", date=dt.date.fromisoformat(last), headline="GP up",
                                 detail="d", severity="notable", evidence={"ret": 0.1})])
    calls = []

    def fake(settings, session, alerts, symbols, rule_evidence, lang="en"):
        calls.append((session, [a.symbol for a in alerts], lang))
        return f"note-in-{lang}", "fake:model"

    monkeypatch.setattr(config.Settings, "ai_enabled", property(lambda self: True))
    monkeypatch.setattr(ai, "write_digest_detailed", fake)
    monkeypatch.setattr(ai, "load_rule_evidence", lambda s: {})

    page = client.get("/").text
    assert "note-in-en" in page and "fake:model" not in page      # the note shows, the model id does not
    assert calls == [(dt.date.fromisoformat(last), ["GP"], "en")]
    assert client.get("/").status_code == 200
    assert len(calls) == 1                      # stored note is reused
    client.get("/lang/bn")
    page = client.get("/").text
    assert "note-in-bn" in page and len(calls) == 2 and calls[-1][2] == "bn"
    assert db.digest_for(uid, last)["lang"] == "bn"


def test_find_page_filters_and_presets(env):
    client, _db = env
    _register(client)
    r = client.get("/find")
    assert r.status_code == 200 and "GP" in r.text and "ROBI" in r.text and "Near 52-week low" in r.text
    r = client.get("/find?sector=Telecom&category=A&sort=symbol")
    assert r.status_code == 200 and "GP" in r.text
    r = client.get("/find?sector=Bank")
    assert "Nothing matches" in r.text
    r = client.get("/find?ret20_min=5")                       # GP rose about 8% over 20 sessions, ROBI is flat
    assert "GP" in r.text and 'title="Robi"' not in r.text
    assert client.get("/find?preset=low").status_code == 200 and client.get("/find?preset=volume").status_code == 200
    r = client.get("/find?pe_min=abc&ret20_min=5")            # junk is refused beside the field, the typed text stays
    assert r.status_code == 400 and "Enter a number." in r.text and 'value="abc"' in r.text and 'value="5"' in r.text


def test_learn_page_in_both_languages(env):
    client, _db = env
    _register(client)
    r = client.get("/learn")
    assert r.status_code == 200 and "Circuit breakers" in r.text and "Big drop" in r.text and 'id="columns"' in r.text
    client.get("/lang/bn")
    r = client.get("/learn")
    assert r.status_code == 200 and "সার্কিট ব্রেকার" in r.text


def test_overview_market_section(env):
    client, _db = env
    _register(client)
    r = client.get("/").text
    assert "Market today" in r and "Telecom" in r and "Biggest moves" in r and "Upcoming record dates" in r



def test_security_headers_and_cookie_flags(client):
    r = client.get("/learn")
    assert r.status_code == 200                                   # public, no account needed
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and "referrer-policy" in r.headers
    r = client.post("/register", data={"email": "a@example.com", "password": "correct horse"}, headers={"x-forwarded-proto": "https"})
    assert "Secure" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    r = client.get("/lang/bn", headers={"referer": "https://evil.example/phish"})
    assert r.headers["location"] == "/phish"                   # the path is kept, the foreign host is dropped
    r = client.get("/lang/en", headers={"referer": "http://testserver/find?preset=low"})
    assert r.headers["location"] == "/find?preset=low"
    assert client.get("/demo").status_code == 405                 # POST only: a link cannot swap someone's session
    assert client.post("/login", data={"email": "a@example.com", "password": "correct horse"},
                       headers={"origin": "https://evil.example"}).status_code == 400


def test_login_rate_limit_and_password_change_signs_other_sessions_out(env):
    client, _db = env
    _register(client)
    other = TestClient(client.app, follow_redirects=False)
    assert other.post("/login", data={"email": "me@example.com", "password": "correct horse"}).status_code == 303
    assert other.get("/portfolio").status_code == 200
    csrf = _csrf(client)
    r = client.post("/settings", data={"csrf": csrf, "alert_to": "me@example.com", "lang": "en", "password": "new password 1"})
    assert r.status_code == 303
    assert client.get("/portfolio").status_code == 200           # the changing browser stays signed in
    assert other.get("/portfolio").headers["location"] == "/login"   # the other one is out
    assert "Wrong email" in other.post("/login", data={"email": "me@example.com", "password": "correct horse"}).text
    for _ in range(10):
        other.post("/login", data={"email": "me@example.com", "password": "still wrong"})
    r = other.post("/login", data={"email": "me@example.com", "password": "new password 1"})
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert "already has an account" in client.post("/register", data={"email": "me@example.com", "password": "whatever12"}).text
    assert "Check the email" in client.post("/register", data={"email": "not-an-email", "password": "whatever12"}).text
    assert client.post("/register", data={"email": "b@example.com", "password": "x" * 200}).status_code == 200


def test_alert_destinations_need_confirmation(env, monkeypatch):
    from dse_alerts import config
    from dse_alerts.notify import deliver

    client, db = env
    sent = []
    monkeypatch.setattr(config.Settings, "email_enabled", property(lambda self: True))
    monkeypatch.setattr(deliver, "send_plain_email", lambda settings, to, subject, body: sent.append((to, body)) or True)
    _register(client)                                              # the first account is the operator's: confirmed as is
    assert db.user("me@example.com")["email_confirmed"] == "me@example.com" and not sent
    client.post("/logout")
    second = TestClient(client.app, follow_redirects=False)
    second.post("/register", data={"email": "two@example.com", "password": "correct horse", "alert_to": "friend@example.com"})
    u = db.user("two@example.com")
    assert u["alert_email"] == "friend@example.com" and u["email_confirmed"] is None
    assert sent and sent[-1][0] == "friend@example.com"
    assert "not confirmed yet" in second.get("/settings").text
    link = sent[-1][1].split("http://127.0.0.1:8000")[1].split()[0]
    assert second.get(link).status_code == 303
    assert db.user("two@example.com")["email_confirmed"] == "friend@example.com"
    assert "is confirmed" in second.get("/settings").text
    assert second.get("/confirm/not-a-token").status_code == 400
    # Telegram: a chat id is verified by sending a code to the bot
    monkeypatch.setattr(config.Settings, "telegram_enabled", property(lambda self: True))
    monkeypatch.setattr(deliver, "telegram_bot_name", lambda settings: "dse_bot")
    csrf = second.get("/").text.split('name="csrf" value="')[1].split('"')[0]
    assert "digits only" in second.post("/settings", data={"csrf": csrf, "alert_to": "friend@example.com", "lang": "en", "telegram_chat_id": "abc"}).text
    second.post("/settings", data={"csrf": csrf, "alert_to": "friend@example.com", "lang": "en", "telegram_chat_id": "12345"})
    u = db.user("two@example.com")
    assert u["telegram_chat_id"] == "12345" and u["telegram_confirmed"] is None and u["telegram_code"]
    page = second.get("/settings").text
    assert u["telegram_code"] in page and "@dse_bot" in page
    monkeypatch.setattr(deliver, "telegram_messages", lambda settings: [{"chat_id": "999", "text": u["telegram_code"]}])
    second.post("/telegram/verify", data={"csrf": csrf})
    assert db.user("two@example.com")["telegram_confirmed"] is None     # right code, wrong chat
    monkeypatch.setattr(deliver, "telegram_messages", lambda settings: [{"chat_id": "12345", "text": "hi " + u["telegram_code"].lower()}])
    second.post("/telegram/verify", data={"csrf": csrf})
    assert db.user("two@example.com")["telegram_confirmed"] == "12345"
    assert "is verified" in second.get("/settings").text


def test_ai_notes_are_kept_per_language(env, monkeypatch):
    import datetime as dt

    from dse_alerts import config
    from dse_alerts.ai import digest as ai
    from dse_alerts.rules import Alert

    client, db = env
    _register(client)
    uid = db.user("me@example.com")["id"]
    last = client.get("/").text.split('<time datetime="')[1].split('"')[0]
    db.record_alerts(uid, [Alert(symbol="GP", rule_id="big_rise", date=dt.date.fromisoformat(last), headline="GP up",
                                 detail="d", severity="notable", evidence={"ret": 0.1})])
    calls = []
    monkeypatch.setattr(config.Settings, "ai_enabled", property(lambda self: True))
    monkeypatch.setattr(ai, "write_digest_detailed", lambda s, session, alerts, symbols, ev, lang="en": (calls.append(lang), (f"note-{lang}", "fake"))[1])
    monkeypatch.setattr(ai, "load_rule_evidence", lambda s: {})
    for code in ("en", "bn", "en", "bn", "en"):
        client.get(f"/lang/{code}")
        assert f"note-{code}" in client.get("/").text
    assert calls == ["en", "bn"]                                    # one provider call per language, never rewritten
    assert db.digest_for(uid, last, "en")["text"] == "note-en" and db.digest_for(uid, last, "bn")["text"] == "note-bn"


def test_unknown_pages_get_a_plain_message(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "There is no page at that address." in r.text and "Not Found" not in r.text
    r = client.get("/demo")
    assert r.status_code == 405 and "does not accept this kind of request" in r.text
    assert client.head("/learn").status_code == 200 and client.head("/learn").text == ""


def test_landing_sample_and_rule_text_match_the_code():
    """The landing page's sample alert and the Rules page describe what the code really does."""
    import json
    import re
    from pathlib import Path

    from dse_alerts.analysis.features import SLABS
    from dse_alerts.rules.catalog import RULES

    for lang in ("en", "bn"):
        cat = json.loads(Path("dse_alerts/i18n", f"{lang}.json").read_text(encoding="utf-8"))
        price = float(re.search(r"Tk (\d+\.\d)", cat["home.sample_detail"]).group(1))
        limit = next(pct for hi, pct in SLABS if price <= hi)
        assert f"{100 * limit:.2f}%" in cat["home.sample_detail"]       # the limit quoted is the slab for that price
        assert "ORIONINFU" in cat["home.sample_head"] and "+8.7%" in cat["home.sample_head"]
        assert "2 days" not in cat["rule.psi_news.desc"] and "২ দিন" not in cat["rule.psi_news.desc"]
    psi = next(r for r in RULES if r.id == "psi_news")
    assert "since the previous session" in psi.description


def test_uat_names_messages_and_undo(client):
    """A company name is accepted, mistakes stay on the page, and a removal can be undone."""
    _register(client)
    csrf = _csrf(client)
    r = client.post("/watchlist/add", data={"symbol": "grameenphone", "csrf": csrf, "kind": "target"}, headers={"referer": "http://testserver/targets"})
    assert r.status_code == 303 and r.headers["location"] == "/targets#add"
    page = client.get("/targets").text
    assert "GP added as a target" in page and "autofocus" in page
    # the same code again: a note, not a duplicate
    client.post("/watchlist/add", data={"symbol": "GP", "csrf": csrf, "kind": "target"})
    assert "already on your list" in client.get("/").text
    # remove, then undo with the rules it had
    client.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on", "big_drop__pct": "8", "note": "keep"})
    r = client.post("/watchlist/remove", data={"symbol": "GP", "csrf": csrf}, headers={"referer": "http://testserver/"})
    assert r.status_code == 303 and r.headers["location"] == "/#watched"
    page = client.get("/").text
    assert "GP removed" in page and 'action="/watchlist/restore"' in page and "Undo" in page
    client.post("/watchlist/restore", data={"symbol": "GP", "kind": "target", "rules": '{"big_drop": {"pct": 0.08}}', "note": "keep", "csrf": csrf})
    page = client.get("/symbol/GP").text
    assert "GP is back on your list" in page and "1 custom rule<" in page and 'value="8.0"' in page and "Move to watch list" in page and "Remove from my list" in page
    client.post("/watchlist/kind", data={"symbol": "GP", "kind": "watch", "csrf": csrf})
    page = client.get("/symbol/GP").text
    assert "on your watch list" in page and "Mark as target" in page


def test_uat_rule_form_errors_stay_on_the_page(client):
    _register(client)
    csrf = _csrf(client)
    client.post("/watchlist/add", data={"symbol": "GP", "csrf": csrf})
    r = client.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on", "big_drop__pct": "abc", "big_drop__days": "7", "note": "typed note"})
    assert r.status_code == 400 and "Enter a number." in r.text and 'value="abc"' in r.text and 'value="7"' in r.text and "typed note" in r.text
    assert "There is no page" not in r.text and "<details" in r.text and " open" in r.text.split('id="rules"', 1)[1][:80]
    assert 'type="number"' in r.text


def test_uat_check_now_reports_and_portfolio_is_editable(client):
    _register(client)
    csrf = _csrf(client)
    page = client.get("/").text
    assert "Add a stock above" in page
    client.post("/watchlist/add", data={"symbol": "GP", "csrf": csrf})
    page = client.get("/").text
    assert "Not checked yet" in page and "have not been checked" in page
    r = client.post("/alerts/check", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/#alerts"
    page = client.get("/").text
    assert "Checked 1 stocks against" in page and "Last checked" in page and "1 stocks, 0 alerts" in page
    client.post("/watchlist/add", data={"symbol": "ROBI", "csrf": csrf})
    page = client.get("/").text
    assert "1 added since" in page and "ROBI" in page.split("Nothing fired on the symbols that were checked")[1][:200]
    # holdings: typed, then uploaded, then removed; never a command line
    page = client.get("/portfolio").text
    assert "dse portfolio import" not in page and 'action="/portfolio/add"' in page and 'action="/portfolio/upload"' in page
    r = client.post("/portfolio/add", data={"csrf": csrf, "account": "BO 1", "symbol": "Grameenphone", "quantity": "100", "avg_cost": "120.5"})
    assert r.status_code == 303
    page = client.get("/portfolio").text
    assert "GP: 100 shares saved" in page and "BO 1" in page and 'action="/portfolio/remove"' in page
    r = client.post("/portfolio/add", data={"csrf": csrf, "account": "BO 1", "symbol": "GP", "quantity": "x", "avg_cost": "1"})
    assert r.status_code == 303 and "must be numbers" in client.get("/portfolio").text
    csv = "account,symbol,quantity,avg_cost\nBO 2,ROBI,1000,31.5\n"
    r = client.post("/portfolio/upload", data={"csrf": csrf}, files={"file": ("h.csv", csv, "text/csv")})
    assert r.status_code == 303 and "1 holdings imported into 1 account" in client.get("/portfolio").text
    bad = "account,symbol,quantity,avg_cost\nBO 2,NOPE,10,1\n"
    client.post("/portfolio/upload", data={"csrf": csrf}, files={"file": ("h.csv", bad, "text/csv")})
    assert "unknown trading codes: NOPE" in client.get("/portfolio").text
    assert client.get("/portfolio/template.csv").text.startswith("account,symbol,quantity,avg_cost")
    page = client.get("/portfolio").text
    account_id = page.split('name="account_id" value="')[1].split('"')[0]
    r = client.post("/portfolio/remove", data={"csrf": csrf, "account_id": account_id, "symbol": "GP"})
    assert r.status_code == 200 and "Please confirm" in r.text and "GP: 100" not in r.text   # no JavaScript: a confirmation page first
    r = client.post("/portfolio/remove", data={"csrf": csrf, "account_id": account_id, "symbol": "GP", "confirmed": "1"})
    assert r.status_code == 303 and "GP removed from your holdings" in client.get("/portfolio").text


def test_uat_settings_panel_is_the_operators_and_loss_makers_show_loss(env):
    client, _db = env
    _register(client)                                   # the operator
    assert "<h2>This installation</h2>" in client.get("/settings").text
    client.post("/logout")
    client.cookies.clear()
    client.post("/register", data={"email": "two@example.com", "password": "correct horse"})
    page = client.get("/settings").text
    assert "<h2>This installation</h2>" not in page and "Recent runs" not in page and "How alerts reach you" in page
    assert 'name="telegram_chat_id"' not in page           # Telegram is off for the whole site
    csrf = _csrf(client)
    client.post("/watchlist/add", data={"symbol": "ROBI", "csrf": csrf, "kind": "target"})
    page = client.get("/targets").text
    assert ">loss<" in page and "0.0 <span" not in page
    assert "loss" in client.get("/symbol/ROBI").text.split("Fundamentals")[1][:600]


def test_last_review_thresholds_plural_accounts_and_promises(env):
    """Zero or negative thresholds are refused, one rule is singular, accounts can be removed,
    and the landing page only promises the channels this installation has (none, in tests)."""
    client, db = env
    page = client.get("/").text                                   # landing page: no users yet -> register; after one, the home page
    _register(client)
    csrf = _csrf(client)
    client.post("/watchlist/add", data={"symbol": "GP", "csrf": csrf})
    r = client.post("/symbol/GP/rules", data={"csrf": csrf, "on_big_drop": "on", "big_drop__pct": "-5", "big_drop__days": "0"})
    assert r.status_code == 400 and r.text.count("Enter a number above 0.") == 2 and 'min="1"' in r.text and 'min="0.01"' in r.text
    from dse_alerts.rules import RULES
    from dse_alerts.rules.catalog import OPT_IN
    on = {f"on_{r.id}": "on" for r in RULES if r.id not in OPT_IN}          # what the browser sends with the defaults ticked
    client.post("/symbol/GP/rules", data={"csrf": csrf, "big_drop__pct": "8", **on})
    page = client.get("/symbol/GP").text
    assert "1 custom rule<" in page and "1 custom rules" not in page
    # an account with a holding: removable, with its count in the confirmation
    client.post("/portfolio/add", data={"csrf": csrf, "account": "UAT test", "symbol": "GP", "quantity": "10", "avg_cost": "100"})
    page = client.get("/settings").text
    assert "Remove the account UAT test and its 1 holdings?" in page and 'action="/accounts/delete"' in page
    acc = page.split('name="account_id" value="')[1].split('"')[0]
    r = client.post("/accounts/delete", data={"csrf": csrf, "account_id": acc})
    assert r.status_code == 200 and "Please confirm" in r.text and 'name="confirmed" value="1"' in r.text
    assert len(db.holdings(db.user("me@example.com")["id"])) == 1                     # nothing deleted without the second step
    assert client.post("/accounts/delete", data={"csrf": csrf, "account_id": acc, "confirmed": "1"}).status_code == 303
    page = client.get("/settings").text
    assert "Account UAT test removed with 1 holdings." in page and "UAT test</" not in page.split("Accounts")[-1][:10]
    assert db.holdings(db.user("me@example.com")["id"]) == []
    assert client.post("/accounts/delete", data={"csrf": csrf, "account_id": 999, "confirmed": "1"}).status_code == 303   # not yours / gone: no error
    # no mail server in the tests: nothing promises email (the operator sees the installation panel instead)
    client.post("/logout"); client.cookies.clear()
    client.post("/register", data={"email": "two@example.com", "password": "correct horse"})
    assert "does not send email" in client.get("/settings").text
    client.post("/logout"); client.cookies.clear()
    home = client.get("/").text
    assert "Alerts appear here, in English or Bangla" in home and "by email" not in home
    assert 'data-sort-hint="Click to sort by this column"' in home
