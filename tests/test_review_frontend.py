"""Acceptance tests for the front-end review: the screener sorts every match before paging,
filters are validated beside the field, destructive actions need a second step without
JavaScript, the chart API names its range, and demo settings cannot look savable."""

import datetime as dt
import json
import random
import re

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dse_alerts.appdb import AppDB
from dse_alerts.config import Settings
from dse_alerts.data.store import MarketStore
from dse_alerts.web.app import create_app

N = 130          # more than two pages of 50


@pytest.fixture()
def wide(tmp_path):
    """A disposable market of 130 stocks with known last-session moves and P/E values."""
    rnd = random.Random(7)
    store = MarketStore(tmp_path / "m.duckdb")
    dates = pd.bdate_range("2025-01-01", periods=30).date
    frames, details, funds, last_move = [], [], [], {}
    for k in range(N):
        sym = f"S{k:03d}"
        closes = [50.0] * 29
        move = round(rnd.uniform(-0.09, 0.09), 4)
        closes.append(round(50.0 * (1 + move), 2))
        last_move[sym] = closes[-1] / closes[-2] - 1
        frames.append(pd.DataFrame({"symbol": sym, "date": dates, "open": closes, "high": [c * 1.01 for c in closes],
                                    "low": [c * 0.99 for c in closes], "close": closes, "volume": 1000}))
        details.append({"symbol": sym, "name": f"Company {k}", "category": "A", "sector": "Bank" if k % 2 else "Fuel",
                        "instrument_type": "Equity", "listing_year": 2010, "tradeable": True})
        funds.append({"symbol": sym, "as_of": dt.date(2025, 1, 1), "price": 50.0, "pe": float(k % 40) - 3, "eps": 1.0, "nav": 10.0,
                      "dividend_yield": 1.0, "free_float_pct": 30.0, "market_cap": 1e9, "paid_up_capital": 1e9, "face_value": 10.0,
                      "week_high_52": 60.0, "week_low_52": 40.0})
    store.upsert_prices(pd.concat(frames), source="live")
    store.upsert_market_summary(pd.DataFrame({"date": dates, "dsex": [5000.0] * 30}))
    store.refresh_instruments()
    store.upsert_instrument_details(pd.DataFrame(details))
    store.upsert_fundamentals(pd.DataFrame(funds))
    from dse_alerts.analysis import features
    features.build(store)
    db = AppDB(tmp_path / "app.sqlite")
    app = create_app(Settings(dse_data_dir=tmp_path, app_secret_key="test-secret"), store, db)
    client = TestClient(app, follow_redirects=False)
    client.parts = (tmp_path, store, db)
    assert client.post("/register", data={"email": "qa@example.com", "password": "correct horse"}).status_code == 303
    return client, last_move


def _codes(html: str) -> list[str]:
    return re.findall(r'<a href="/symbol/(S\d{3})"><strong>', html)


def _csrf(client) -> str:
    return client.get("/").text.split('name="csrf" value="')[1].split('"')[0]


def test_sorting_finds_the_real_extremes_across_every_match(wide):
    client, moves = wide
    top = min(moves, key=lambda s: (-moves[s], s))
    bottom = min(moves, key=lambda s: (moves[s], s))
    r = client.get("/find?sort=ret_1&dir=desc")
    assert r.status_code == 200 and _codes(r.text)[0] == top
    assert "1-50 of 130 matching stocks" in r.text and "Page 1 of 3" in r.text
    r = client.get("/find?sort=ret_1&dir=asc")
    assert _codes(r.text)[0] == bottom                     # the lowest of all 130, not of the first page
    seen = []
    for p in (1, 2, 3):
        seen += _codes(client.get(f"/find?sort=ret_1&dir=asc&page={p}").text)
    assert len(seen) == N and len(set(seen)) == N          # every match once across the pages
    assert "101-130 of 130" in client.get("/find?sort=ret_1&dir=asc&page=3").text
    assert "101-130 of 130" in client.get("/find?sort=ret_1&dir=asc&page=99").text     # past the end: the last page


def test_filters_and_sort_survive_paging_and_header_links(wide):
    client, _ = wide
    html = client.get("/find?sector=Bank&pe_min=1&sort=pe&dir=asc").text
    links = [h.replace("&amp;", "&") for h in re.findall(r'class="th-sort" href="([^"]+)"', html)]
    pages = [h.replace("&amp;", "&") for h in re.findall(r'class="page" href="([^"]+)"', html)]
    assert links and all("sector=Bank" in h and "pe_min=1" in h for h in links + pages)
    pe_link = next(h for h in links if "sort=pe" in h)
    assert "dir=desc" in pe_link                            # the active column flips its direction
    assert 'aria-sort="ascending"' in html and html.count('aria-sort="none"') >= 8
    first = _codes(client.get("/find?sort=pe&dir=asc").text)[0]
    assert int(first[1:]) % 40 - 3 > 0                      # loss-makers' 0 or negative P/E never tops the list
    html = client.get("/find?sort=ret_1&dir=desc&page=2").text
    nxt = re.search(r'rel="next" href="([^"]+)"', html).group(1).replace("&amp;", "&")
    prv = re.search(r'rel="prev" href="([^"]+)"', html).group(1).replace("&amp;", "&")
    assert "page=3" in nxt and "sort=ret_1" in nxt and "dir=desc" in nxt and "page=" not in prv.split("#")[0]


def test_invalid_ranges_and_non_finite_numbers_are_refused_beside_the_field(wide):
    client, _ = wide
    r = client.get("/find?pe_min=20&pe_max=5")
    assert r.status_code == 400 and r.text.count("The minimum is above the maximum.") == 2
    assert 'aria-invalid="true"' in r.text and 'value="20"' in r.text and 'value="5"' in r.text and "Fix the highlighted fields" in r.text
    r = client.get("/find?pe_min=nan&ret20_max=inf&vol_min=1e999")
    assert r.status_code == 400 and r.text.count("Enter a number.") == 3 and 'value="nan"' in r.text
    client.get("/lang/bn")
    r = client.get("/find?ret20_min=10&ret20_max=-10")
    assert "সর্বনিম্ন মান সর্বোচ্চের চেয়ে বেশি।" in r.text


def test_chart_api_names_its_range_and_refuses_unknown_ones(wide):
    client, _ = wide
    r = client.get("/api/ohlc/S001?range=3m")
    assert r.status_code == 200 and r.json()["range"] == "3m" and r.json()["candles"]
    assert client.get("/api/ohlc/S001?range=10y").status_code == 400
    page = client.get("/symbol/S001").text
    assert 'aria-pressed="true"' in page and 'role="status"' in page and 'role="alert"' in page and "Show the prices as a table" in page
    client.get("/lang/bn")
    page = client.get("/symbol/S001").text
    words = json.loads(page.split('<script type="application/json" id="chart-i18n">')[1].split("</script>")[0])
    assert ">সব<" in page and words["volume"] == "ভলিউম" and words["close"] == "ক্লোজ" and words["months"][9] == "অক্টো"


def test_demo_settings_are_disabled_and_the_language_switch_stays(wide):
    tmp_path, store, db = wide[0].parts
    from dse_alerts.appdb import DEMO_EMAIL
    uid = db.ensure_user(DEMO_EMAIL)
    db.add_item(db.ensure_watchlist(uid), "S001", {}, kind="watch")
    client = TestClient(create_app(Settings(dse_data_dir=tmp_path, app_secret_key="test-secret", app_demo=True), store, db), follow_redirects=False)
    assert client.post("/demo").status_code == 303
    page = client.get("/settings").text
    assert '<fieldset class="plain" disabled>' in page and "EN / বাং" in page and 'href="/lang/bn"' in page
    r = client.get("/lang/bn", headers={"referer": "http://testserver/settings"})
    assert r.status_code == 303 and "ভাষা" in client.get("/settings").text


def test_no_javascript_delete_needs_a_second_step(wide):
    client, _ = wide
    csrf = _csrf(client)
    client.post("/portfolio/add", data={"csrf": csrf, "account": "Disposable", "symbol": "S001", "quantity": "5", "avg_cost": "10"})
    page = client.get("/portfolio").text
    acc = page.split('name="account_id" value="')[1].split('"')[0]
    r = client.post("/portfolio/remove", data={"csrf": csrf, "account_id": acc, "symbol": "S001"}, headers={"referer": "http://testserver/portfolio"})
    assert r.status_code == 200 and "Remove S001 from your holdings?" in r.text and 'href="/portfolio"' in r.text
    assert "S001" in client.get("/portfolio").text                        # cancel is a link back: nothing was deleted
    r = client.post("/accounts/delete", data={"csrf": csrf, "account_id": acc})
    assert r.status_code == 200 and "Remove the account Disposable and its 1 holdings?" in r.text
    assert "Disposable" in client.get("/settings").text


def test_one_delivery_message_when_the_site_cannot_email(wide):
    client, _ = wide
    client.post("/logout")
    client.cookies.clear()
    client.post("/register", data={"email": "two@example.com", "password": "correct horse"})
    page = client.get("/settings").text
    n = sum(page.count(s) for s in ("cannot send email", "does not send email", "not confirmed yet"))
    assert n == 1
