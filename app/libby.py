"""Libby / OverDrive integration: borrow holds as soon as they're ready, and send new
ebook loans to the Kindle.

Libby has no public API. This talks to the same endpoints the Libby app uses
(sentry-read.svc.overdrive.com). It signs in once with the library card number and
PIN, the way libby-archiver does, and keeps only Libby's session token; the PIN is
not stored. Loans and holds belong to the card, so this sees the same ones as the
Libby app on the phone.

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
LIBRARIES_API = "https://thunder.api.overdrive.com/v2/libraries"
DEFAULT_LIBRARY = "aucklandlibraries"
# Libby's client id and version, sent when creating a session ("chip").
CHIP_PARAMS = {"c": "d:22.1.1", "s": "0"}
HEADERS = {
    "Accept": "application/json",
    "Cache-Control": "no-cache",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
}
# Formats we can send, best first: DRM-free EPUB needs no DRM step at all.
FORMATS = ["ebook-epub-open", "ebook-epub-adobe", "ebook-pdf-open", "ebook-pdf-adobe"]
EXTENSIONS = {"ebook-epub-open": ".epub", "ebook-epub-adobe": ".acsm", "ebook-pdf-open": ".pdf", "ebook-pdf-adobe": ".acsm"}
MAX_BACKOFF_SECONDS = 4 * 3600
# On some networks sentry-read.svc.overdrive.com is served by an OverDrive edge whose
# certificate is only valid for *.odrsre.overdrive.com (also noted by libby-archiver).
# When that exact mismatch happens, the certificate is still fully verified (trusted CA,
# dates), but checked against this OverDrive name instead of the host name.
EDGE_CERT_NAME = "sentry-read.odrsre.overdrive.com"


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


def _is_hostname_mismatch(exc: Exception) -> bool:
    text = str(exc)
    return "Hostname mismatch" in text or "doesn't match" in text or "not valid for" in text


def _edge_session() -> Any:
    """A requests session that verifies the OverDrive edge certificate by its own name."""
    import requests
    from requests.adapters import HTTPAdapter

    class EdgeAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["assert_hostname"] = EDGE_CERT_NAME
            super().init_poolmanager(*args, **kwargs)

    session = requests.Session()
    session.mount(API + "/", EdgeAdapter())
    return session


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
        self.api_http = http  # replaced by an edge session if Libby's certificate name mismatches
        self._edge_factory = _edge_session

    # --- The saved session: {"identity", "chip", "library": {...}, "linked"}

    def session(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            raise LibbyError("Libby isn't connected. Connect it on the Settings page.") from None

    def _identity(self) -> str:
        try:
            return self.session()["identity"]
        except KeyError:
            raise LibbyError("Libby isn't connected. Connect it on the Settings page.") from None

    def _save_session(self, **changes) -> None:
        try:
            data = self.session()
        except LibbyError:
            data = {}
        _write_private(self.path, {**data, **changes})

    # --- HTTP

    def _request(self, method: str, endpoint: str, *, token: str | None = None, anonymous: bool = False,
                 retry: bool = True, raw: bool = False, **kwargs) -> Any:
        headers = dict(HEADERS)
        if raw:
            headers["Accept"] = "*/*"
        if not anonymous:
            headers["Authorization"] = f"Bearer {token or self._identity()}"
        url = endpoint if endpoint.startswith("https://") else f"{API}/{endpoint}"
        res = self._send(method, url, headers=headers, **kwargs)
        status = res.status_code
        result = _result(res) if status >= 400 else ""
        if (status == 401 or result == "missing_chip") and not anonymous and token is None and retry:
            self._refresh()
            return self._request(method, endpoint, retry=False, raw=raw, **kwargs)
        if status == 429 or (status == 403 and "whoa" in (res.text or "")):
            raise LibbyRateLimited("Libby is limiting requests for now. The service will try again later.")
        if status >= 400:
            where = endpoint.rsplit("/", 1)[-1] if endpoint.startswith("https://") else endpoint.split("/")[0]
            raise LibbyError(f"Libby said {status} for {where}: {_detail(res)}")
        return res

    def _send(self, method: str, url: str, **kwargs) -> Any:
        """One HTTP request to Libby, turning network failures into readable errors."""
        import requests

        http = self.api_http if url.startswith(API + "/") else self.http
        try:
            return http.request(method, url, timeout=60, **kwargs)
        except requests.exceptions.SSLError as exc:
            if not (url.startswith(API + "/") and http is self.http and _is_hostname_mismatch(exc)):
                raise LibbyError(f"Couldn't make a secure connection to Libby: {exc}") from None
            log.warning("Libby's server presented a certificate for OverDrive's edge network instead of "
                        "%s; verifying it as %s", API.removeprefix("https://"), EDGE_CERT_NAME)
            self.api_http = self._edge_factory()
            try:
                return self.api_http.request(method, url, timeout=60, **kwargs)
            except requests.exceptions.SSLError as exc2:
                raise LibbyError(f"Couldn't make a secure connection to Libby: {exc2}") from None
            except requests.exceptions.RequestException as exc2:
                raise LibbyError(f"Couldn't reach Libby: {exc2}") from None
        except requests.exceptions.RequestException as exc:
            raise LibbyError(f"Couldn't reach Libby: {exc}") from None

    def _mint(self, token: str | None = None, chip_id: str | None = None) -> dict:
        """Create a Libby session ("chip"), or refresh one when given its token and id."""
        params = dict(CHIP_PARAMS)
        if chip_id:
            params["v"] = chip_id[:8]
        if token:
            return self._request("POST", "chip", token=token, params=params).json()
        return self._request("POST", "chip", anonymous=True, params=params).json()

    def _refresh(self) -> None:
        """Libby's session tokens expire; the saved chip can mint a fresh one."""
        saved = self.session()
        chip = self._mint(token=saved["identity"], chip_id=saved.get("chip"))
        self._save_session(identity=chip["identity"])

    # --- Connecting

    def find_library(self, key: str) -> dict:
        """Look up a library by its Libby key (libbyapp.com/library/<key>)."""
        key = (key or DEFAULT_LIBRARY).strip().lower()
        match = re.search(r"libbyapp\.com/library/([a-z0-9-]+)", key)
        key = match.group(1) if match else key
        if not re.fullmatch(r"[a-z0-9-]+", key):
            raise LibbyError("That doesn't look like a Libby library name. It's the word after libbyapp.com/library/.")
        res = self._send("GET", f"{LIBRARIES_API}/{key}", headers=HEADERS)
        if res.status_code == 404:
            raise LibbyError(f"Libby doesn't know a library called “{key}”. It's the word after libbyapp.com/library/ "
                             "when you open your library in a browser.")
        if res.status_code >= 400:
            raise LibbyError(f"Looking up the library failed ({res.status_code}): {_detail(res)}")
        body = res.json()
        return {"key": body.get("preferredKey") or key, "name": body.get("name") or key, "websiteId": str(body["websiteId"])}

    def _ils(self, token: str, library: dict) -> str:
        """Which sign-in form the library uses. Most libraries have exactly one."""
        forms = self._request("GET", f"auth/forms/{library['websiteId']}", token=token).json().get("forms") or []
        names = [f.get("ilsName") for f in forms if f.get("ilsName")]
        if not names:
            raise LibbyError(f"{library['name']} doesn't offer a card sign-in that this service understands.")
        for name in names:
            if name == library["key"]:
                return name
        if len(names) > 1:
            log.info("Library has several sign-in forms (%s); using the first", ", ".join(names))
        return names[0]

    def connect(self, card_number: str, pin: str, library_key: str = DEFAULT_LIBRARY) -> dict:
        """Sign in with a library card and keep only Libby's session token. Returns the first sync."""
        card_number = re.sub(r"\s", "", card_number or "")
        if not card_number:
            raise LibbyError("Enter your library card number.")
        library = self.find_library(library_key)

        primary = self._mint()
        try:
            self._request("POST", f"auth/link/{library['websiteId']}", token=primary["identity"],
                          json={"ils": self._ils(primary["identity"], library), "username": card_number, "password": pin or ""})
        except LibbyRateLimited:
            raise
        except LibbyError as exc:
            if "credentials_rejected" in str(exc) or " 401 " in str(exc):
                raise LibbyError(f"{library['name']} didn't accept that card number and PIN. "
                                 "Check them by signing in on the library's website.") from None
            raise
        # Like the Libby app: move the signed-in card to a second chip with a sync code,
        # then refresh that chip's identity so it carries the card.
        code = self._request("GET", "chip/clone/code", token=primary["identity"], params={"role": "primary"}).json()["code"]
        secondary = self._mint()
        self._request("POST", "chip/clone/code", token=secondary["identity"], json={"code": code, "role": "secondary"})
        chip = self._mint(token=secondary["identity"], chip_id=secondary["chip"])

        self.path.unlink(missing_ok=True)
        self._save_session(identity=chip["identity"], chip=secondary["chip"], library=library, linked=time.time())
        data = self.sync()
        if not data.get("cards"):
            self.path.unlink(missing_ok=True)
            raise LibbyError(f"Signed in, but Libby shows no {library['name']} card on the account. Please try again.")
        return data

    # --- Using the account

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
            res = self._send("GET", location, headers={"User-Agent": HEADERS["User-Agent"]})
            if res.status_code >= 400:
                raise LibbyError(f"Downloading the book from OverDrive failed ({res.status_code}).")
        return res.content

    def borrow(self, hold: dict, card: dict) -> dict:
        type_id = (hold.get("type") or {}).get("id", "ebook")
        payload = {"period": lending_days(card, type_id), "units": "days", "lucky_day": None, "title_format": type_id}
        return self._request("POST", f"card/{hold['cardId']}/loan/{hold['id']}", json=payload).json()


def _result(res: Any) -> str:
    try:
        return str((res.json() or {}).get("result") or "")
    except Exception:
        return ""


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
        libraries = sorted({library_name(c) for c in cards})
        if not libraries:
            try:
                libraries = [self.client.session()["library"]["name"]]
            except (LibbyError, KeyError, TypeError):
                pass
        return {
            "connected": True,
            "libraries": libraries,
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
