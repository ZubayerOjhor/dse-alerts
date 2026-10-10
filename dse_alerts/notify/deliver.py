"""Delivery channels: console, local outbox (always), SMTP email (when configured)."""

from __future__ import annotations

import datetime as dt
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from dse_alerts.config import Settings
from dse_alerts.notify import render
from dse_alerts.rules import Alert

log = logging.getLogger(__name__)


def to_outbox(outbox: Path, session: dt.date, email: str, alerts: list[Alert], symbols: list[str], digest: str | None = None) -> Path:
    """Write the text rendering to data/outbox/<session>_<email>.txt and return the path."""
    outbox.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "@.-_" else "_" for c in email)
    p = outbox / f"{session.isoformat()}_{safe}.txt"
    p.write_text(render.subject(session, alerts) + "\n\n" + render.text(session, alerts, symbols, digest), encoding="utf-8")
    return p


def to_console(session: dt.date, email: str, alerts: list[Alert], symbols: list[str], digest: str | None = None) -> None:
    print(f"--- {email}: {render.subject(session, alerts)}")
    print(render.text(session, alerts, symbols, digest))


TELEGRAM_LIMIT = 4000   # Telegram caps a message at 4096 characters


def telegram_text(session: dt.date, alerts: list[Alert], digest: str | None = None) -> str:
    """A compact plain-text rendering for chat: headline per alert, one detail line each."""
    lines = [f"DSE alerts, session {session.isoformat()}"]
    if digest:
        lines += ["", digest.strip()]
    if not alerts:
        lines.append("Nothing fired.")
    for a in render.sort_alerts(alerts):
        mark = {"high": "!!", "notable": "!", "info": "·"}.get(a.severity, "·")
        lines += ["", f"{mark} {a.headline}", f"   {a.detail}"]
    lines += ["", "Observations, not advice."]
    return "\n".join(lines)


def send_telegram(settings: Settings, chat_id: str, text: str, transport=None) -> bool:
    """Send via the Bot API; long texts are split. Returns True when every part was accepted."""
    if not settings.telegram_enabled or not chat_id:
        return False
    import httpx

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > TELEGRAM_LIMIT:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    parts.append(cur)
    ok = True
    try:
        with httpx.Client(timeout=30.0, transport=transport) as c:
            for p in parts:
                r = c.post(url, json={"chat_id": chat_id, "text": p, "disable_web_page_preview": True})
                if r.status_code != 200:
                    log.error("telegram to %s failed: %s %s", chat_id, r.status_code, r.text[:200])
                    ok = False
    except Exception as e:  # noqa: BLE001
        log.error("telegram to %s failed: %s", chat_id, e)
        return False
    return ok


def telegram_updates(settings: Settings, transport=None) -> list[dict]:
    """Recent chats that messaged the bot: [{chat_id, name, text}], for discovering one's chat id."""
    if not settings.telegram_enabled:
        return []
    import httpx

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
    try:
        with httpx.Client(timeout=30.0, transport=transport) as c:
            r = c.get(url)
        data = r.json().get("result", []) if r.status_code == 200 else []
    except Exception as e:  # noqa: BLE001
        log.error("telegram getUpdates failed: %s", e)
        return []
    seen, out = set(), []
    for u in data:
        m = u.get("message") or u.get("channel_post") or {}
        chat = m.get("chat") or {}
        if chat.get("id") and chat["id"] not in seen:
            seen.add(chat["id"])
            name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name"), chat.get("title"), chat.get("username")) if x)
            out.append({"chat_id": str(chat["id"]), "name": name, "text": (m.get("text") or "")[:60]})
    return out


def send_email(settings: Settings, to: str, session: dt.date, alerts: list[Alert], symbols: list[str], digest: str | None = None) -> bool:
    """Send via SMTP (STARTTLS). Returns True on success; never raises on a mail error."""
    if not settings.email_enabled:
        return False
    msg = EmailMessage()
    msg["Subject"] = render.subject(session, alerts)
    msg["From"] = settings.alert_from or settings.smtp_user
    msg["To"] = to
    msg.set_content(render.text(session, alerts, symbols, digest))
    msg.add_alternative(render.html_body(session, alerts, symbols, digest), subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("email to %s failed: %s", to, e)
        return False
