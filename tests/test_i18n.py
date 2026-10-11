import datetime as dt
import re

import pytest

from dse_alerts import i18n
from dse_alerts.i18n.alerts import localize
from dse_alerts.notify import deliver, render
from dse_alerts.notify.context import DeliveryContext
from dse_alerts.rules import Alert

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_bangla_catalog_is_complete_and_placeholders_match():
    assert i18n.missing_keys("bn") == []
    en, bn = i18n.catalog("en"), i18n.catalog("bn")
    for key, s in en.items():
        assert set(PLACEHOLDER.findall(s)) == set(PLACEHOLDER.findall(bn[key])), key
    extra = set(bn) - set(en)
    assert not extra, f"bn has keys en lacks: {sorted(extra)}"


def test_t_falls_back_and_formats():
    assert i18n.t("bn", "nav.overview") == "সারসংক্ষেপ"
    assert i18n.t("xx", "nav.overview") == "Overview"            # unknown language -> English
    assert i18n.t("en", "no.such.key") == "no.such.key"            # unknown key -> the key
    assert i18n.t("en", "dash.alerts_many", n=3) == "3 alerts this session"
    assert i18n.plural("bn", 1, "email.n_alerts_one", "email.n_alerts_many") == "১টি অ্যালার্ট"
    assert i18n.normalize(" BN ") == "bn" and i18n.normalize(None) == "en"


EVIDENCE = {
    "big_drop": {"ret": -0.12, "threshold": -0.108, "days": 10, "close": 242.0},
    "big_rise": {"ret": 0.15, "threshold": 0.1, "days": 20, "close": 50.0},
    "upper_circuit": {"ret_1": 0.098, "streak": 2, "slab_limit": 0.1, "hits_per_year": 2.4},
    "lower_circuit": {"ret_1": -0.03, "streak": 1, "slab_limit": 0.03, "hits_per_year": None},
    "volume_spike": {"vol_ratio": 4.2, "ret_1": 0.05, "threshold": 3.0},
    "new_250d_high": {"close": 300.0, "previous_extreme": 295.0},
    "new_250d_low": {"close": 20.0, "previous_extreme": 21.0},
    "vs_index": {"ret_1": 0.06, "dsex_ret_1": 0.001, "diff": 0.059},
    "psi_news": {"count": 2, "titles": ["Board meeting", "Dividend"]},
    "record_date": {"record_date": "2026-10-20", "dividend": "10% cash"},
    "ma_cross": {"close": 100.0, "sma_50": 98.0, "direction": 1},
    "pe_level": {"pe": 8.5, "min": None, "max": 10},
    "illiquid": {"zero_volume_sessions": 6},
}


@pytest.mark.parametrize("rule_id", sorted(EVIDENCE))
def test_every_rule_has_a_bangla_headline(rule_id):
    head, det = localize(rule_id, "GP", EVIDENCE[rule_id], "english headline", "english detail", "bn")
    assert head != "english headline" and "GP" in head
    assert re.search(r"[ঀ-৿]", head), head        # contains Bangla script
    assert det                                               # never empty


def test_localize_passes_through_for_english_and_broken_evidence():
    assert localize("big_drop", "GP", EVIDENCE["big_drop"], "h", "d", "en") == ("h", "d")
    assert localize("big_drop", "GP", {}, "h", "d", "bn") == ("h", "d")           # missing fields -> stored text
    assert localize("big_drop", "GP", "not json", "h", "d", "bn") == ("h", "d")
    assert localize("unknown_rule", "GP", {"x": 1}, "h", "d", "bn") == ("h", "d")
    # evidence_json strings from the database work too
    head, _ = localize("illiquid", "GP", '{"zero_volume_sessions": 6}', "h", "d", "bn")
    assert "6" in head and "GP" in head


def test_bangla_email_and_telegram():
    alerts = [Alert("GP", "big_drop", dt.date(2026, 10, 8), "GP down 12% over 10 sessions", "Threshold.", "high", EVIDENCE["big_drop"])]
    ctx = DeliveryContext(session=dt.date(2026, 10, 8), alerts=alerts, symbols=["GP"], digest=None,
                          index={"close": 5475.58, "ret_1": 0.0021, "ret_20": -0.013}, lang="bn")
    assert render.subject(ctx.session, alerts, "bn").startswith("DSE অ্যালার্ট 2026-10-08")
    txt = render.text(ctx)
    assert "সেশনে" in txt and "GP" in txt and "-12.0%" in txt and i18n.t("bn", "email.disclaimer") in txt
    html = render.html_body(ctx)
    assert "কী ঘটেছে" in html and "GP 10 সেশনে -12.0% কমেছে" in html
    tg = deliver.telegram_text(ctx.session, alerts, None, ctx)
    assert tg.startswith("DSE অ্যালার্ট") and tg.rstrip().endswith("পর্যবেক্ষণ, পরামর্শ নয়।")
    # English unchanged
    assert render.subject(ctx.session, alerts) == "DSE alerts 2026-10-08: 1 alert, 1 high (GP)"
