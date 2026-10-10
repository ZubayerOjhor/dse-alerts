"""Optional written summary of a user's alerts, produced by a language model.

The model only ever sees what the rule engine produced (headlines, details, the numbers
behind them) plus the backtest evidence for the rules that fired. It is asked to explain,
in plain language, what happened and how unusual it is for each stock, and it is told in
no uncertain terms not to advise. The output is passed through unchanged into the email
and the outbox under a "Summary" heading; the rule-based alerts stay the source of truth.

Two providers:

* ``anthropic``          the official SDK; default model Claude Fable 5.1, with server-side
                         refusal fallbacks enabled as recommended for that model
* ``openai_compatible``  any ``/chat/completions`` endpoint: Groq, OpenRouter, a local Ollama

Both are off unless configured in ``.env``. A failure returns ``None`` and the run
continues without a summary; it never blocks an alert.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from dse_alerts.config import Settings
from dse_alerts.rules import Alert

log = logging.getLogger(__name__)

SYSTEM = """You write a short daily note for one retail investor who follows a few stocks on the Dhaka Stock Exchange (DSE), Bangladesh.

You are given alerts produced by a rule-based monitor for the latest trading session, with the numbers behind each alert and, where available, how often that rule has fired historically and what typically followed.

Write 120-200 words of plain English:
- Open with one sentence on the session as a whole for this watchlist.
- Then one short paragraph per alerted stock: what happened, how unusual it is for that stock (use the baselines given), and any filing or record date that explains it. Keep the numbers.
- Close with one sentence on what the historical evidence says about such events in general, phrased as a description of the past.

Rules you must follow:
- Never advise. Do not say buy, sell, hold, accumulate, exit, wait, or any equivalent, and do not predict prices.
- Do not invent facts. If something is not in the input, do not mention it.
- No headings, no bullet points, no emojis. Plain prose. British or Bangladeshi English spelling is fine.
"""

DSE_CONTEXT = (
    "Context about the DSE that is safe to rely on: trading days are Sunday to Thursday; daily price moves are "
    "capped by slab-based circuit breakers (10% for shares under Tk 200, less for pricier shares); categories A, B "
    "and Z reflect dividend and AGM compliance, Z being the weakest; 'PSI' means price-sensitive information the "
    "company filed with the exchange."
)


def build_prompt(session: dt.date, alerts: list[Alert], symbols: list[str], evidence: dict[str, dict] | None = None) -> str:
    payload: dict[str, Any] = {
        "session": session.isoformat(),
        "watchlist": symbols,
        "alerts": [
            {"symbol": a.symbol, "rule": a.rule_id, "severity": a.severity, "headline": a.headline,
             "detail": a.detail, "numbers": a.evidence}
            for a in alerts
        ],
    }
    if evidence:
        fired = {a.rule_id for a in alerts}
        payload["rule_history"] = {
            k: {"fires_per_stock_year": round(v.get("per_250_sessions", 0), 2),
                "median_20_session_move_after": v.get("fwd20_median"),
                "share_positive_after_20_sessions": v.get("fwd20_positive_share")}
            for k, v in evidence.items() if k in fired
        }
    return DSE_CONTEXT + "\n\nInput (JSON):\n" + json.dumps(payload, default=str, indent=1)


def _via_anthropic(settings: Settings, prompt: str, api_key: str | None = None, model: str | None = None) -> str | None:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key or settings.anthropic_api_key, timeout=120.0)
    model = model or settings.anthropic_model
    kwargs: dict[str, Any] = {}
    if model.startswith(("claude-fable-5", "claude-opus-5", "claude-sonnet-5-5")):
        # server-side refusal fallbacks, recommended for these models; routes by refusal category
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    resp = client.beta.messages.create(
        model=model, max_tokens=2048, system=SYSTEM,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    if resp.stop_reason == "refusal":
        log.warning("digest refused: %s", getattr(resp, "stop_details", None))
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return text or None


def _via_openai_compatible(settings: Settings, prompt: str, transport=None, base_url: str | None = None,
                           api_key: str | None = None, model: str | None = None) -> str | None:
    import httpx

    base_url = base_url or settings.openai_compat_base_url
    api_key = settings.openai_compat_api_key if api_key is None else api_key
    model = model or settings.openai_compat_model
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if "openrouter.ai" in base_url:
        headers["HTTP-Referer"] = "https://github.com/ZubayerOjhor/dse-alerts"
        headers["X-Title"] = "dse-alerts"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 700,
    }
    with httpx.Client(timeout=120.0, transport=transport) as c:
        r = c.post(url, headers=headers, json=body)
    if r.status_code != 200:
        log.warning("digest provider returned %s: %s", r.status_code, r.text[:300])
        return None
    try:
        text = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError, TypeError):
        log.warning("digest provider returned an unexpected body")
        return None
    return (text or "").strip() or None


def write_digest(settings: Settings, session: dt.date, alerts: list[Alert], symbols: list[str],
                 evidence: dict[str, dict] | None = None) -> str | None:
    """Return the summary text, or None when AI is off, nothing fired, or the call failed."""
    if not settings.ai_enabled or not alerts:
        return None
    prompt = build_prompt(session, alerts, symbols, evidence)
    for p in settings.ai_chain():
        try:
            if p["kind"] == "anthropic":
                text = _via_anthropic(settings, prompt, api_key=p["api_key"], model=p["model"])
            else:
                text = _via_openai_compatible(settings, prompt, base_url=p["base_url"], api_key=p["api_key"], model=p["model"])
        except Exception:
            log.exception("digest via %s failed", p["name"])
            continue
        if text:
            log.info("digest written by %s (%s)", p["name"], p["model"])
            return text
        log.warning("digest via %s returned nothing; trying the next provider", p["name"])
    return None


def load_rule_evidence(settings: Settings) -> dict[str, dict]:
    """Backtest numbers per rule from data/calibration.json, if the study has been run."""
    p = settings.dse_data_dir / "calibration.json"
    if not p.exists():
        return {}
    try:
        return {e["event"]: e for e in json.loads(p.read_text(encoding="utf-8")).get("event_stats", [])}
    except (ValueError, KeyError, TypeError):
        return {}
