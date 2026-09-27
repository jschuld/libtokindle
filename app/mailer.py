"""Deliver a book to the Kindle's Send to Kindle address."""

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .config import Config

MIME_TYPES = {
    ".epub": ("application", "epub+zip"),
    ".pdf": ("application", "pdf"),
}


def send_to_kindle(cfg: Config, book: Path) -> None:
    maintype, subtype = MIME_TYPES.get(book.suffix.lower(), ("application", "octet-stream"))

    msg = EmailMessage()
    msg["From"] = cfg.smtp_from
    msg["To"] = cfg.kindle_email
    # An empty subject makes Amazon keep the book's own title and author.
    msg["Subject"] = ""
    msg.set_content("Sent by libtokindle.")
    msg.add_attachment(book.read_bytes(), maintype=maintype, subtype=subtype, filename=book.name)

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
