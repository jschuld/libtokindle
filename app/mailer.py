"""Outgoing email: books to the Kindle, and pass/fail notices to you."""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .config import Config

mimetypes.add_type("application/epub+zip", ".epub")


def _send(cfg: Config, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if cfg.smtp_ssl:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=60) as smtp:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as smtp:
            smtp.starttls(context=context)
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)


def send_to_kindle(cfg: Config, book: Path) -> None:
    mime = mimetypes.guess_type(book.name)[0] or "application/octet-stream"
    maintype, subtype = mime.split("/", 1)

    msg = EmailMessage()
    msg["From"] = cfg.smtp_from
    msg["To"] = cfg.kindle_email
    # An empty subject makes Amazon keep the book's own title and author.
    msg["Subject"] = ""
    msg.set_content("Sent by libtokindle.")
    msg.add_attachment(book.read_bytes(), maintype=maintype, subtype=subtype, filename=book.name)
    _send(cfg, msg)


def send_notification(cfg: Config, subject: str, body: str, attachment: Path | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.smtp_from
    msg["To"] = cfg.notify_email
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment:
        mime = mimetypes.guess_type(attachment.name)[0] or "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=attachment.name)
    _send(cfg, msg)
