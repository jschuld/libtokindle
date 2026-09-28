"""Settings edited from the web page, saved to /config/settings.json.

Keys are the same names as the environment variables, and saved values override them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Config, read_settings_file

# Settings the page can change. Secrets are never sent back to the browser.
EDITABLE = [
    "KINDLE_EMAIL",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_FROM",
    "NOTIFY_EMAIL",
    "DRIVE_FOLDER_ID",
    "DRIVE_POLL_SECONDS",
    "UPLOAD_TOKEN",
]
SECRETS = {"SMTP_PASSWORD", "UPLOAD_TOKEN"}

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SettingsError(Exception):
    pass


def current(cfg: Config) -> dict:
    """What the Settings page shows."""
    values = {
        "KINDLE_EMAIL": cfg.kindle_email,
        "SMTP_USER": cfg.smtp_user,
        "SMTP_HOST": cfg.smtp_host,
        "SMTP_PORT": str(cfg.smtp_port),
        "SMTP_FROM": cfg.smtp_from if cfg.smtp_from != cfg.smtp_user else "",
        "NOTIFY_EMAIL": _saved_value(cfg, "NOTIFY_EMAIL"),
        "DRIVE_FOLDER_ID": cfg.drive_folder_id,
        "DRIVE_POLL_SECONDS": str(cfg.drive_poll_seconds),
    }
    return {
        "values": values,
        "secrets_set": {
            "SMTP_PASSWORD": bool(cfg.smtp_password),
            "UPLOAD_TOKEN": bool(cfg.upload_token),
        },
        "google_key": google_key_info(cfg),
        "adobe_activated": cfg.adobe_activated,
        "problems": cfg.problems(),
    }


def _saved_value(cfg: Config, key: str) -> str:
    """The value as the user set it, e.g. "" for "use the default", rather than the resolved one."""
    return read_settings_file(cfg.settings_file).get(key, os.environ.get(key, ""))


def normalise(changes: dict) -> dict[str, str]:
    """Validate the fields sent from the page. Empty secrets mean "keep the current one"."""
    clean: dict[str, str] = {}
    errors: list[str] = []
    for key, value in changes.items():
        if key not in EDITABLE:
            errors.append(f"{key} can't be changed here")
            continue
        value = str(value if value is not None else "").strip()
        if key in SECRETS and value == "":
            continue
        if key == "SMTP_PASSWORD":
            value = value.replace(" ", "")  # Gmail shows app passwords in groups of four
        clean[key] = value

    for key in ("KINDLE_EMAIL", "SMTP_USER", "SMTP_FROM"):
        if clean.get(key) and not EMAIL.match(clean[key]):
            errors.append(f"{clean[key]} isn't an email address")
    notify = clean.get("NOTIFY_EMAIL", "")
    if notify and notify.lower() != "none" and not EMAIL.match(notify):
        errors.append(f"{notify} isn't an email address (leave empty for your Gmail, or type none)")
    if "SMTP_PORT" in clean and not clean["SMTP_PORT"].isdigit():
        errors.append("SMTP port must be a number")
    if "DRIVE_POLL_SECONDS" in clean:
        if not clean["DRIVE_POLL_SECONDS"].isdigit() or int(clean["DRIVE_POLL_SECONDS"]) < 15:
            errors.append("Drive check interval must be a number of seconds, at least 15")
    if "DRIVE_FOLDER_ID" in clean:
        clean["DRIVE_FOLDER_ID"] = drive_folder_id(clean["DRIVE_FOLDER_ID"])
    if "UPLOAD_TOKEN" in clean and len(clean["UPLOAD_TOKEN"]) < 12:
        errors.append("The access token must be at least 12 characters")
    if errors:
        raise SettingsError("; ".join(errors))
    return clean


def drive_folder_id(value: str) -> str:
    """Accept either a folder ID or the folder's full URL."""
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value


def save(cfg: Config, changes: dict[str, str]) -> None:
    data = read_settings_file(cfg.settings_file)
    data.update(changes)
    _write_private(cfg.settings_file, json.dumps(data, indent=2, sort_keys=True))


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


# --- Google service account key

def google_key_info(cfg: Config) -> dict:
    try:
        key = json.loads(cfg.google_credentials.read_text())
    except (FileNotFoundError, ValueError):
        return {"present": False, "client_email": None}
    return {"present": True, "client_email": key.get("client_email")}


def save_google_key(cfg: Config, data: bytes) -> None:
    try:
        key = json.loads(data)
    except ValueError:
        raise SettingsError("That isn't a JSON key file.") from None
    if not isinstance(key, dict) or key.get("type") != "service_account" or not key.get("client_email") or not key.get("private_key"):
        raise SettingsError("That isn't a service account key. In Google Cloud, create a JSON key for a service account.")
    _write_private(cfg.google_credentials, json.dumps(key))


# --- Adobe activation

def activate_adobe(cfg: Config, adobe_id: str = "", password: str = "") -> None:
    if cfg.adobe_activated:
        raise SettingsError("Already activated.")
    args = [os.environ.get("ADEPT_ACTIVATE", "adept_activate"), "--random-serial", "--output-dir", str(cfg.adept_dir)]
    if adobe_id:
        args += ["--username", adobe_id, "--password", password]
    else:
        args.append("--anonymous")
    # A half-finished earlier attempt leaves a folder that adept_activate won't overwrite.
    shutil.rmtree(cfg.adept_dir, ignore_errors=True)
    cfg.adept_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise SettingsError("adept_activate isn't installed.") from None
    except subprocess.TimeoutExpired:
        raise SettingsError("Activation timed out. Are Adobe's servers reachable?") from None
    if result.returncode != 0 or not cfg.adobe_activated:
        output = (result.stderr or result.stdout).strip().splitlines()
        raise SettingsError("Activation failed: " + (" ".join(output[-3:]) if output else f"exit code {result.returncode}"))
