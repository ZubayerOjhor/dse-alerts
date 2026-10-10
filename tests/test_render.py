import datetime as dt

from dse_alerts.notify import render
from dse_alerts.notify.context import DeliveryContext
from dse_alerts.rules import Alert

A = [Alert("BEXIMCO", "upper_circuit", dt.date(2026, 10, 8), "BEXIMCO closed at its upper circuit (+9.8%)", "Daily limit 10%.", "notable", {}),
     Alert("GP", "big_drop", dt.date(2026, 10, 8), "GP down 12% over 10 sessions", "Threshold -10.8%.", "high", {})]


def _ctx(**kw):
    base = {
        "session": dt.date(2026, 10, 8), "alerts": A, "symbols": ["GP", "BEXIMCO", "ROBI"], "digest": "A calm session <b>overall</b>.",
        "index": {"close": 5475.58, "ret_1": 0.0021, "ret_20": -0.013},
        "watch_rows": [{"symbol": "GP", "name": "Grameenphone", "kind": "owned", "close": 242.0, "ret_1": -0.0025, "ret_20": -0.02},
                       {"symbol": "ROBI", "name": "Robi", "kind": "target", "close": 29.9, "ret_1": 0.01, "ret_20": 0.05}],
        "portfolio": {"value": 142418.0, "cost": 135673.0, "pnl": 6745.0, "pnl_pct": 0.0497, "day_change": 186.0, "day_change_pct": 0.0013,
                      "positions": 55, "symbols": 24, "statement_value": 120412.0, "as_of": dt.date(2026, 10, 8),
                      "best": [{"symbol": "ROBI", "day_change": 300.0, "ret_1": 0.01}], "worst": [{"symbol": "GP", "day_change": -13.0, "ret_1": -0.0025}]},
        "base_url": "http://127.0.0.1:8000",
    }
    base.update(kw)
    return DeliveryContext(**base)


def test_subject_and_text():
    assert render.subject(dt.date(2026, 10, 8), A) == "DSE alerts 2026-10-08: 2 alerts, 1 high (BEXIMCO, GP)"
    assert render.subject(dt.date(2026, 10, 8), []) == "DSE alerts 2026-10-08: nothing fired"
    t = render.text(_ctx())
    assert "[HIGH] GP down 12%" in t and "DSEX 5,475.58" in t and "Value Tk 142,418" in t and "GP" in t
    assert t.index("[HIGH]") < t.index("[NOTABLE]")              # high severity first
    assert render.DISCLAIMER in t


def test_html_is_escaped_and_complete():
    html = render.html_body(_ctx())
    assert "&lt;b&gt;overall&lt;/b&gt;" in html and "<b>overall</b>" not in html   # digest is escaped
    assert "DSE Alerts" in html and "Session 2026-10-08" in html and "5,475.58" in html
    assert "Tk 142,418" in html and "+6,745" in html and "Portfolio movers today" in html
    assert "Grameenphone" in html and "target" in html and "Open the dashboard" in html
    assert render.DISCLAIMER in html and html.count("rule:") == 2


def test_html_without_portfolio_or_alerts():
    html = render.html_body(_ctx(alerts=[], portfolio=None, digest=None, watch_rows=[]))
    assert "Nothing fired for your 3 watched symbols" in html and "Portfolio movers" not in html
    assert "Watched" in html
