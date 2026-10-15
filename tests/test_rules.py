import datetime as dt

import pandas as pd

from dse_alerts.rules import RULES, RuleContext, get_rule

AS_OF = dt.date(2026, 10, 8)


def _ctx(row: dict, stats: dict | None = None, fundamentals: dict | None = None,
         news: list[dict] | None = None, dividends: list[dict] | None = None, prev_row: dict | None = None) -> RuleContext:
    base = {
        "symbol": "GP", "date": pd.Timestamp(AS_OF), "close": 240.0, "prev_close": 241.0, "volume": 100_000,
        "ret_1": -0.004, "ret_5": -0.01, "ret_10": -0.02, "ret_20": -0.03, "vol_ratio_20": 1.0,
        "slab_limit": 0.0875, "hit_upper": False, "hit_lower": False, "upper_streak": 0, "lower_streak": 0,
        "high_250": 300.0, "low_250": 200.0, "sma_20": 241.0, "sma_50": 245.0, "sma50_cross": 0,
        "zero_vol_10": 0, "dsex_ret_1": 0.001, "ret_5_prev": -0.01, "ret_10_prev": -0.02, "ret_20_prev": -0.03,
    }
    latest = pd.Series({**base, **row})
    rows = [pd.Series({**base, **(prev_row or {})}), latest]
    recent = pd.DataFrame(rows)
    return RuleContext(
        row=latest,
        stats=pd.Series(stats) if stats else None,
        instrument=None,
        fundamentals=pd.Series(fundamentals) if fundamentals else None,
        news=pd.DataFrame(news or [], columns=["symbol", "date", "kind", "title", "body"]),
        dividends=pd.DataFrame(dividends or [], columns=["symbol", "declared_on", "year_end", "dividend", "agm_date", "record_date"]),
        recent=recent,
        as_of=AS_OF,
    )


STATS = {"ret10_p05": -0.108, "ret10_p95": 0.141, "ret20_p05": -0.15, "ret20_p95": 0.2,
         "abs_ret1_p95": 0.05, "vol_ratio_p95": 4.0, "upper_hits_per_250": 1.9, "lower_hits_per_250": 0.5}


def test_catalog_ids_unique():
    ids = [r.id for r in RULES]
    assert len(ids) == len(set(ids))


def test_big_drop_uses_stock_baseline_by_default():
    r = get_rule("big_drop")
    assert r.run(_ctx({"ret_10": -0.05}, STATS)) is None            # -5% is normal for this stock
    a = r.run(_ctx({"ret_10": -0.12}, STATS))
    assert a and a.severity == "notable" and "-12.0%" in a.headline
    a = r.run(_ctx({"ret_10": -0.20}, STATS))
    assert a.severity == "high"


def test_big_drop_fires_once_per_episode():
    r = get_rule("big_drop")
    assert r.run(_ctx({"ret_10": -0.12, "ret_10_prev": -0.09}, STATS))             # crossed today
    assert r.run(_ctx({"ret_10": -0.12, "ret_10_prev": -0.115}, STATS)) is None    # already below yesterday
    assert r.run(_ctx({"ret_10": -0.12}, STATS))                                   # no history: fire


def test_big_drop_absolute_override_wins():
    r = get_rule("big_drop")
    a = r.run(_ctx({"ret_10": -0.06}, STATS), {"pct": 0.05})
    assert a and "your setting of 5%" in a.detail
    assert r.run(_ctx({"ret_5": -0.02}, STATS), {"days": 5}) is None


def test_big_drop_without_stats_and_without_pct_is_silent():
    assert get_rule("big_drop").run(_ctx({"ret_10": -0.3})) is None


def test_circuit_streak_and_severity():
    r = get_rule("upper_circuit")
    assert r.run(_ctx({"hit_upper": False}, STATS)) is None
    a = r.run(_ctx({"hit_upper": True, "upper_streak": 1, "ret_1": 0.087}, STATS))
    assert a and a.severity == "notable" and "upper circuit" in a.headline
    a = r.run(_ctx({"hit_upper": True, "upper_streak": 3, "ret_1": 0.087}, STATS))
    assert a.severity == "high" and "3rd session in a row" in a.headline
    assert r.run(_ctx({"hit_upper": True, "upper_streak": 2}, STATS), {"streak": 3}) is None


def test_volume_spike_threshold_is_max_of_default_and_stock_p95():
    r = get_rule("volume_spike")
    assert r.run(_ctx({"vol_ratio_20": 3.5, "ret_1": 0.04}, STATS)) is None   # stock p95 is 4x
    a = r.run(_ctx({"vol_ratio_20": 4.5, "ret_1": 0.04}, STATS))
    assert a and "4.5x" in a.headline
    assert r.run(_ctx({"vol_ratio_20": 4.5, "ret_1": 0.01}, STATS)) is None   # move too small
    assert r.run(_ctx({"vol_ratio_20": 3.5, "ret_1": 0.04}, STATS), {"use_stock_baseline": False})


def test_range_break_and_vs_index():
    assert get_rule("new_250d_high").run(_ctx({"close": 301.0}, STATS)).headline.endswith("301.0")
    assert get_rule("new_250d_low").run(_ctx({"close": 199.0}, STATS))
    assert get_rule("new_250d_high").run(_ctx({"close": 250.0}, STATS)) is None
    a = get_rule("vs_index").run(_ctx({"ret_1": 0.06, "dsex_ret_1": 0.0}, STATS))
    assert a and "DSEX" in a.headline
    assert get_rule("vs_index").run(_ctx({"ret_1": 0.02}, STATS)) is None


def test_psi_news_and_record_date():
    news = [{"symbol": "GP", "date": AS_OF - dt.timedelta(days=1), "kind": "psi", "title": "Q3 results", "body": "..."}]
    a = get_rule("psi_news").run(_ctx({}, STATS, news=news))
    assert a and "Q3 results" in a.headline
    old = [{**news[0], "date": AS_OF - dt.timedelta(days=10)}]
    assert get_rule("psi_news").run(_ctx({}, STATS, news=old)) is None

    div = [{"symbol": "GP", "declared_on": AS_OF, "year_end": "2026", "dividend": "20% cash",
            "agm_date": AS_OF + dt.timedelta(days=40), "record_date": AS_OF + dt.timedelta(days=2)}]
    rec = get_rule("record_date").run(_ctx({}, STATS, dividends=div))
    assert rec and "00:00" not in rec.headline and "-" not in rec.headline.split(":")[0]   # "20 Sep 2026", not 2026-09-20
    from dse_alerts.i18n import fmt_date
    from dse_alerts.i18n.alerts import localize
    assert fmt_date("en", rec.evidence["record_date"]) in rec.headline
    bn_head, bn_det = localize("record_date", rec.symbol, rec.evidence, rec.headline, rec.detail, "bn")
    assert fmt_date("bn", rec.evidence["record_date"]) in bn_head and fmt_date("bn", rec.evidence["agm_date"]) in bn_det
    assert get_rule("record_date").run(_ctx({}, STATS, dividends=div), {"within_days": 1}) is None


def test_ma_cross_pe_and_illiquid_fire_on_state_change():
    assert get_rule("ma_cross").run(_ctx({"sma50_cross": 1}, STATS)).headline.endswith("above its 50-session average")
    assert get_rule("ma_cross").run(_ctx({"sma50_cross": 1}, STATS), {"direction": "below"}) is None

    pe = get_rule("pe_level")
    assert pe.run(_ctx({}, STATS, fundamentals={"pe": 9.0, "prev_pe": 12.0}), {"max": 10}) is not None
    assert pe.run(_ctx({}, STATS, fundamentals={"pe": 9.0, "prev_pe": 9.5}), {"max": 10}) is None   # unchanged state
    assert pe.run(_ctx({}, STATS, fundamentals={"pe": 9.0}), {}) is None                              # nothing configured

    il = get_rule("illiquid")
    assert il.run(_ctx({"zero_vol_10": 6}, STATS, prev_row={"zero_vol_10": 4}))
    assert il.run(_ctx({"zero_vol_10": 6}, STATS, prev_row={"zero_vol_10": 6})) is None
