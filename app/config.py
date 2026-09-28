"""Settings: environment variables (see .env.example), overridden by the ones saved
from the web page's Settings screen in /config/settings.json."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _notify_email(value: str, smtp_user: str) -> str:
    value = value.strip()
    if value.lower() == "none":
        return ""
    return value or smtp_user


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
    notify_email: str
    drive_folder_id: str
    google_credentials: Path
    drive_poll_seconds: int
    drive_state_file: Path
    settings_file: Path

    @property
    def adobe_activated(self) -> bool:
        return (self.adept_dir / "activation.xml").exists()

    @property
    def drive_enabled(self) -> bool:
        return bool(self.drive_folder_id)

    @classmethod
    def load(cls) -> "Config":
        """Environment variables, with the web page's saved settings on top."""
        env = dict(os.environ)
        env.update(read_settings_file(Path(env.get("SETTINGS_FILE", "/config/settings.json"))))
        return cls.from_env(env)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        smtp_port = int(env.get("SMTP_PORT") or "587")
        smtp_user = env.get("SMTP_USER", "")
        return cls(
            upload_token=env.get("UPLOAD_TOKEN", ""),
            kindle_email=env.get("KINDLE_EMAIL", ""),
            smtp_host=env.get("SMTP_HOST") or "smtp.gmail.com",
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=env.get("SMTP_PASSWORD", ""),
            smtp_from=env.get("SMTP_FROM") or smtp_user,
            smtp_ssl=_bool(env.get("SMTP_SSL"), default=smtp_port == 465),
            adept_dir=Path(env.get("ADEPT_DIR", "/config/adept")),
            work_dir=Path(env.get("WORK_DIR", "/tmp/libtokindle")),
            keep_files=_bool(env.get("KEEP_FILES")),
            acsmdownloader=env.get("ACSMDOWNLOADER", "acsmdownloader"),
            adept_remove=env.get("ADEPT_REMOVE", "adept_remove"),
            tool_timeout=int(env.get("TOOL_TIMEOUT", "180")),
            # Send to Kindle rejects emails over 50 MB; leave room for base64 overhead.
            max_attachment_bytes=int(env.get("MAX_ATTACHMENT_BYTES", str(36 * 1024 * 1024))),
            # Where pass/fail notices go. Defaults to the Gmail account; "none" turns them off.
            notify_email=_notify_email(env.get("NOTIFY_EMAIL", ""), smtp_user),
            drive_folder_id=env.get("DRIVE_FOLDER_ID", "").strip(),
            google_credentials=Path(env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/config/google-service-account.json")),
            drive_poll_seconds=max(15, int(env.get("DRIVE_POLL_SECONDS") or "60")),
            drive_state_file=Path(env.get("DRIVE_STATE_FILE", "/config/drive-state.json")),
            settings_file=Path(env.get("SETTINGS_FILE", "/config/settings.json")),
        )

    def problems(self) -> list[str]:
        """Missing settings that would stop a book from being delivered."""
        missing = []
        for label, value in [
            ("Access token", self.upload_token),
            ("Kindle email", self.kindle_email),
            ("Gmail address", self.smtp_user),
            ("Gmail app password", self.smtp_password),
        ]:
            if not value:
                missing.append(f"{label} isn't set")
        if not self.adobe_activated:
            missing.append("Adobe device isn't activated")
        if self.drive_enabled and not self.google_credentials.exists():
            missing.append("Google Drive folder is set but the Google key hasn't been uploaded")
        return missing


def read_settings_file(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {str(k): str(v) for k, v in data.items()}
