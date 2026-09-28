import json
from dataclasses import replace

import pytest

from app import pipeline
from app.drive import DriveError, DriveWatcher, _now_rfc3339
from app.jobs import JobStore
from tests.test_pipeline import ACSM, cfg, notices, sent  # noqa: F401  (fixtures)

LONG_AGO = "2020-01-01T00:00:00.000Z"


class FakeResponse:
    def __init__(self, body=None, content=b"", status_code=200):
        self._body = body
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._body


class FakeDrive:
    def __init__(self):
        self.files = {}  # id -> (metadata, content)
        self.downloads = []
        self.error = None  # (status, message) to fail every request with

    def add(self, file_id, name, content=b"", mime="application/octet-stream", created=None):
        meta = {"id": file_id, "name": name, "mimeType": mime, "size": str(len(content)),
                "createdTime": created or _now_rfc3339()}
        self.files[file_id] = (meta, content)

    def get(self, url, params=None, timeout=None):
        if self.error:
            status, message = self.error
            return FakeResponse({"error": {"code": status, "message": message}}, status_code=status)
        if url.endswith("/files"):
            assert "'folder123' in parents" in params["q"]
            assert "createdTime" in params["fields"]
            return FakeResponse({"files": [meta for meta, _ in self.files.values()]})
        file_id = url.rsplit("/", 1)[1]
        self.downloads.append(file_id)
        return FakeResponse(content=self.files[file_id][1])


@pytest.fixture
def drive():
    return FakeDrive()


@pytest.fixture
def watcher(cfg, drive):
    return DriveWatcher(replace(cfg, drive_folder_id="folder123"), JobStore(), session=drive)


def state(watcher):
    return json.loads(watcher.cfg.drive_state_file.read_text())


def test_files_already_in_folder_are_skipped(watcher, drive, sent):
    drive.add("old", "old.acsm", ACSM, created=LONG_AGO)
    watcher.poll_once()
    assert sent == []
    assert drive.downloads == []
    assert state(watcher)["seen"] == ["old"]
    assert state(watcher)["folder"] == "folder123"


def test_file_added_while_checks_fail_is_still_sent(watcher, drive, sent):
    """The watcher starts, Drive fails for a while, a file is added, then Drive works again."""
    drive.error = (404, "File not found: folder123.")
    with pytest.raises(DriveError):
        watcher.poll_once()
    drive.add("new", "loan.acsm", ACSM)
    drive.error = None
    watcher.poll_once()
    assert len(sent) == 1


def test_new_acsm_is_sent_once(watcher, drive, sent, notices):
    watcher.poll_once()  # first run: empty folder
    drive.add("new", "loan.acsm", ACSM)
    watcher.poll_once()
    watcher.poll_once()
    assert len(sent) == 1
    assert drive.downloads == ["new"]
    [job] = watcher.store.recent()
    assert (job.source, job.status) == ("drive", "done")
    assert notices[0][0].startswith("✅")
    assert "Google Drive" in notices[0][1]


def test_changing_folder_starts_fresh(watcher, drive, sent):
    watcher.cfg.drive_state_file.write_text(json.dumps({"folder": "other", "since": LONG_AGO, "seen": []}))
    drive.add("old", "old.acsm", ACSM, created=LONG_AGO)
    watcher.poll_once()
    assert sent == []  # counted as already in the (new) folder
    assert state(watcher)["folder"] == "folder123"


def test_state_from_older_version_keeps_working(watcher, drive, sent):
    watcher.cfg.drive_state_file.write_text(json.dumps({"seen": ["old"]}))
    drive.add("old", "old.acsm", ACSM, created=LONG_AGO)
    drive.add("new", "new.acsm", ACSM, created=LONG_AGO)
    watcher.poll_once()
    assert drive.downloads == ["new"]


def test_folder_not_found_error_says_what_to_fix(watcher, drive):
    watcher.cfg.google_credentials.parent.mkdir(parents=True, exist_ok=True)
    watcher.cfg.google_credentials.write_text(json.dumps({"client_email": "bot@proj.iam.gserviceaccount.com"}))
    drive.error = (404, "File not found: folder123.")
    with pytest.raises(DriveError) as exc:
        watcher.poll_once()
    message = str(exc.value)
    assert "can't find the folder" in message
    assert "bot@proj.iam.gserviceaccount.com" in message
    assert "File not found" in message


def test_access_refused_error(watcher, drive):
    drive.error = (403, "Google Drive API has not been used in project 123 before or it is disabled.")
    with pytest.raises(DriveError, match="Drive API is enabled"):
        watcher.poll_once()


def test_google_doc_fails_with_notice(watcher, drive, sent, notices):
    watcher.poll_once()
    drive.add("doc", "Reading list", mime="application/vnd.google-apps.document")
    watcher.poll_once()
    assert sent == []
    assert drive.downloads == []
    [job] = watcher.store.recent()
    assert job.status == "failed"
    assert notices[0][0] == "❌ Not sent to Kindle: Reading list"


def test_unsupported_file_not_downloaded(watcher, drive, notices):
    watcher.poll_once()
    drive.add("mp3", "song.mp3", b"ID3")
    watcher.poll_once()
    assert drive.downloads == []
    assert watcher.store.recent()[0].status == "failed"


def test_file_marked_seen_even_if_processing_crashes(watcher, drive, monkeypatch):
    watcher.poll_once()
    drive.add("new", "loan.acsm", ACSM)

    def crash(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "process", crash)
    with pytest.raises(RuntimeError):
        watcher.poll_once()
    assert "new" in state(watcher)["seen"]
