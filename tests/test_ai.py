import datetime as dt
import json

import httpx

from dse_alerts.ai import digest
from dse_alerts.config import Settings
from dse_alerts.rules import Alert

ALERTS = [
    Alert("BEXIMCO", "upper_circuit", dt.date(2026, 10, 8), "BEXIMCO closed at its upper circuit (+9.8%)",
          "Daily limit 10%.", "notable", {"ret_1": 0.098, "streak": 1}),
    Alert("RENATA", "psi_news", dt.date(2026, 10, 8), "RENATA filed price-sensitive information", "Board meeting moved.",
          "notable", {"count": 1}),
]


def test_prompt_contains_alerts_and_only_fired_rules_evidence():
    ev = {"upper_circuit": {"per_250_sessions": 2.36, "fwd20_median": -0.007, "fwd20_positive_share": 0.47},
          "lower_circuit": {"per_250_sessions": 0.69}}
    p = digest.build_prompt(dt.date(2026, 10, 8), ALERTS, ["GP", "BEXIMCO", "RENATA"], ev)
    data = json.loads(p.split("Input (JSON):\n", 1)[1])
    assert [a["symbol"] for a in data["alerts"]] == ["BEXIMCO", "RENATA"]
    assert "upper_circuit" in data["rule_history"] and "lower_circuit" not in data["rule_history"]
    assert "never advise" in digest.SYSTEM.lower()


def test_off_when_not_configured_or_nothing_fired():
    assert digest.write_digest(Settings(), dt.date(2026, 10, 8), ALERTS, ["GP"]) is None
    s = Settings(ai_provider="openai_compatible", openai_compat_api_key="k")
    assert digest.write_digest(s, dt.date(2026, 10, 8), [], ["GP"]) is None


def test_openai_compatible_roundtrip_and_failure_handling():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        if seen["body"]["model"] == "broken":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"choices": [{"message": {"content": "  A calm session overall.  "}}]})

    s = Settings(ai_provider="openai_compatible", openai_compat_api_key="gsk_test", openai_compat_model="llama-3.3-70b-versatile")
    prompt = digest.build_prompt(dt.date(2026, 10, 8), ALERTS, ["GP"])
    out = digest._via_openai_compatible(s, prompt, transport=httpx.MockTransport(handler))
    assert out == "A calm session overall."
    assert seen["auth"] == "Bearer gsk_test" and seen["body"]["messages"][0]["role"] == "system"

    s2 = Settings(ai_provider="openai_compatible", openai_compat_api_key="gsk_test", openai_compat_model="broken")
    assert digest._via_openai_compatible(s2, prompt, transport=httpx.MockTransport(handler)) is None


def test_anthropic_path_uses_sdk_and_handles_refusal(monkeypatch):
    calls = {}

    class _Block:
        type = "text"
        text = "Two alerts today."

    class _Resp:
        def __init__(self, stop):
            self.stop_reason = stop
            self.content = [_Block()]
            self.stop_details = None

    class _Messages:
        def create(self, **kw):
            calls.update(kw)
            return _Resp("refusal" if kw["model"] == "refuse-me" else "end_turn")

    class _Beta:
        messages = _Messages()

    class _Client:
        def __init__(self, **kw):
            calls["client_kwargs"] = kw
            self.beta = _Beta()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    s = Settings(ai_provider="anthropic", anthropic_api_key="sk-test", anthropic_model="claude-fable-5-1")
    out = digest.write_digest(s, dt.date(2026, 10, 8), ALERTS, ["GP"])
    assert out == "Two alerts today."
    assert calls["model"] == "claude-fable-5-1" and calls["fallbacks"] == "default"
    assert calls["betas"] == ["server-side-fallback-2026-07-01"] and calls["system"] == digest.SYSTEM

    calls.clear()
    s2 = Settings(ai_provider="anthropic", anthropic_api_key="sk-test", anthropic_model="refuse-me")
    assert digest.write_digest(s2, dt.date(2026, 10, 8), ALERTS, ["GP"]) is None   # refusal -> no summary
    assert "fallbacks" not in calls                                                # only sent for models that support it
