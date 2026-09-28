import datetime as dt
import json
import logging
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import logs, main
from app.config import Config
from app.drive import DriveWatcher
from app.jobs import JobStore
from tests.test_pipeline import cfg, notices, sent  # noqa: F401  (fixtures)


# --- History

def test_history_survives_restart(tmp_path):
    path = tmp_path / "history.json"
    store = JobStore(path)
    done = store.create("a.acsm")
    store.update(done, status="done", title="A Book")
    running = store.create("b.acsm", source="drive")
    store.update(running, status="sending")

    reloaded = JobStore(path)
    jobs = {j.filename: j for j in reloaded.recent()}
    assert jobs["a.acsm"].status == "done" and jobs["a.acsm"].title == "A Book"
    # A job cut off by a restart is reported as failed, not left "in progress" forever.
    assert jobs["b.acsm"].status == "failed"
    assert "restarted" in jobs["b.acsm"].error
    assert jobs["b.acsm"].source == "drive"


def test_history_drops_entries_past_retention(tmp_path):
    path = tmp_path / "history.json"
    old_time = time.time() - 31 * 86400
    path.write_text(json.dumps([
        {"id": "old", "filename": "old.acsm", "status": "done", "created": old_time, "updated": old_time},
        {"id": "new", "filename": "new.acsm", "status": "done", "created": time.time(), "updated": time.time()},
    ]))
    store = JobStore(path, retention_days=30)
    assert [j.id for j in store.recent()] == ["new"]

    store.retention_days = 1
    store.update(store.get("new"), created=time.time() - 2 * 86400)
    store.prune()
    assert store.recent() == []
    assert json.loads(path.read_text()) == []


def test_damaged_history_file_is_ignored(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{not json")
    store = JobStore(path)
    store.create("x.acsm")
    assert len(json.loads(path.read_text())) == 1


# --- Log files

def make_logs(log_dir, dates):
    log_dir.mkdir()
    (log_dir / "libtokindle.log").write_text("today\n")
    for date in dates:
        (log_dir / f"libtokindle.log.{date}").write_text(f"{date}\n")


def test_prune_deletes_logs_older_than_retention(tmp_path):
    log_dir = tmp_path / "logs"
    make_logs(log_dir, ["2026-09-27", "2026-08-29", "2026-08-28", "2026-06-01"])
    (log_dir / "unrelated.txt").write_text("keep me")

    deleted = logs.prune(log_dir, 30, today=dt.date(2026, 9, 28))

    assert sorted(deleted) == ["libtokindle.log.2026-06-01", "libtokindle.log.2026-08-28"]
    remaining = sorted(p.name for p in log_dir.iterdir())
    assert remaining == ["libtokindle.log", "libtokindle.log.2026-08-29", "libtokindle.log.2026-09-27", "unrelated.txt"]


def test_list_files_newest_first(tmp_path):
    log_dir = tmp_path / "logs"
    make_logs(log_dir, ["2026-09-26", "2026-09-27"])
    names = [f["name"] for f in logs.list_files(log_dir)]
    assert names[0] == "libtokindle.log"
    assert names[1:] == ["libtokindle.log.2026-09-27", "libtokindle.log.2026-09-26"]


def test_read_problems_only_keeps_tracebacks(tmp_path):
    (tmp_path / "libtokindle.log").write_text(
        "2026-09-28 10:00:00,000 INFO app.pipeline: Processing a.acsm (from upload)\n"
        "2026-09-28 10:00:01,000 ERROR app.drive: Drive check failed\n"
        "Traceback (most recent call last):\n"
        "  File \"drive.py\", line 1\n"
        "2026-09-28 10:00:02,000 INFO app.pipeline: Sent to Kindle: A\n"
        "2026-09-28 10:00:03,000 WARNING app.pipeline: Not sent: b.acsm\n"
    )
    lines = logs.read(tmp_path, "libtokindle.log", problems_only=True)
    assert lines == [
        "2026-09-28 10:00:01,000 ERROR app.drive: Drive check failed",
        "Traceback (most recent call last):",
        '  File "drive.py", line 1',
        "2026-09-28 10:00:03,000 WARNING app.pipeline: Not sent: b.acsm",
    ]
    assert len(logs.read(tmp_path, "libtokindle.log")) == 6


@pytest.mark.parametrize("name", ["../settings.json", "settings.json", "libtokindle.log.../../x", "/etc/passwd"])
def test_read_only_accepts_log_names(tmp_path, name):
    with pytest.raises(FileNotFoundError):
        logs.read(tmp_path, name)


def test_file_handler_writes_and_retention_updates(tmp_path):
    log_dir = tmp_path / "logs"
    logs.setup(log_dir, 7)
    try:
        logging.getLogger("app.test").info("hello from the test")
        logs._file_handler.flush()
        assert "hello from the test" in (log_dir / "libtokindle.log").read_text()
        logs.set_retention(3)
        assert logs._file_handler.backupCount == 3
    finally:
        logs.setup(tmp_path / "unused", 30)


def test_retention_is_capped_at_30_days():
    assert Config.from_env({"LOG_RETENTION_DAYS": "90"}).retention_days == 30
    assert Config.from_env({"LOG_RETENTION_DAYS": "0"}).retention_days == 1
    assert Config.from_env({}).retention_days == 30


# --- Drive errors are logged once, not every minute

def test_repeated_drive_error_logged_once(cfg, caplog):
    class Broken:
        def get(self, *args, **kwargs):
            raise OSError("network down")

    watcher = DriveWatcher(replace(cfg, drive_folder_id="f"), JobStore(), session=Broken())
    rounds = []

    class CountingStop:
        def is_set(self):
            return len(rounds) >= 3

        def wait(self, seconds):
            rounds.append(seconds)

    with caplog.at_level(logging.INFO, logger="app.drive"):
        watcher.run(CountingStop())
    failures = [r for r in caplog.records if r.getMessage() == "Drive check failed"]
    assert len(failures) == 1
    assert watcher.last_error == "network down"


# --- API

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "cfg", replace(main.cfg, upload_token="a-long-token-123"))
    return TestClient(main.app)


AUTH = {"Authorization": "Bearer a-long-token-123"}


def test_logs_api(client, tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    make_logs(log_dir, ["2026-09-27"])
    monkeypatch.setattr(main, "cfg", replace(main.cfg, log_dir=log_dir))

    assert client.get("/api/logs").status_code == 401
    res = client.get("/api/logs", headers=AUTH).json()
    assert res["file"] == "libtokindle.log"
    assert res["lines"] == ["today"]
    assert res["retention_days"] == 30
    assert client.get("/api/logs?file=libtokindle.log.2026-09-27", headers=AUTH).json()["lines"] == ["2026-09-27"]
    assert client.get("/api/logs?file=../settings.json", headers=AUTH).status_code == 404


def test_history_api(client):
    job = main.store.create("history-test.acsm")
    main.store.update(job, status="done")
    assert client.get("/api/history").status_code == 401
    jobs = client.get("/api/history", headers=AUTH).json()["jobs"]
    assert any(j["id"] == job.id for j in jobs)


def test_logs_page_served(client):
    assert client.get("/logs").status_code == 200
