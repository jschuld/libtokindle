import json
import stat
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import libby, main
from app.config import Config
from app.jobs import JobStore
from app.libby import LibbyClient, LibbyError, LibbyRateLimited, LibbyWatcher, lending_days
from tests.test_pipeline import ACSM, cfg, make_epub, notices, sent  # noqa: F401  (fixtures)

CARD = {"cardId": "111", "advantageKey": "aucklandlibraries", "library": {"name": "Auckland Libraries"},
        "lendingPeriods": {"book": {"options": [[7, "days"], [14, "days"], [21, "days"]], "preference": [14, "days"]}}}


def ebook(title_id, title, formats=("ebook-epub-adobe",), **extra):
    return {"id": title_id, "cardId": "111", "title": title, "firstCreatorName": "Author",
            "type": {"id": "ebook"}, "formats": [{"id": f} for f in formats], **extra}


class Response:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body
        self.content = content if content else json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode("utf-8", "replace")
        self.headers = headers or {}
        self.reason = ""

    def json(self):
        return self._body


class FakeLibby:
    """Just enough of Libby (sentry-read.svc.overdrive.com), OverDrive's library directory
    and a download host to test against. Sessions ("chips") only see the card once it has
    been signed in on them or cloned to them, like the real thing."""

    def __init__(self):
        self.card_number, self.pin = "21234000123456", "1234"
        self.cards = [CARD]
        self.loans = []
        self.holds = []
        self.calls = []
        self.tokens = 0
        self.linked = set()  # tokens whose chip has the card
        self.codes = {}
        self.expired = set()
        self.rate_limited = False
        self.borrow_error = None
        self.open_epub = b""
        self.forms = [{"ilsName": "aucklandlibraries", "type": "Local"}]

    def _token(self, linked=False):
        self.tokens += 1
        token = f"token-{self.tokens}"
        if linked:
            self.linked.add(token)
        return token

    def request(self, method, url, headers=None, timeout=None, json=None, params=None, allow_redirects=True):
        path = url.replace(libby.API + "/", "")
        auth = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        self.calls.append((method, path, auth, json, params))
        if url.startswith("https://download.example/"):
            return Response(content=self.open_epub)
        if url.startswith(libby.LIBRARIES_API):
            key = url.rsplit("/", 1)[1]
            if key != "aucklandlibraries":
                return Response(404, {"message": "not found"})
            return Response(body={"websiteId": 42, "preferredKey": "aucklandlibraries", "name": "Auckland Libraries"})
        if self.rate_limited:
            return Response(403, {"result": "whoa"})
        if path == "chip" and method == "POST":
            assert params["c"] == "d:22.1.1"
            if auth:  # refreshing an existing chip keeps its card
                assert params.get("v")
                return Response(body={"identity": self._token(linked=auth in self.linked), "chip": params["v"]})
            return Response(body={"identity": self._token(), "chip": f"chip{self.tokens}abcdef"})
        if auth in self.expired:
            return Response(401, {"result": "unauthorized"})
        if path == "auth/forms/42":
            return Response(body={"forms": self.forms})
        if path == "auth/link/42":
            if (json["username"], json["password"]) != (self.card_number, self.pin):
                return Response(401, {"result": "credentials_rejected"})
            assert json["ils"] == "aucklandlibraries"
            self.linked.add(auth)
            return Response(body={"result": "linked"})
        if path == "chip/clone/code" and method == "GET":
            assert params == {"role": "primary"}
            code = f"{len(self.codes) + 10000000}"
            self.codes[code] = auth in self.linked
            return Response(body={"code": code})
        if path == "chip/clone/code" and method == "POST":
            if json.get("role") != "secondary" or json["code"] not in self.codes:
                return Response(400, {"result": "clone_code_invalid"})
            if self.codes[json["code"]]:
                self.linked.add(auth)
            return Response(body={"result": "cloned"})
        if path == "chip/sync":
            cards = self.cards if auth in self.linked else []
            return Response(body={"result": "synchronized", "cards": cards, "loans": self.loans if cards else [],
                                  "holds": self.holds if cards else []})
        if path.startswith("open/"):
            return Response(body={"urls": {"openbook": "x"}})
        if "/fulfill/" in path:
            fmt = path.rsplit("/", 1)[1]
            if fmt == "ebook-epub-open":
                return Response(302, headers={"Location": "https://download.example/book.epub"})
            return Response(content=ACSM)
        if path.startswith("card/") and "/loan/" in path and method == "POST":
            if self.borrow_error:
                return Response(400, {"result": self.borrow_error})
            title_id = path.rsplit("/", 1)[1]
            hold = next(h for h in self.holds if h["id"] == title_id)
            self.holds.remove(hold)
            loan = {**hold, "checkoutDate": "2026-09-28T10:00:00Z"}
            loan.pop("isAvailable", None)
            self.loans.append(loan)
            return Response(body=loan)
        return Response(404, {"result": "not_found"})

    def paths(self, prefix=""):
        return [c[1] for c in self.calls if c[1].startswith(prefix)]


@pytest.fixture
def fake():
    return FakeLibby()


@pytest.fixture
def lcfg(cfg, tmp_path):
    return replace(cfg, libby_file=tmp_path / "libby.json", libby_state_file=tmp_path / "libby-state.json",
                   libby_auto_borrow=True)


@pytest.fixture
def client(lcfg, fake):
    c = LibbyClient(lcfg.libby_file, http=fake)
    c.connect(fake.card_number, fake.pin)
    fake.calls.clear()
    return c


@pytest.fixture
def watcher(lcfg, client):
    return LibbyWatcher(lcfg, JobStore(), client=client)


# --- Connecting

def test_connect_with_card_and_pin(lcfg, fake):
    data = LibbyClient(lcfg.libby_file, http=fake).connect(" 2123 4000 123456 ", fake.pin)
    assert [c["cardId"] for c in data["cards"]] == ["111"]
    steps = [(c[0], c[1].replace(libby.LIBRARIES_API, "libraries")) for c in fake.calls]
    assert steps == [
        ("GET", "libraries/aucklandlibraries"),
        ("POST", "chip"),                   # primary session
        ("GET", "auth/forms/42"),
        ("POST", "auth/link/42"),           # sign in with the card
        ("GET", "chip/clone/code"),         # sync code from the primary
        ("POST", "chip"),                   # secondary session
        ("POST", "chip/clone/code"),        # card copied to the secondary
        ("POST", "chip"),                   # refresh the secondary's identity
        ("GET", "chip/sync"),
    ]
    saved = json.loads(lcfg.libby_file.read_text())
    assert saved["identity"] == "token-3"
    assert saved["chip"] == "chip2abcdef"
    assert saved["library"] == {"key": "aucklandlibraries", "name": "Auckland Libraries", "websiteId": "42"}
    assert fake.pin not in lcfg.libby_file.read_text()  # the PIN is never stored
    assert stat.S_IMODE(lcfg.libby_file.stat().st_mode) == 0o600


def test_connect_wrong_pin(lcfg, fake):
    with pytest.raises(LibbyError, match="didn't accept that card number and PIN"):
        LibbyClient(lcfg.libby_file, http=fake).connect(fake.card_number, "0000")
    assert not lcfg.libby_file.exists()


def test_connect_unknown_library(lcfg, fake):
    with pytest.raises(LibbyError, match="doesn't know a library called “nowhere”"):
        LibbyClient(lcfg.libby_file, http=fake).connect(fake.card_number, fake.pin, "nowhere")


def test_library_key_can_be_a_libby_link(lcfg, fake):
    client = LibbyClient(lcfg.libby_file, http=fake)
    assert client.find_library("https://libbyapp.com/library/aucklandlibraries")["websiteId"] == "42"
    with pytest.raises(LibbyError, match="libbyapp.com/library/"):
        client.find_library("Auckland Libraries")


def test_connect_requires_card_number(lcfg, fake):
    with pytest.raises(LibbyError, match="card number"):
        LibbyClient(lcfg.libby_file, http=fake).connect("  ", fake.pin)
    assert fake.calls == []


def test_connect_picks_the_library_sign_in_form(lcfg, fake):
    fake.forms = [{"ilsName": "partnerlib"}, {"ilsName": "aucklandlibraries"}]
    LibbyClient(lcfg.libby_file, http=fake).connect(fake.card_number, fake.pin)
    link = next(c for c in fake.calls if c[1] == "auth/link/42")
    assert link[3]["ils"] == "aucklandlibraries"


def test_connect_session_without_card(lcfg, fake, monkeypatch):
    fake.cards = []
    with pytest.raises(LibbyError, match="shows no Auckland Libraries card"):
        LibbyClient(lcfg.libby_file, http=fake).connect(fake.card_number, fake.pin)
    assert not lcfg.libby_file.exists()


def test_expired_token_is_refreshed_once(client, fake, lcfg):
    fake.expired.add("token-3")
    client.sync()
    assert fake.paths() == ["chip/sync", "chip", "chip/sync"]
    refresh = fake.calls[1]
    assert refresh[2] == "token-3" and refresh[4]["v"] == "chip2abc"  # the saved chip's id
    saved = json.loads(lcfg.libby_file.read_text())
    assert saved["identity"] == "token-4"
    assert saved["library"]["name"] == "Auckland Libraries"  # refreshing keeps the rest


def test_rate_limit_is_recognised(client, fake):
    fake.rate_limited = True
    with pytest.raises(LibbyRateLimited):
        client.sync()


def test_lending_days():
    assert lending_days(CARD, "ebook") == 14
    assert lending_days({"lendingPeriods": {"ebook": {"preference": [0, "days"], "options": [[7, "days"], [21, "days"]]}}}, "ebook") == 21
    assert lending_days({}, "ebook") == 21


# --- Watching

def test_first_check_skips_existing_loans(watcher, fake, sent):
    fake.loans = [ebook("1", "Old Book", checkoutDate="2026-09-01T00:00:00Z")]
    watcher.poll_once()
    assert sent == []
    assert fake.paths("card/") == []


def test_new_adobe_loan_is_sent(watcher, fake, sent, notices):
    watcher.poll_once()
    fake.loans = [ebook("2", "A Time to Kill", checkoutDate="2026-09-28T09:00:00Z")]
    watcher.poll_once()
    watcher.poll_once()  # sent once only

    assert fake.paths("open/") == ["open/ebook/card/111/title/2"]
    assert fake.paths("card/") == ["card/111/loan/2/fulfill/ebook-epub-adobe"]
    assert len(sent) == 1
    [job] = watcher.store.recent()
    assert (job.source, job.status, job.filename) == ("libby", "done", "A Time to Kill.acsm")
    assert "from Libby" in notices[0][1]


def test_drm_free_loan_is_downloaded_from_redirect(watcher, fake, sent, tmp_path):
    book = tmp_path / "open.epub"
    make_epub(book, title="Open Book", author="Anon", drm=False)
    fake.open_epub = book.read_bytes()
    watcher.poll_once()
    fake.loans = [ebook("3", "Open Book", formats=("ebook-epub-adobe", "ebook-epub-open"), checkoutDate="d")]
    watcher.poll_once()
    assert fake.paths("card/") == ["card/111/loan/3/fulfill/ebook-epub-open"]
    assert sent[0][0] == "Open Book - Anon.epub"


def test_ready_hold_is_borrowed_and_sent(watcher, fake, sent):
    watcher.poll_once()
    fake.holds = [
        ebook("4", "Ready Hold", isAvailable=True, placedDate="p1"),
        ebook("5", "Still Waiting", isAvailable=False, placedDate="p2"),
        {**ebook("6", "An Audiobook", isAvailable=True, placedDate="p3"), "type": {"id": "audiobook"}},
    ]
    watcher.poll_once()

    borrows = [(c[1], c[3]) for c in fake.calls if c[0] == "POST" and c[1].startswith("card/")]
    assert borrows == [("card/111/loan/4", {"period": 14, "units": "days", "lucky_day": None, "title_format": "ebook"})]
    assert len(sent) == 1
    assert watcher.store.recent()[0].filename == "Ready Hold.acsm"


def test_ready_hold_borrowed_on_first_check_is_sent(watcher, fake, sent):
    fake.loans = [ebook("1", "Old Book", checkoutDate="old")]
    fake.holds = [ebook("4", "Ready Hold", isAvailable=True, placedDate="p1")]
    watcher.poll_once()
    assert len(sent) == 1
    assert [j.filename for j in watcher.store.recent()] == ["Ready Hold.acsm"]


def test_auto_borrow_off(watcher, fake, sent):
    watcher.cfg = replace(watcher.cfg, libby_auto_borrow=False)
    watcher.poll_once()
    fake.holds = [ebook("4", "Ready Hold", isAvailable=True, placedDate="p1")]
    watcher.poll_once()
    assert not [c for c in fake.calls if c[0] == "POST" and c[1].startswith("card/")]
    assert sent == []


def test_borrow_failure_notified_once_and_retried(watcher, fake, notices):
    watcher.poll_once()
    fake.holds = [ebook("4", "Ready Hold", isAvailable=True, placedDate="p1")]
    fake.borrow_error = "checkout_limit_reached"
    watcher.poll_once()
    watcher.poll_once()
    borrows = [c for c in fake.calls if c[0] == "POST" and c[1].startswith("card/")]
    assert len(borrows) == 2  # retried each check
    assert len(notices) == 1  # but only one email
    assert "loan limit" in notices[0][1]


def test_loan_without_download_is_reported(watcher, fake, sent, notices):
    watcher.poll_once()
    fake.loans = [ebook("7", "Web Only", formats=("ebook-overdrive",), checkoutDate="d")]
    watcher.poll_once()
    assert sent == []
    assert "only be read in the Libby app" in notices[0][1]


def test_audiobook_loan_is_ignored(watcher, fake, sent, notices):
    watcher.poll_once()
    fake.loans = [{**ebook("8", "Listen", checkoutDate="d"), "type": {"id": "audiobook"}}]
    watcher.poll_once()
    assert sent == [] and notices == [] and watcher.store.recent() == []


def test_borrowing_same_title_again_later_sends_again(watcher, fake, sent):
    watcher.poll_once()
    fake.loans = [ebook("2", "Again", checkoutDate="first")]
    watcher.poll_once()
    fake.loans = [ebook("2", "Again", checkoutDate="second")]
    watcher.poll_once()
    assert len(sent) == 2


def test_send_existing_loan_on_demand(watcher, fake, sent):
    fake.loans = [ebook("1", "Old Book", checkoutDate="old")]
    watcher.poll_once()
    [loan] = watcher.loans()
    assert loan["sendable"] and loan["handled"]
    watcher.send_by_key(loan["key"])
    assert len(sent) == 1


def test_rate_limit_backs_off(watcher, fake):
    fake.rate_limited = True
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            watcher._stop.set()

    watcher._wake.wait = fake_wait
    watcher.run()
    assert waits == [3600, 7200, 14400]  # 30 min doubled, capped at 4 hours
    assert "limiting" in watcher.last_error


# --- API and settings

@pytest.fixture
def api(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("LIBBY_FILE", str(tmp_path / "libby.json"))
    monkeypatch.setenv("LIBBY_STATE_FILE", str(tmp_path / "libby-state.json"))
    monkeypatch.setenv("UPLOAD_TOKEN", "a-long-token-123")
    monkeypatch.setattr(libby, "LibbyClient", lambda path, http=None: LibbyClient(path, http=fake))
    monkeypatch.setattr(main, "LibbyClient", lambda path, http=None: LibbyClient(path, http=fake))
    monkeypatch.setattr(LibbyWatcher, "start", lambda self: None)  # no background thread in tests
    main.apply_config(Config.load())
    yield TestClient(main.app)
    main.apply_config(Config.from_env({}))


AUTH = {"Authorization": "Bearer a-long-token-123"}


def test_api_connect_send_disconnect(api, fake, tmp_path, monkeypatch, caplog):
    assert api.get("/api/status", headers=AUTH).json()["libby"] == {"connected": False}
    bad = api.post("/api/libby/connect", headers=AUTH, json={"card_number": fake.card_number, "pin": "0000"})
    assert bad.status_code == 400 and "PIN" in bad.json()["detail"]

    res = api.post("/api/libby/connect", headers=AUTH, json={"card_number": fake.card_number, "pin": fake.pin})
    assert res.status_code == 200, res.text
    assert res.json()["libby"]["connected"] is True
    assert res.json()["libby"]["libraries"] == ["Auckland Libraries"]
    assert fake.pin not in caplog.text and "0000" not in caplog.text  # PINs are never logged
    assert main.libby_watcher is not None

    fake.loans = [ebook("1", "Old Book", checkoutDate="old")]
    loans = api.get("/api/libby/loans", headers=AUTH).json()["loans"]
    assert [l["title"] for l in loans] == ["Old Book"]

    sends = []
    monkeypatch.setattr(main.libby_watcher, "send_by_key", sends.append)
    assert api.post("/api/libby/send", headers=AUTH, json={"key": loans[0]["key"]}).status_code == 200
    assert sends == [loans[0]["key"]]
    assert api.post("/api/libby/send", headers=AUTH, json={"key": "nope"}).status_code == 404

    assert api.post("/api/libby/disconnect", headers=AUTH).status_code == 200
    assert not (tmp_path / "libby.json").exists()
    assert main.libby_watcher is None
    assert api.post("/api/libby/check", headers=AUTH).status_code == 400


def test_libby_settings_validation(api):
    res = api.put("/api/settings", headers=AUTH, json={"LIBBY_POLL_MINUTES": "5"})
    assert res.status_code == 400 and "15 minutes" in res.json()["detail"]
    res = api.put("/api/settings", headers=AUTH, json={"LIBBY_AUTO_BORROW": "false", "LIBBY_POLL_MINUTES": "45"})
    assert res.json()["values"]["LIBBY_AUTO_BORROW"] == "false"
    assert main.cfg.libby_auto_borrow is False and main.cfg.libby_poll_minutes == 45


def test_config_defaults():
    c = Config.from_env({})
    assert c.libby_poll_minutes == 30 and c.libby_auto_borrow is True
    assert Config.from_env({"LIBBY_POLL_MINUTES": "1"}).libby_poll_minutes == 15


# --- Network and certificate problems

MISMATCH = ("HTTPSConnectionPool(host='sentry-read.svc.overdrive.com', port=443): Max retries exceeded "
            "(Caused by SSLError(SSLCertVerificationError(1, \"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify "
            "failed: Hostname mismatch, certificate is not valid for 'sentry-read.svc.overdrive.com'. (_ssl.c:1007)\")))")


class Failing:
    """An HTTP session whose requests to Libby's API host fail; others go to `fallback`."""

    def __init__(self, error, fallback):
        self.error, self.fallback, self.api_calls = error, fallback, 0

    def request(self, method, url, **kwargs):
        if url.startswith(libby.API + "/"):
            self.api_calls += 1
            raise self.error
        return self.fallback.request(method, url, **kwargs)


def test_edge_certificate_mismatch_falls_back_to_edge_session(lcfg, fake, caplog):
    """The exact error from the user's log: the edge session takes over for Libby's API."""
    import requests

    broken = Failing(requests.exceptions.SSLError(MISMATCH), fake)
    client = LibbyClient(lcfg.libby_file, http=broken)
    edges = []
    client._edge_factory = lambda: edges.append(1) or fake
    client.connect(fake.card_number, fake.pin)
    assert broken.api_calls == 1 and edges == [1]  # detected once, then the edge session is used
    assert client.sync()["cards"]
    assert caplog.text.count("OverDrive's edge network") == 1


def test_other_certificate_errors_are_not_bypassed(lcfg, fake):
    import requests

    error = requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")
    client = LibbyClient(lcfg.libby_file, http=Failing(error, fake))
    client._edge_factory = lambda: pytest.fail("must not fall back for a certificate that isn't trusted")
    with pytest.raises(LibbyError, match="secure connection"):
        client.connect(fake.card_number, fake.pin)


def test_network_errors_become_readable(lcfg, fake):
    import requests

    client = LibbyClient(lcfg.libby_file, http=Failing(requests.exceptions.ConnectionError("Name or service not known"), fake))
    with pytest.raises(LibbyError, match="Couldn't reach Libby"):
        client.connect(fake.card_number, fake.pin)


def test_connect_endpoint_reports_network_error_instead_of_crashing(api, fake, monkeypatch):
    import requests

    broken = Failing(requests.exceptions.ConnectionError("timed out"), fake)
    monkeypatch.setattr(main, "LibbyClient", lambda path, http=None: LibbyClient(path, http=broken))
    res = api.post("/api/libby/connect", headers=AUTH, json={"card_number": fake.card_number, "pin": fake.pin})
    assert res.status_code == 400
    assert "Couldn't reach Libby" in res.json()["detail"]
