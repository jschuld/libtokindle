"""Libby / OverDrive integration: borrow holds as soon as they're ready, and send new
ebook loans to the Kindle.

Libby has no public API. This talks to the same endpoints the Libby web app uses
(sentry-read.svc.overdrive.com), the way odmpy and the Libby calibre plugin do.
The service links to the account as another "device" with a Libby setup code
(Libby → Menu → Copy To Another Device); no library card PIN is stored.

For each new ebook loan it asks Libby for the Adobe EPUB (.acsm) or, when the
publisher offers one, a DRM-free EPUB, and hands it to the normal pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import pipeline
from .config import Config
from .jobs import JobStore

log = logging.getLogger(__name__)

API = "https://sentry-read.svc.overdrive.com"
HEADERS = {
    "Accept": "application/json",
    "Cache-Control": "no-cache",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
}
# Formats we can send, best first: DRM-free EPUB needs no DRM step at all.
FORMATS = ["ebook-epub-open", "ebook-epub-adobe", "ebook-pdf-open", "ebook-pdf-adobe"]
EXTENSIONS = {"ebook-epub-open": ".epub", "ebook-epub-adobe": ".acsm", "ebook-pdf-open": ".pdf", "ebook-pdf-adobe": ".acsm"}
MAX_BACKOFF_SECONDS = 4 * 3600


class LibbyError(Exception):
    """A Libby problem with a message meant for the person using the web page."""


class LibbyRateLimited(LibbyError):
    pass


# --- Helpers for Libby's loan / hold / card objects

def item_key(item: dict) -> str:
    """Identifies one loan or hold. The date makes a later re-borrow of the same title count as new."""
    return f"{item.get('cardId')}:{item.get('id')}:{item.get('checkoutDate') or item.get('placedDate') or ''}"


def is_ebook(item: dict) -> bool:
    return (item.get("type") or {}).get("id") == "ebook"


def best_format(item: dict) -> str | None:
    available = {f.get("id") for f in item.get("formats") or []}
    return next((f for f in FORMATS if f in available), None)


def describe(item: dict) -> str:
    title = item.get("title") or item.get("sortTitle") or f"Libby title {item.get('id')}"
    author = item.get("firstCreatorName")
    return f"{title} by {author}" if author else title


def lending_days(card: dict, type_id: str) -> int:
    """The card's preferred loan length for this kind of title (Libby's own default)."""
    periods = card.get("lendingPeriods") or {}
    period = periods.get(type_id) or (periods.get("book") if type_id == "ebook" else None) or {}
    preference = (period.get("preference") or [0])[0]
    if preference:
        return int(preference)
    options = period.get("options") or []
    return int(options[-1][0]) if options else 21


def library_name(card: dict) -> str:
    return (card.get("library") or {}).get("name") or card.get("advantageKey") or card.get("cardName") or "your library"


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    tmp.replace(path)


# --- API client

class LibbyClient:
    def __init__(self, path: Path, http: Any = None) -> None:
        self.path = path
        if http is None:
            import requests

            http = requests.Session()
        self.http = http

    def _identity(self) -> str:
        try:
            return json.loads(self.path.read_text())["identity"]
        except (OSError, ValueError, KeyError):
            raise LibbyError("Libby isn't connected. Connect it on the Settings page.") from None

    def _save_identity(self, identity: str) -> None:
        _write_private(self.path, {"identity": identity, "linked": time.time()})

    def _request(self, method: str, endpoint: str, *, token: str | None = None, anonymous: bool = False,
                 retry: bool = True, raw: bool = False, **kwargs) -> Any:
        headers = dict(HEADERS)
        if raw:
            headers["Accept"] = "*/*"
        if not anonymous:
            headers["Authorization"] = f"Bearer {token or self._identity()}"
        res = self.http.request(method, f"{API}/{endpoint}", headers=headers, timeout=60, **kwargs)
        status = res.status_code
        if status == 401 and not anonymous and token is None and retry:
            self._refresh()
            return self._request(method, endpoint, retry=False, raw=raw, **kwargs)
        if status == 429 or (status == 403 and "whoa" in (res.text or "")):
            raise LibbyRateLimited("Libby is limiting requests for now. The service will try again later.")
        if status >= 400:
            raise LibbyError(f"Libby said {status} for {endpoint.split('/')[0]}: {_detail(res)}")
        return res

    def _refresh(self) -> None:
        """Libby's session tokens expire; a signed-in chip can mint a fresh one."""
        res = self._request("POST", "chip", token=self._identity(), params={"client": "dewey"})
        self._save_identity(res.json()["identity"])

    def connect(self, code: str) -> dict:
        """Link to a Libby account with an 8-digit setup code. Returns the first sync."""
        code = re.sub(r"\D", "", code or "")
        if len(code) != 8:
            raise LibbyError("The setup code is 8 digits. In Libby, open the menu and choose Copy To Another Device.")
        chip = self._request("POST", "chip", anonymous=True, params={"client": "dewey"}).json()
        token = chip["identity"]
        try:
            self._request("POST", "chip/clone/code", token=token, json={"code": code})
        except LibbyRateLimited:
            raise
        except LibbyError as exc:
            raise LibbyError(
                f"Libby didn't accept that setup code. Codes only last a few minutes, so get a new one and try again. ({exc})"
            ) from None
        # Mint the identity again so it carries the linked library cards.
        chip = self._request("POST", "chip", token=token, params={"client": "dewey"}).json()
        self._save_identity(chip["identity"])
        data = self.sync()
        if not data.get("cards"):
            self.path.unlink(missing_ok=True)
            raise LibbyError("Linked, but that Libby account has no library cards. Add your Auckland Libraries card in Libby first.")
        return data

    def sync(self) -> dict:
        return self._request("GET", "chip/sync").json()

    def open_loan(self, loan: dict) -> None:
        """Libby expects a loan to be "opened" before it's fulfilled, like the app does."""
        type_id = (loan.get("type") or {}).get("id", "ebook")
        self._request("GET", f"open/{type_id}/card/{loan['cardId']}/title/{loan['id']}")

    def fulfill(self, loan: dict, fmt: str) -> bytes:
        """The .acsm (Adobe formats) or the DRM-free file itself (open formats)."""
        res = self._request("GET", f"card/{loan['cardId']}/loan/{loan['id']}/fulfill/{fmt}", raw=True, allow_redirects=False)
        if 300 <= res.status_code < 400:
            location = res.headers.get("Location")
            if not location:
                raise LibbyError("Libby sent a redirect without a download link.")
            res = self.http.request("GET", location, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=120)
            if res.status_code >= 400:
                raise LibbyError(f"Downloading the book from OverDrive failed ({res.status_code}).")
        return res.content

    def borrow(self, hold: dict, card: dict) -> dict:
        type_id = (hold.get("type") or {}).get("id", "ebook")
        payload = {"period": lending_days(card, type_id), "units": "days", "lucky_day": None, "title_format": type_id}
        return self._request("POST", f"card/{hold['cardId']}/loan/{hold['id']}", json=payload).json()


def _detail(res: Any) -> str:
    try:
        body = res.json()
        return str(body.get("result") or body.get("message") or body)[:200]
    except Exception:
        return (getattr(res, "text", "") or "")[:200] or getattr(res, "reason", "")


# --- Watcher

class LibbyWatcher:
    def __init__(self, cfg: Config, store: JobStore, client: LibbyClient | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client or LibbyClient(cfg.libby_file)
        self.last_check: float | None = None
        self.last_error: str | None = None
        self.last_sync: dict | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()

    # --- State: which loans and hold problems have been handled

    def _load_state(self) -> dict | None:
        try:
            return json.loads(self.cfg.libby_state_file.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def _save_state(self, state: dict) -> None:
        self.cfg.libby_state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.libby_state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.cfg.libby_state_file)

    # --- Checking

    def poll_once(self) -> None:
        with self._lock:
            self._poll()

    def _poll(self) -> None:
        data = self.client.sync()
        state = self._load_state()
        if state is None:
            # First check after connecting: loans you already have are skipped.
            state = {"seen": [item_key(loan) for loan in data.get("loans", [])], "hold_failures": []}
            self._save_state(state)
            log.info("Libby connected; skipping %d loan(s) you already have", len(state["seen"]))

        if self.cfg.libby_auto_borrow and self._borrow_ready_holds(data, state):
            data = self.client.sync()  # the new loans
        self.last_sync = data

        seen = set(state["seen"])
        for loan in data.get("loans", []):
            key = item_key(loan)
            if key in seen:
                continue
            # Record it before sending, so a crash can't make it send again and again.
            seen.add(key)
            state["seen"] = sorted(seen)
            self._save_state(state)
            if not is_ebook(loan):
                log.info("New Libby loan %s is not an ebook; not sending it", describe(loan))
                continue
            self.send_loan(loan)

    def _borrow_ready_holds(self, data: dict, state: dict) -> bool:
        cards = {c.get("cardId"): c for c in data.get("cards", [])}
        failures = set(state.get("hold_failures", []))
        borrowed = False
        for hold in data.get("holds", []):
            if not hold.get("isAvailable") or not is_ebook(hold):
                continue
            key = item_key(hold)
            try:
                self.client.borrow(hold, cards.get(hold.get("cardId"), {}))
            except LibbyRateLimited:
                raise
            except LibbyError as exc:
                log.warning("Couldn't borrow ready hold %s: %s", describe(hold), exc)
                if key not in failures:  # tell the user once, keep trying each check
                    failures.add(key)
                    job = self.store.create(describe(hold), source="libby")
                    pipeline.fail(self.cfg, self.store, job,
                                  f"Your hold is ready, but borrowing it failed: {exc}. "
                                  "You may be at your loan limit; borrow it in Libby within 3 days.")
                continue
            log.info("Borrowed ready hold: %s", describe(hold))
            borrowed = True
        state["hold_failures"] = sorted(failures)
        self._save_state(state)
        return borrowed

    def send_loan(self, loan: dict) -> None:
        name = describe(loan)
        fmt = best_format(loan)
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", loan.get("title") or name).strip(" .") or "book"
        job = self.store.create(safe + (EXTENSIONS[fmt] if fmt else ""), source="libby")
        log.info("Sending Libby loan %s (%s)", name, fmt or "no downloadable format")
        if not fmt:
            return pipeline.fail(self.cfg, self.store, job,
                                 f"“{name}” can only be read in the Libby app or a browser; "
                                 "the library doesn't offer it as an EPUB or PDF download.")
        try:
            try:
                self.client.open_loan(loan)
            except LibbyRateLimited:
                raise
            except LibbyError as exc:
                log.warning("Opening loan %s failed (continuing): %s", name, exc)
            data = self.client.fulfill(loan, fmt)
        except LibbyError as exc:
            return pipeline.fail(self.cfg, self.store, job, f"Couldn't get “{name}” from Libby: {exc}")
        pipeline.process(self.cfg, self.store, job, data)

    # --- For the web page

    def loans(self) -> list[dict]:
        """Current loans from the last check, for the Send to Kindle buttons."""
        data = self.last_sync
        if data is None:
            with self._lock:
                data = self.last_sync = self.client.sync()
        seen = set((self._load_state() or {}).get("seen", []))
        return [
            {
                "key": item_key(loan),
                "title": loan.get("title"),
                "author": loan.get("firstCreatorName"),
                "type": (loan.get("type") or {}).get("id"),
                "sendable": is_ebook(loan) and best_format(loan) is not None,
                "handled": item_key(loan) in seen,
                "expires": loan.get("expireDate"),
            }
            for loan in data.get("loans", [])
        ]

    def send_by_key(self, key: str) -> None:
        loan = next((l for l in (self.last_sync or {}).get("loans", []) if item_key(l) == key), None)
        if loan is None:
            raise LibbyError("That loan isn't in your Libby account any more. Refresh the page.")
        with self._lock:
            state = self._load_state() or {"seen": [], "hold_failures": []}
            state["seen"] = sorted(set(state["seen"]) | {key})
            self._save_state(state)
        self.send_loan(loan)

    def status(self) -> dict:
        cards = (self.last_sync or {}).get("cards", [])
        return {
            "connected": True,
            "libraries": sorted({library_name(c) for c in cards}),
            "auto_borrow": self.cfg.libby_auto_borrow,
            "last_check": self.last_check,
            "last_error": self.last_error,
        }

    # --- Background loop

    def run(self) -> None:
        interval = self.cfg.libby_poll_minutes * 60
        log.info("Watching Libby every %d minutes (auto-borrow %s)", self.cfg.libby_poll_minutes,
                 "on" if self.cfg.libby_auto_borrow else "off")
        backoff = 0
        while not self._stop.is_set():
            wait = interval
            try:
                self.poll_once()
                if self.last_error:
                    log.info("Libby check is working again")
                self.last_error = None
                backoff = 0
            except LibbyRateLimited as exc:
                backoff += 1
                wait = min(interval * 2 ** backoff, MAX_BACKOFF_SECONDS)
                log.warning("Libby is rate-limiting; next check in %d minutes", wait // 60)
                self.last_error = str(exc)
            except Exception as exc:
                # Log a failure once, not every check while it lasts.
                if str(exc) != self.last_error:
                    log.exception("Libby check failed")
                self.last_error = str(exc)
            self.last_check = time.time()
            self._wake.wait(wait)
            self._wake.clear()

    def check_now(self) -> None:
        self._wake.set()

    def start(self) -> None:
        threading.Thread(target=self.run, name="libby-watcher", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
