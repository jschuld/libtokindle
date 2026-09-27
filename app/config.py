"""Settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    upload_token: str
    kindle_email: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_ssl: bool
    adept_dir: Path
    work_dir: Path
    keep_files: bool
    acsmdownloader: str
    adept_remove: str
    tool_timeout: int
    max_attachment_bytes: int

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        smtp_port = int(env.get("SMTP_PORT", "587"))
        smtp_user = env.get("SMTP_USER", "")
        return cls(
            upload_token=env.get("UPLOAD_TOKEN", ""),
            kindle_email=env.get("KINDLE_EMAIL", ""),
            smtp_host=env.get("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=env.get("SMTP_PASSWORD", ""),
            smtp_from=env.get("SMTP_FROM", smtp_user),
            smtp_ssl=_bool(env.get("SMTP_SSL"), default=smtp_port == 465),
            adept_dir=Path(env.get("ADEPT_DIR", "/config/adept")),
            work_dir=Path(env.get("WORK_DIR", "/tmp/libtokindle")),
            keep_files=_bool(env.get("KEEP_FILES")),
            acsmdownloader=env.get("ACSMDOWNLOADER", "acsmdownloader"),
            adept_remove=env.get("ADEPT_REMOVE", "adept_remove"),
            tool_timeout=int(env.get("TOOL_TIMEOUT", "180")),
            # Send to Kindle rejects emails over 50 MB; leave room for base64 overhead.
            max_attachment_bytes=int(env.get("MAX_ATTACHMENT_BYTES", str(36 * 1024 * 1024))),
        )

    def problems(self) -> list[str]:
        """Missing settings that would stop a book from being delivered."""
        missing = []
        for name, value in [
            ("UPLOAD_TOKEN", self.upload_token),
            ("KINDLE_EMAIL", self.kindle_email),
            ("SMTP_USER", self.smtp_user),
            ("SMTP_PASSWORD", self.smtp_password),
        ]:
            if not value:
                missing.append(f"{name} is not set")
        if not (self.adept_dir / "activation.xml").exists():
            missing.append(f"Adobe device not activated (no activation.xml in {self.adept_dir}); run `activate` first")
        return missing
