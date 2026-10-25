"""The DSE website refuses the scraping library's User-Agent; the shared session is tuned before any call."""

from dse_alerts.data import live


def test_session_gets_a_browser_agent_and_a_timeout_floor(monkeypatch):
    from bdshare.util import helper

    s = helper._session
    monkeypatch.setattr(s, "_dse_alerts_tuned", False, raising=False)
    seen = {}

    def fake_request(method, url, **kw):
        seen.update(kw)
        return "resp"
    monkeypatch.setattr(s, "request", fake_request)
    live._tune_session()
    assert s.headers["User-Agent"].startswith("Mozilla/5.0") and "bdshare" not in s.headers["User-Agent"]
    assert s.request("GET", "https://dse.com.bd/x", timeout=10) == "resp" and seen["timeout"] == live.MIN_TIMEOUT
    s.request("GET", "https://dse.com.bd/x", timeout=120)
    assert seen["timeout"] == 120                       # a longer timeout from the library is kept
    tuned = s.request
    live._tune_session()
    assert s.request is tuned                           # idempotent: no double wrapping
