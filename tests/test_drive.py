import json
from dataclasses import replace

import pytest

from app import pipeline
from app.drive import DriveWatcher
from app.jobs import JobStore
from tests.test_pipeline import ACSM, cfg, notices, sent  # noqa: F401  (fixtures)


class FakeResponse:
    def __init__(self, body=None, content=b""):
        self._body = body
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeDrive:
    def __init__(self):
        self.files = {}  # id -> (metadata, content)
        self.downloads = []

    def add(self, file_id, name, content=b"", mime="application/octet-stream"):
        self.files[file_id] = ({"id": file_id, "name": name, "mimeType": mime, "size": str(len(content))}, content)

    def get(self, url, params=None, timeout=None):
        if url.endswith("/files"):
            assert "'folder123' in parents" in params["q"]
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


def test_first_poll_ignores_existing_files(watcher, drive, sent):
    drive.add("old", "old.acsm", ACSM)
    watcher.poll_once()
    assert sent == []
    assert json.loads(watcher.cfg.drive_state_file.read_text()) == {"seen": ["old"]}


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
    assert "new" in json.loads(watcher.cfg.drive_state_file.read_text())["seen"]
