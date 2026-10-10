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
