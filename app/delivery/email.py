"""Scan-to-email over SMTP (standard library only)."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from ..config import Settings


class EmailError(Exception):
    pass


def build_message(settings: Settings, to: str, pdf: Path, subject: str, body: str) -> EmailMessage:
    sender = settings.smtp_from or settings.smtp_user
    if not sender:
        raise EmailError("PLATEN_SMTP_FROM (or PLATEN_SMTP_USER) is not set")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] if "@" in sender else None)
    msg.set_content(body)
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf",
                       filename=pdf.name)
    return msg


def send_pdf(settings: Settings, to: str, pdf: Path, subject: str, body: str) -> str:
    """Send ``pdf`` to ``to``. Blocking; run it in a thread. Returns a short result text."""
    if not settings.smtp_host:
        raise EmailError("Email is not configured (PLATEN_SMTP_HOST)")
    size_mb = pdf.stat().st_size / (1024 * 1024)
    if size_mb > settings.email_max_mb:
        raise EmailError(
            f"The PDF is {size_mb:.1f} MB, over the {settings.email_max_mb:g} MB email limit. "
            "Download it instead, or scan at a lower resolution or in gray."
        )
    msg = build_message(settings, to, pdf, subject, body)
    security = settings.smtp_security.lower()
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=60,
                                      context=context)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
        with server:
            if security == "starttls":
                server.starttls(context=context)
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            refused = server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError("SMTP login failed. Check PLATEN_SMTP_USER and the app password.") \
            from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Sending failed: {exc}") from exc
    if refused:
        raise EmailError(f"The server refused the recipient: {', '.join(refused)}")
    return f"Sent to {to}"
