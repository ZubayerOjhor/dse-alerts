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
import re
from typing import Any

from dse_alerts.config import Settings
from dse_alerts.rules import Alert

log = logging.getLogger(__name__)

SYSTEM = """You write a short daily note for one retail investor who follows a few stocks on the Dhaka Stock Exchange (DSE), Bangladesh.

You are given alerts produced by a rule-based monitor for the latest trading session, with the numbers behind each alert and, where available, how often that rule has fired historically and what typically followed.

Write 120-200 words of plain English:
- Open with one sentence on the session as a whole for this watchlist.
- Then one short paragraph per alerted stock: what happened, how unusual it is for that stock (use the baselines given), and any filing or record date that explains it. Keep the numbers.
- If the input has a rule_history section, close with one sentence that repeats its figures for the rules that fired, as a description of the past ("over 2013 to 2026, ..."), and give the any-day figure next to the share that rose so the reader has the comparison. If there is no rule_history section, do not mention history at all.

Rules you must follow:
- Never advise. Do not say buy, sell, hold, accumulate, exit, wait, or any equivalent, and do not predict prices.
- Never describe what such moves usually lead to, precede, signal or are followed by. No "rebound", "reversal", "momentum", "oversold", "support" or "resistance".
- Do not invent facts. Every number you write must appear in the input. If something is not in the input, do not mention it; do not count or date past events yourself. Say nothing about stocks that have no alert, beyond that no rule fired for them; you do not know how they traded.
- Write the session as the day given in the input (for example "on Sunday 12 October"). Do not repeat background about the exchange, such as which weekdays it trades.
- Never read field names back. Write what a value means ("one filing", "a 9.8% rise"), not "count of 1" or "the headline says".
- No headings, no bullet points, no emojis. Plain prose. British or Bangladeshi English spelling is fine.
- Plain punctuation only: commas, full stops and ordinary hyphens. No dashes, no arrows, no symbols. Write percentages as 9.8%, with no space before the sign.
- Explain any term a first-time investor may not know in a few words the first time it appears (for example "upper circuit, the daily price limit").
"""

LANG_NOTE = {
    "bn": ("\nWrite the whole note in Bangla (বাংলা), in the plain register of a Bangladeshi business newspaper. "
           "Keep trading codes (e.g. GP, BRACBANK), the word DSEX and all numbers in Latin script exactly as given."),
}


def system_prompt(lang: str = "en") -> str:
    return SYSTEM + LANG_NOTE.get(lang or "en", "")


DSE_CONTEXT = (
    "Background for your own understanding, not to be repeated: on the DSE daily price moves are capped by "
    "slab-based circuit breakers (10% for shares under Tk 200, less for pricier shares); categories A, B and Z "
    "reflect dividend and AGM compliance, Z being the weakest; 'PSI' means price-sensitive information the "
    "company filed with the exchange."
)

# evidence keys as words, so the note never has to guess what a field means
_LABELS = {
    "ret_1": "move on the day", "ret": "move over the period", "days": "sessions in the period", "close": "close",
    "streak": "sessions in a row at the limit", "slab_limit": "daily limit", "hits_per_year": "times a year this stock hits the limit",
    "count": "filings since the previous session", "titles": "filing titles", "previous_extreme": "previous 250-session extreme",
    "ratio": "volume versus the 20-session average", "vol_ratio": "volume versus the 20-session average",
    "threshold": "this stock's usual limit for such a move", "pct": "move", "record_date": "record date",
    "dividend": "dividend", "diff": "difference from the index", "dsex_ret_1": "index move on the day",
    "pe": "P/E", "zero_volume_sessions": "sessions with no trades", "ref": "reference level", "factor": "adjustment factor",
}


def _words(ev: dict | None) -> dict:
    out = {}
    for k, v in (ev or {}).items():
        label = _LABELS.get(k, str(k).replace("_", " "))
        if isinstance(v, float) and abs(v) < 1 and k not in ("factor", "pe") and not k.endswith("close"):
            v = f"{100 * v:+.1f}%"          # returns arrive as fractions; the note should see percentages
        out[label] = v
    return out


def notable(alerts: list[Alert]) -> bool:
    """A note is worth writing only when something happened: a routine filing or an "info" alert
    on its own gives the model nothing to describe, and it will pad."""
    return any(a.severity in ("notable", "high") for a in alerts)


def build_prompt(session: dt.date, alerts: list[Alert], symbols: list[str], evidence: dict[str, dict] | None = None) -> str:
    payload: dict[str, Any] = {
        "session": session.isoformat(),
        "session_day": session.strftime("%A %d %B %Y").replace(" 0", " "),
        "watchlist": symbols,
        "stocks_with_no_alert": sorted(set(symbols) - {a.symbol for a in alerts}),
        "alerts": [
            {"symbol": a.symbol, "rule": a.rule_id, "severity": a.severity, "headline": a.headline,
             "detail": a.detail, "numbers": _words(a.evidence)}
            for a in alerts
        ],
    }
    if evidence:
        fired = {a.rule_id for a in alerts}
        history = {
            k: {"fires_per_stock_year": round(v.get("per_250_sessions") or 0, 1),
                "median_move_20_sessions_later": f"{100 * (v.get('fwd20_median') or 0):+.1f}%",
                "share_that_rose_20_sessions_later": f"{round(100 * (v.get('fwd20_positive_share') or 0))}%",
                "share_that_rose_20_sessions_after_any_day": f"{round(100 * (v.get('base_fwd20_positive_share') or 0))}%"
                if v.get("base_fwd20_positive_share") is not None else "not available",
                "study": "2013 to 2026, every DSE stock"}
            for k, v in evidence.items() if k in fired and (v.get("n") or v.get("fwd20_median") is not None or v.get("per_250_sessions"))
        }
        if history:
            payload["rule_history"] = history
    return DSE_CONTEXT + "\n\nInput (JSON):\n" + json.dumps(payload, default=str, indent=1)


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_FORBIDDEN = re.compile(
    r"\b(usually|typically|tends? to|often (precedes|follows|leads)|precede[sd]?|signal(s|led|ling)?|rebound|reversal|"
    r"momentum|oversold|overbought|bullish|bearish|support level|resistance|likely to|expect(ed|s)?|"
    r"should (rise|fall|recover)|will (rise|fall|recover)|buy|sell|hold|accumulate|"
    r"Sunday to Thursday|Sunday-Thursday|within (its|their) (usual|normal) (daily )?limits?|"
    r"(the )?(count|headline|detail|rule|severity|field) (of|says|reads|is)|a count of)\b", re.IGNORECASE)
_RETRY_NOTE = ("\n\nYour previous draft was rejected for these reasons: {reasons}. Write it again using only numbers "
               "that appear in the input and without describing what such moves usually lead to.")


def input_numbers(prompt: str) -> set[str]:
    """Every number in the prompt, plus the same values as percentages (0.098 -> 9.8, 10) and
    rounded, so a note may say 9.8% or 10% for a 0.0981 return without being called invented."""
    out: set[str] = set()
    for m in _NUMBER.finditer(prompt):
        raw = m.group(0).replace(",", "")
        out.add(raw)
        try:
            v = float(raw)
        except ValueError:
            continue
        for x in (v, v * 100):
            out.update({f"{x:.0f}", f"{x:.1f}", f"{x:.2f}", f"{abs(x):.0f}", f"{abs(x):.1f}", f"{abs(x):.2f}"})
    return out


def check_note(text: str, prompt: str) -> list[str]:
    """Problems with a note: numbers that are not in the input, and phrases that describe what
    usually follows (a prediction in disguise). An empty list means it passes."""
    allowed = input_numbers(prompt)
    problems = []
    for m in _NUMBER.finditer(text):
        n = m.group(0).replace(",", "")
        if len(n) >= 4 and "." not in n:   # years and dates are checked like any other number
            pass
        if n not in allowed and n.rstrip("0").rstrip(".") not in allowed:
            problems.append(f"number {n} is not in the input")
    for m in _FORBIDDEN.finditer(text):
        problems.append(f"phrase '{m.group(0)}'")
    return problems


def _via_anthropic(settings: Settings, prompt: str, api_key: str | None = None, model: str | None = None,
                   lang: str = "en") -> str | None:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key or settings.anthropic_api_key, timeout=120.0)
    model = model or settings.anthropic_model
    kwargs: dict[str, Any] = {}
    if model.startswith(("claude-fable-5", "claude-opus-5", "claude-sonnet-5-5")):
        # server-side refusal fallbacks, recommended for these models; routes by refusal category
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    resp = client.beta.messages.create(
        model=model, max_tokens=2048, system=system_prompt(lang),
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
                           api_key: str | None = None, model: str | None = None, lang: str = "en") -> str | None:
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
        "messages": [{"role": "system", "content": system_prompt(lang)}, {"role": "user", "content": prompt}],
        "temperature": 0.3,
        # Bangla tokenises longer; reasoning models (gpt-oss) also spend completion tokens on
        # hidden thinking, so the budget is generous and their effort is kept low.
        "max_tokens": 2500 if lang == "bn" else 2000,
    }
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
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


_TIDY = {
    "\u2014": ", ", "\u2013": ", ", "\u2012": "-", "\u2011": "-", "\u2010": "-", "\u2212": "-",
    "\u202f": " ", "\u00a0": " ", "\u2009": " ", "\u2026": "...", "\u2192": " to ", "\u2022": "",
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2726": "", "\u00d7": "x",
}


_BENGALI_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def tidy(text: str) -> str:
    """Normalise model output for a plain page: typographic dashes become commas or hyphens,
    odd spaces become spaces, "9.8 %" becomes "9.8%", and repeated spaces collapse."""
    for k, v in _TIDY.items():
        text = text.replace(k, v)
    text = text.translate(_BENGALI_DIGITS)   # numbers stay in Latin digits, as everywhere else in the UI
    text = re.sub(r"(\d) %", r"\1%", text)
    text = re.sub(r" ,", ",", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def write_digest_detailed(settings: Settings, session: dt.date, alerts: list[Alert], symbols: list[str],
                          evidence: dict[str, dict] | None = None, lang: str = "en") -> tuple[str | None, str | None]:
    """Return ``(text, "provider:model")``; ``(None, None)`` when AI is off, nothing notable fired,
    or every provider failed."""
    if not settings.ai_enabled or not alerts or not notable(alerts):
        return None, None
    prompt = build_prompt(session, alerts, symbols, evidence)
    for p in settings.ai_chain():
        ask = prompt
        for attempt in (1, 2):   # a note that invents a number or predicts gets one more try, then no note
            try:
                if p["kind"] == "anthropic":
                    text = _via_anthropic(settings, ask, api_key=p["api_key"], model=p["model"], lang=lang)
                else:
                    text = _via_openai_compatible(settings, ask, base_url=p["base_url"], api_key=p["api_key"], model=p["model"], lang=lang)
            except Exception:
                log.exception("digest via %s failed", p["name"])
                text = None
                break
            if not text:
                log.warning("digest via %s returned nothing; trying the next provider", p["name"])
                break
            text = tidy(text)
            problems = check_note(text, prompt)
            if not problems:
                log.info("digest written by %s (%s)", p["name"], p["model"])
                return text, f"{p['name']}:{p['model']}"
            log.warning("digest via %s rejected (attempt %d): %s", p["name"], attempt, "; ".join(problems[:6]))
            ask = prompt + _RETRY_NOTE.format(reasons="; ".join(problems[:6]))
    return None, None


def write_digest(settings: Settings, session: dt.date, alerts: list[Alert], symbols: list[str],
                 evidence: dict[str, dict] | None = None, lang: str = "en") -> str | None:
    """Return the summary text, or None when AI is off, nothing fired, or the call failed."""
    return write_digest_detailed(settings, session, alerts, symbols, evidence, lang)[0]


def load_rule_evidence(settings: Settings) -> dict[str, dict]:
    """Backtest numbers keyed by rule id (this installation's study, else the one shipped)."""
    from dse_alerts.analysis.calibrate import rule_evidence

    return rule_evidence(settings)
