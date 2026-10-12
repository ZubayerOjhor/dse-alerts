"""The numbers every page depends on: adjusted returns, honest circuit detection, special
sessions, once-per-episode rules, episode scoring and the AI note's fact check."""

import datetime as dt

import pandas as pd
import pytest

from dse_alerts.analysis import features
from dse_alerts.data.store import MarketStore


def _dates(n: int, start="2025-01-01"):
    return list(pd.bdate_range(start, periods=n).date)


@pytest.fixture()
def store(tmp_path):
    s = MarketStore(tmp_path / "m.duckdb")
    dates = _dates(320)
    # ADJ: flat at 100, then a 25% bonus issue with record date on session 300: the printed
    # price drops to 80 the next session. The archive's adjusted series knows about it.
    closes = [100.0] * 300 + [80.0] * 20
    factor = [0.8] * 300 + [1.0] * 20
    s.upsert_prices(pd.DataFrame({"symbol": "ADJ", "date": dates, "open": closes, "high": closes, "low": closes,
                                  "close": closes, "volume": 1000, "adj_factor": factor}), source="archive")
    # LIM: a 10% jump on a "Price Limit Open" day, then a real upper circuit (+9.9%), then a
    # 30% jump that nothing explains
    lim = [50.0] * 300 + [55.0, 60.45, 60.45, 78.6] + [78.6] * 16
    s.upsert_prices(pd.DataFrame({"symbol": "LIM", "date": dates, "open": lim, "high": lim, "low": lim,
                                  "close": lim, "volume": 1000}), source="live")
    s.upsert_market_summary(pd.DataFrame({"date": dates, "dsex": [5000.0] * 320}))
    s.refresh_instruments()
    s.upsert_instrument_details(pd.DataFrame([
        {"symbol": "ADJ", "name": "Adjusted Co", "category": "A", "sector": "Test", "instrument_type": "Equity", "listing_year": 2010, "tradeable": True},
        {"symbol": "LIM", "name": "Limit Co", "category": "A", "sector": "Test", "instrument_type": "Equity", "listing_year": 2010, "tradeable": True},
    ]))
    with s.connect() as con:
        con.execute("INSERT INTO news VALUES ('LIM', ?, 'psi', 'LIM: Price Limit Open', '')", [dates[300]])
        con.execute("INSERT INTO dividends VALUES ('ADJ', ?, 'Dec 2024', '25% Stock Dividend', ?, ?)", [dates[290], dates[310], dates[299]])
    features.build(s)
    return s, dates


def _row(store, symbol, date):
    with store.connect(read_only=True) as con:
        return con.execute("SELECT * FROM features WHERE symbol = ? AND date = ?", [symbol, date]).df().iloc[0]


def test_bonus_issue_is_not_a_crash(store):
    s, dates = store
    ex = _row(s, "ADJ", dates[300])
    assert ex["ex_session"] and not ex["hit_lower"] and not ex["limit_break"]
    assert abs(ex["raw_ret_1"] - (-0.2)) < 1e-9                  # what the exchange printed
    assert abs(ex["ret_1"]) < 1e-9                               # what actually happened to a holder
    assert abs(ex["ret_20"]) < 1e-9 and abs(_row(s, "ADJ", dates[319])["ret_20"]) < 1e-9
    assert _row(s, "ADJ", dates[301])["low_250"] == pytest.approx(80.0)   # the range follows the adjustment


def test_circuit_hits_need_a_limit_that_applied(store):
    s, dates = store
    open_day, hit, flat, jump = (_row(s, "LIM", dates[i]) for i in (300, 301, 302, 303))
    assert open_day["no_limit"] and not open_day["hit_upper"] and abs(open_day["raw_ret_1"] - 0.10) < 1e-9
    assert hit["hit_upper"] and not hit["limit_break"]           # +9.9% on a Tk 55 share: the 10% slab, within a tick
    assert not flat["hit_upper"]
    assert jump["limit_break"] and not jump["hit_upper"]         # +30% with no notice and no record date


def test_range_and_record_rules_fire_once_per_episode():
    from dse_alerts.rules.base import RuleContext
    from dse_alerts.rules.catalog import get_rule

    dates = _dates(5)
    recent = pd.DataFrame({"symbol": "X", "date": pd.to_datetime(dates), "close": [10.0, 9.0, 8.5, 8.0, 7.5],
                           "low_250": [9.5, 9.5, 9.0, 8.5, 8.0], "high_250": [12.0] * 5, "adj_factor": [1.0] * 5,
                           "limit_break": [False] * 5})
    divs = pd.DataFrame([{"symbol": "X", "declared_on": dates[0], "year_end": "", "dividend": "10% Cash", "agm_date": None, "record_date": dates[4]}])

    def ctx(i):
        return RuleContext(row=recent.iloc[i], stats=None, instrument=None, fundamentals=None, news=pd.DataFrame(),
                           dividends=divs, recent=recent.iloc[: i + 1], as_of=dates[i])

    low = get_rule("new_250d_low").evaluate
    assert low(ctx(1), {}) is not None                            # 9.0 < 9.5: the episode starts
    assert low(ctx(2), {}) is None and low(ctx(3), {}) is None    # still below: same episode, no repeat
    rec = get_rule("record_date").evaluate
    assert rec(ctx(1), {"within_days": 5}) is not None            # dates[4] comes within reach
    assert rec(ctx(2), {"within_days": 5}) is None                # was already within reach last session


def test_news_rule_alerts_each_filing_once_and_leads_with_what_matters():
    from dse_alerts.rules.base import RuleContext
    from dse_alerts.rules.catalog import get_rule, news_rank

    dates = _dates(3)
    recent = pd.DataFrame({"symbol": "X", "date": pd.to_datetime(dates), "close": [10.0] * 3, "limit_break": [False] * 3})
    news = pd.DataFrame([
        {"symbol": "X", "date": dates[1], "kind": "psi", "title": "X: Update of Information", "body": "bank details"},
        {"symbol": "X", "date": dates[1], "kind": "psi", "title": "X: Declaration of 35% Cash Dividend", "body": "..."},
        {"symbol": "X", "date": dates[1], "kind": "psi", "title": "X: Purchase of Land", "body": "..."},
    ])
    psi = get_rule("psi_news").evaluate

    def ctx(i):
        return RuleContext(row=recent.iloc[i], stats=None, instrument=None, fundamentals=None, news=news,
                           dividends=pd.DataFrame(), recent=recent.iloc[: i + 1], as_of=dates[i])

    a = psi(ctx(1), {})
    assert a is not None and "35% Cash Dividend" in a.headline and "(+2 more)" in a.headline and a.severity == "notable"
    assert psi(ctx(2), {}) is None                                # the next session does not repeat it
    assert news_rank("X: Price Limit Open") > news_rank("X: Purchase of Land") > news_rank("X: Declaration of Dividend")


def test_scorecard_counts_episodes_not_repeats():
    from dse_alerts import scorecard as sc

    rows = pd.DataFrame({
        "symbol": ["ORION"] * 4 + ["GP"], "rule_id": ["new_250d_low"] * 5, "severity": ["notable"] * 5,
        "session_date": ["2025-03-02", "2025-03-03", "2025-03-04", "2025-05-01", "2025-03-03"],
        "headline": ["h"] * 5, "close": [1.0] * 5, "fwd_5": [0.1, 0.1, 0.1, 0.2, -0.1], "fwd_20": [0.2, 0.2, 0.2, 0.3, -0.1],
        "dsex_fwd_5": [0.0] * 5, "dsex_fwd_20": [0.0] * 5, "excess_20": [0.2, 0.2, 0.2, 0.3, -0.1], "status": ["scored"] * 5,
    })
    rows = rows.sort_values(["symbol", "rule_id", "session_date"])
    when = pd.to_datetime(rows["session_date"])
    gap = when.groupby([rows["symbol"], rows["rule_id"]]).diff().dt.days
    rows["episode_start"] = gap.isna() | (gap > sc.EPISODE_GAP_DAYS)
    agg = sc.by_rule(rows, {"new_250d_low": {"fwd20_median": 0.006}})
    r = agg.iloc[0]
    assert r["alerts"] == 5 and r["episodes"] == 3 and r["scored"] == 3        # three runs, not five alerts
    assert r["fwd20_median"] == pytest.approx(0.2) and not r["enough"]        # far from the 30 needed
    assert r["backtest_fwd20_median"] == pytest.approx(0.006)


def test_ai_note_is_checked_against_its_input():
    from dse_alerts.ai import digest as ai
    from dse_alerts.rules import Alert

    alerts = [Alert(symbol="WALTONHIL", rule_id="new_250d_low", date=dt.date(2026, 10, 8), headline="WALTONHIL closed at a 250-session low: 331.1",
                    detail="Previous 250-session low was 332.5.", severity="notable", evidence={"close": 331.1, "previous_extreme": 332.5})]
    prompt = ai.build_prompt(dt.date(2026, 10, 8), alerts, ["WALTONHIL"], {"new_250d_low": {"n": 9088, "per_250_sessions": 2.33, "fwd20_median": 0.006, "fwd20_positive_share": 0.51}})
    assert "rule_history" in prompt and "+0.6%" in prompt
    ok = "WALTONHIL closed at 331.1, a 250-session low, below the previous low of 332.5. Over 2013 to 2026 this fired 2.3 times a year per stock and the median move 20 sessions later was +0.6%, with 51% rising."
    assert ai.check_note(ok, prompt) == []
    bad = "Walton has not broken a 250-day trough in the past year and this usually precedes a short-term rebound; it appeared three times, 3 in total."
    problems = ai.check_note(bad, prompt)
    assert any("usually" in p for p in problems) and any("rebound" in p for p in problems) and any("number 3 " in p for p in problems)
    # without evidence the prompt carries no history section at all
    assert "rule_history" not in ai.build_prompt(dt.date(2026, 10, 8), alerts, ["WALTONHIL"], {})


def test_rejected_notes_are_retried_once_then_dropped(monkeypatch):
    from dse_alerts import config
    from dse_alerts.ai import digest as ai
    from dse_alerts.rules import Alert

    alerts = [Alert(symbol="GP", rule_id="big_rise", date=dt.date(2026, 10, 8), headline="GP up 9.8% over 10 sessions", detail="Close 300.0.",
                    severity="notable", evidence={"ret": 0.098, "days": 10, "close": 300.0})]
    calls = []

    def fake(settings, prompt, **kw):
        calls.append(prompt)
        return "GP rose 9.8% to 300.0; such moves usually precede a rebound of 15%." if len(calls) == 1 else "GP rose 9.8% over 10 sessions to close at 300.0."

    monkeypatch.setattr(config.Settings, "ai_enabled", property(lambda self: True))
    monkeypatch.setattr(config.Settings, "ai_chain", lambda self: [{"name": "groq", "kind": "openai_compatible", "base_url": "x", "api_key": "k", "model": "m"}])
    monkeypatch.setattr(ai, "_via_openai_compatible", fake)
    text, provider = ai.write_digest_detailed(config.Settings(app_secret_key="t"), dt.date(2026, 10, 8), alerts, ["GP"], {})
    assert text == "GP rose 9.8% over 10 sessions to close at 300.0." and provider == "groq:m" and len(calls) == 2
    assert "rejected" in calls[1]
    calls.clear()
    monkeypatch.setattr(ai, "_via_openai_compatible", lambda settings, prompt, **kw: calls.append(prompt) or "It will likely rebound 20%.")
    assert ai.write_digest_detailed(config.Settings(app_secret_key="t"), dt.date(2026, 10, 8), alerts, ["GP"], {}) == (None, None)
    assert len(calls) == 2


def test_web_reads_the_published_snapshot(tmp_path):
    import pandas as pd

    writer = MarketStore(tmp_path / "m.duckdb")
    dates = _dates(3)
    writer.upsert_prices(pd.DataFrame({"symbol": "GP", "date": dates, "open": 1.0, "high": 1.0, "low": 1.0, "close": [1.0, 2.0, 3.0], "volume": 1}), source="live")
    reader = MarketStore(tmp_path / "m.duckdb", prefer_snapshot=True)
    assert reader.summary()["rows"] == 3                      # no snapshot yet: the live file
    writer.snapshot()
    writer.upsert_prices(pd.DataFrame({"symbol": "GP", "date": _dates(2, "2025-02-01"), "open": 1.0, "high": 1.0, "low": 1.0, "close": 4.0, "volume": 1}), source="live")
    assert writer.summary()["rows"] == 5 and reader.summary()["rows"] == 3   # readers see the published copy
    writer.snapshot()
    assert reader.summary()["rows"] == 5
