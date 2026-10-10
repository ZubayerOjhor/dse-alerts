import datetime as dt

from dse_alerts.appdb import AppDB
from dse_alerts.rules import Alert


def test_users_watchlists_and_alert_dedup(tmp_path):
    db = AppDB(tmp_path / "app.sqlite")
    uid = db.ensure_user("a@example.com", alert_email="alerts@example.com")
    assert db.ensure_user("a@example.com") == uid
    wl = db.ensure_watchlist(uid)
    db.add_item(wl, "gp ", {"big_drop": {"pct": 0.05}})
    db.add_item(wl, "GP", {"big_drop": {"pct": 0.07}})          # update, not duplicate
    db.add_item(wl, "ROBI")
    items = db.items(uid)
    assert [i["symbol"] for i in items] == ["GP", "ROBI"]
    assert items[0]["rules"] == {"big_drop": {"pct": 0.07}}

    a = Alert("GP", "big_drop", dt.date(2026, 10, 8), "GP down", "detail", "notable", {"ret": -0.1})
    assert len(db.record_alerts(uid, [a])) == 1
    assert db.record_alerts(uid, [a]) == []                       # same alert again is not new
    db.mark_delivered(uid, [a])
    hist = db.alert_history(uid)
    assert len(hist) == 1 and hist[0]["delivered_at"] is not None

    db.remove_item(wl, "GP")
    assert [i["symbol"] for i in db.items(uid)] == ["ROBI"]

    rid = db.start_run()
    db.finish_run(rid, session_date="2026-10-08", users=1, symbols=1, alerts_new=1, alerts_sent=0, status="ok")
    assert db.last_runs(1)[0]["status"] == "ok"
