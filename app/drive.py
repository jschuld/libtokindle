"""Watch a Google Drive folder and send every new file in it to the Kindle.

Uses a Google Cloud service account with read-only access to the one folder you
share with it. A small JSON state file records when watching started (files created
before then are skipped) and the IDs of files already handled, so nothing is sent
twice and nothing in the folder is changed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Protocol

from . import pipeline
from .config import Config
from .jobs import JobStore

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# When settings change, a new watcher starts while the old one may still be mid-check;
# this keeps them from both handling the same new file.
_poll_lock = threading.Lock()


class DriveError(Exception):
    """A Drive problem with a message meant for the person using the web page."""


def _now_rfc3339() -> str:
    """The current time in the format Drive uses for createdTime, e.g. 2026-09-28T07:01:02.123Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Session(Protocol):
    def get(self, url: str, params: dict | None = None, timeout: int = ...) -> Any: ...


def authorized_session(cfg: Config) -> Session:
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(str(cfg.google_credentials), scopes=SCOPES)
    return AuthorizedSession(creds)


class DriveWatcher:
    def __init__(self, cfg: Config, store: JobStore, session: Session | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self._session = session
        self.last_check: float | None = None
        self.last_error: str | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = authorized_session(self.cfg)
        return self._session

    # --- Drive API

    def list_files(self) -> list[dict]:
        files: list[dict] = []
        params = {
            "q": f"'{self.cfg.drive_folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, size, createdTime)",
            "orderBy": "createdTime",
            "pageSize": 100,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        while True:
            res = self.session.get(f"{API}/files", params=params, timeout=30)
            self._check(res)
            body = res.json()
            files.extend(body.get("files", []))
            if not body.get("nextPageToken"):
                return files
            params["pageToken"] = body["nextPageToken"]

    def download(self, file_id: str) -> bytes:
        res = self.session.get(f"{API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}, timeout=120)
        self._check(res)
        return res.content

    def _check(self, res: Any) -> None:
        """Turn Google's HTTP errors into messages that say what to fix."""
        status = getattr(res, "status_code", 200)
        if status < 400:
            return
        try:
            detail = res.json()["error"]["message"]
        except Exception:
            detail = getattr(res, "reason", "") or f"HTTP {status}"
        who = self._service_account_email() or "the service account"
        if status == 404:
            raise DriveError(
                f"Google Drive can't find the folder. Check the folder link in Settings, and that the "
                f"folder is shared with {who} (as Viewer). Google said: {detail}"
            )
        if status == 403:
            raise DriveError(
                f"Google Drive refused access. Check that the Google Drive API is enabled in your Google "
                f"Cloud project and that the folder is shared with {who}. Google said: {detail}"
            )
        raise DriveError(f"Google Drive error {status}: {detail}")

    def _service_account_email(self) -> str | None:
        try:
            return json.loads(self.cfg.google_credentials.read_text()).get("client_email")
        except (OSError, ValueError):
            return None

    # --- State: when watching started, and IDs of files already handled

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.cfg.drive_state_file.read_text())
        except (FileNotFoundError, ValueError):
            state = None
        if not state or state.get("folder", self.cfg.drive_folder_id) != self.cfg.drive_folder_id:
            # New folder: skip what's already in it, but send anything added from now on,
            # even if the first successful check is a while away.
            state = {"folder": self.cfg.drive_folder_id, "since": _now_rfc3339(), "seen": []}
            self._save_state(state)
            log.info("Started watching Google Drive folder %s; files already in it are skipped", self.cfg.drive_folder_id)
        state.setdefault("folder", self.cfg.drive_folder_id)
        state.setdefault("since", "")  # state from older versions: every unseen file is new
        return state

    def _save_state(self, state: dict) -> None:
        self.cfg.drive_state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.drive_state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({**state, "seen": sorted(set(state["seen"]))}))
        tmp.replace(self.cfg.drive_state_file)

    # --- Polling

    def poll_once(self) -> None:
        with _poll_lock:
            self._poll()

    def _poll(self) -> None:
        state = self._load_state()
        files = self.list_files()
        seen = set(state["seen"])
        for f in files:
            if f["id"] in seen:
                continue
            # Record it before processing, so a crash can't make us send it again and again.
            seen.add(f["id"])
            state["seen"] = sorted(seen)
            self._save_state(state)
            # RFC 3339 timestamps in UTC compare correctly as strings.
            if state["since"] and f.get("createdTime", "") < state["since"]:
                log.info("Skipping %s: it was in the folder before watching started", f["name"])
                continue
            self._handle(f)

    def _handle(self, f: dict) -> None:
        name = f["name"]
        log.info("New file in Drive: %s", name)
        job = self.store.create(name, source="drive")
        if f.get("mimeType", "").startswith("application/vnd.google-apps."):
            return pipeline.fail(self.cfg, self.store, job, "Google Docs/Sheets files can't be sent. Add an .acsm, EPUB or PDF file.")
        if not pipeline.is_supported(name):
            return pipeline.fail(self.cfg, self.store, job, f"{name} isn't a file a Kindle can read. Add an .acsm, EPUB or PDF file.")
        if int(f.get("size") or 0) > self.cfg.max_attachment_bytes:
            return pipeline.fail(self.cfg, self.store, job, "The file is too big to email to a Kindle.")
        try:
            data = self.download(f["id"])
        except Exception as exc:
            return pipeline.fail(self.cfg, self.store, job, f"Couldn't download it from Google Drive: {exc}")
        pipeline.process(self.cfg, self.store, job, data)

    def run(self, stop: threading.Event) -> None:
        log.info("Watching Google Drive folder %s every %ds", self.cfg.drive_folder_id, self.cfg.drive_poll_seconds)
        while not stop.is_set():
            try:
                self.poll_once()
                if self.last_error:
                    log.info("Google Drive check is working again")
                self.last_error = None
            except Exception as exc:
                # Log a failure once, not every minute while it lasts.
                if str(exc) != self.last_error:
                    log.exception("Drive check failed")
                self.last_error = str(exc)
            self.last_check = time.time()
            stop.wait(self.cfg.drive_poll_seconds)

    def start(self) -> threading.Event:
        stop = threading.Event()
        threading.Thread(target=self.run, args=(stop,), name="drive-watcher", daemon=True).start()
        return stop

    def status(self) -> dict:
        return {"enabled": True, "last_check": self.last_check, "last_error": self.last_error}
