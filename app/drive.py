"""Watch a Google Drive folder and send every new file in it to the Kindle.

Uses a Google Cloud service account with read-only access to the one folder you
share with it. Files already processed are remembered by ID in a small JSON state
file, so nothing is sent twice and nothing in the folder is changed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
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
            "fields": "nextPageToken, files(id, name, mimeType, size)",
            "orderBy": "createdTime",
            "pageSize": 100,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        while True:
            res = self.session.get(f"{API}/files", params=params, timeout=30)
            res.raise_for_status()
            body = res.json()
            files.extend(body.get("files", []))
            if not body.get("nextPageToken"):
                return files
            params["pageToken"] = body["nextPageToken"]

    def download(self, file_id: str) -> bytes:
        res = self.session.get(f"{API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}, timeout=120)
        res.raise_for_status()
        return res.content

    # --- State: IDs of files already handled

    def _load_seen(self) -> set[str] | None:
        try:
            return set(json.loads(self.cfg.drive_state_file.read_text())["seen"])
        except FileNotFoundError:
            return None

    def _save_seen(self, seen: set[str]) -> None:
        self.cfg.drive_state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.drive_state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": sorted(seen)}))
        tmp.replace(self.cfg.drive_state_file)

    # --- Polling

    def poll_once(self) -> None:
        with _poll_lock:
            self._poll()

    def _poll(self) -> None:
        files = self.list_files()
        seen = self._load_seen()
        if seen is None:
            # First run: files already in the folder are treated as sent, so turning
            # the watcher on doesn't email everything that's there.
            self._save_seen({f["id"] for f in files})
            log.info("Drive watcher started; ignoring %d file(s) already in the folder", len(files))
            return

        for f in files:
            if f["id"] in seen:
                continue
            # Record it before processing, so a crash can't make us send it again and again.
            seen.add(f["id"])
            self._save_seen(seen)
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
