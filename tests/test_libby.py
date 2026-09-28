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
    """Just enough of sentry-read.svc.overdrive.com (and a download host) to test against."""

    def __init__(self):
        self.valid_code = "12345678"
        self.cards = [CARD]
        self.loans = []
        self.holds = []
        self.calls = []
        self.tokens = 0
        self.expired = set()
        self.rate_limited = False
        self.borrow_error = None
        self.open_epub = b""

    def _token(self):
        self.tokens += 1
        return f"token-{self.tokens}"

    def request(self, method, url, headers=None, timeout=None, json=None, params=None, allow_redirects=True):
        path = url.replace(libby.API + "/", "")
        auth = (headers or {}).get("Authorization", "")
        self.calls.append((method, path, auth, json))
        if url.startswith("https://download.example/"):
            return Response(content=self.open_epub)
        if self.rate_limited:
            return Response(403, {"result": "whoa"})
        if path == "chip" and method == "POST":
            return Response(body={"identity": self._token(), "chip": "c1"})
        if auth.removeprefix("Bearer ") in self.expired:
            return Response(401, {"result": "unauthorized"})
        if path == "chip/clone/code":
            if json["code"] != self.valid_code:
                return Response(400, {"result": "clone_code_invalid"})
            return Response(body={"result": "cloned"})
        if path == "chip/sync":
            return Response(body={"result": "synchronized", "cards": self.cards, "loans": self.loans, "holds": self.holds})
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
        return [p for _, p, _, _ in self.calls if p.startswith(prefix)]


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
    c.connect(fake.valid_code)
    fake.calls.clear()
    return c


@pytest.fixture
def watcher(lcfg, client):
    return LibbyWatcher(lcfg, JobStore(), client=client)


# --- Connecting

def test_connect_links_account_and_saves_identity(lcfg, fake):
    data = LibbyClient(lcfg.libby_file, http=fake).connect("1234 5678")
    assert [c["cardId"] for c in data["cards"]] == ["111"]
    # anonymous chip → clone with its token → chip again with that token → sync
    assert [(m, p) for m, p, _, _ in fake.calls] == [
        ("POST", "chip"), ("POST", "chip/clone/code"), ("POST", "chip"), ("GET", "chip/sync")]
    assert fake.calls[0][2] == ""
    assert fake.calls[1][2] == fake.calls[2][2] == "Bearer token-1"
    saved = json.loads(lcfg.libby_file.read_text())
    assert saved["identity"] == "token-2"
    assert stat.S_IMODE(lcfg.libby_file.stat().st_mode) == 0o600


@pytest.mark.parametrize("code", ["", "1234", "abcdefgh"])
def test_connect_rejects_malformed_code(lcfg, fake, code):
    with pytest.raises(LibbyError, match="8 digits"):
        LibbyClient(lcfg.libby_file, http=fake).connect(code)
    assert fake.calls == []


def test_connect_wrong_code(lcfg, fake):
    with pytest.raises(LibbyError, match="didn't accept that setup code"):
        LibbyClient(lcfg.libby_file, http=fake).connect("87654321")
    assert not lcfg.libby_file.exists()


def test_connect_account_without_cards(lcfg, fake):
    fake.cards = []
    with pytest.raises(LibbyError, match="no library cards"):
        LibbyClient(lcfg.libby_file, http=fake).connect(fake.valid_code)
    assert not lcfg.libby_file.exists()


def test_expired_token_is_refreshed_once(client, fake, lcfg):
    fake.expired.add("token-2")
    client.sync()
    assert fake.paths() == ["chip/sync", "chip", "chip/sync"]
    assert json.loads(lcfg.libby_file.read_text())["identity"] == "token-3"


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

    borrows = [(p, body) for m, p, _, body in fake.calls if m == "POST" and p.startswith("card/")]
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
    assert not [p for m, p, _, _ in fake.calls if m == "POST" and p.startswith("card/")]
    assert sent == []


def test_borrow_failure_notified_once_and_retried(watcher, fake, notices):
    watcher.poll_once()
    fake.holds = [ebook("4", "Ready Hold", isAvailable=True, placedDate="p1")]
    fake.borrow_error = "checkout_limit_reached"
    watcher.poll_once()
    watcher.poll_once()
    borrows = [p for m, p, _, _ in fake.calls if m == "POST" and p.startswith("card/")]
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


def test_api_connect_send_disconnect(api, fake, tmp_path, monkeypatch):
    assert api.get("/api/status", headers=AUTH).json()["libby"] == {"connected": False}
    assert api.post("/api/libby/connect", headers=AUTH, json={"code": "000"}).status_code == 400

    res = api.post("/api/libby/connect", headers=AUTH, json={"code": fake.valid_code})
    assert res.status_code == 200, res.text
    assert res.json()["libby"]["connected"] is True
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
