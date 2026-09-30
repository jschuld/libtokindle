import os
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import books, main, pipeline
from app.jobs import JobStore
from tests.test_pipeline import ACSM, cfg, make_epub, notices, sent  # noqa: F401  (fixtures)


# --- Pipeline keeps a copy and attaches it to the result email

def test_converted_book_is_kept_and_attached(cfg, notices):
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)
    assert job.status == "done"
    assert job.book == "Te Kōrero A Story - Some Author.epub"
    assert (cfg.books_dir / job.book).is_file()
    assert notices.attachments == [job.book]
    assert "attached" in notices[0][1]


def test_book_kept_when_kindle_email_fails(cfg, notices, monkeypatch):
    def fail(*args):
        raise OSError("SMTP down")

    monkeypatch.setattr(pipeline.mailer, "send_to_kindle", fail)
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)
    assert job.status == "failed"
    assert (cfg.books_dir / job.book).is_file()
    assert notices.attachments == [None]  # failure emails carry no attachment
    assert "Downloads page" in notices[0][1]


def test_failed_conversion_keeps_nothing(cfg, notices, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "fulfil")
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)
    assert job.book is None
    assert not cfg.books_dir.exists() or not any(cfg.books_dir.iterdir())
    assert "Downloads" not in notices[0][1]


def test_big_book_is_not_attached(cfg, notices, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_RESULT_ATTACHMENT_BYTES", 10)
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)
    assert job.status == "done"
    assert notices.attachments == [None]
    assert "too big to attach" in notices[0][1]


def test_real_email_has_the_attachment(tmp_path, monkeypatch):
    """The real mailer (the cfg fixture replaces send_notification, so it isn't used here)."""
    from app.config import Config

    cfg = Config.from_env({"SMTP_USER": "me@gmail.com"})
    sent_messages = []
    monkeypatch.setattr(pipeline.mailer, "_send", lambda cfg, msg: sent_messages.append(msg))
    book = tmp_path / "Book - Author.epub"
    make_epub(book, drm=False)
    pipeline.mailer.send_notification(cfg, "subject", "body", attachment=book)
    [msg] = sent_messages
    [part] = list(msg.iter_attachments())
    assert part.get_filename() == "Book - Author.epub"
    assert part.get_content_type() == "application/epub+zip"


# --- The books folder

def test_path_for_only_accepts_plain_book_names(tmp_path):
    (tmp_path / "ok.epub").write_text("x")
    (tmp_path / ".hidden.epub").write_text("x")
    (tmp_path / "notes.json").write_text("x")
    assert books.path_for(tmp_path, "ok.epub") == tmp_path / "ok.epub"
    for bad in ["../ok.epub", "/etc/passwd", ".hidden.epub", "notes.json", "missing.epub", ""]:
        with pytest.raises(FileNotFoundError):
            books.path_for(tmp_path, bad)


def test_prune_by_age(tmp_path):
    old, new = tmp_path / "old.epub", tmp_path / "new.epub"
    old.write_text("x")
    new.write_text("x")
    long_ago = time.time() - 31 * 86400
    os.utime(old, (long_ago, long_ago))
    assert books.prune(tmp_path, 30) == ["old.epub"]
    assert [b["name"] for b in books.list_books(tmp_path, 30)] == ["new.epub"]


def test_signed_links():
    expires, sig = books.sign("secret", "a.epub", now=1000)
    assert books.verify("secret", "a.epub", expires, sig, now=1000)
    assert not books.verify("secret", "b.epub", expires, sig, now=1000)  # other book
    assert not books.verify("other", "a.epub", expires, sig, now=1000)   # token changed
    assert not books.verify("secret", "a.epub", expires, sig, now=expires + 1)  # expired
    assert not books.verify("", "a.epub", expires, sig, now=1000)


# --- API

AUTH = {"Authorization": "Bearer a-long-token-123"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "cfg", replace(main.cfg, upload_token="a-long-token-123", books_dir=tmp_path))
    (tmp_path / "Te Kōrero - Author.epub").write_bytes(b"EPUB!")
    return TestClient(main.app)


def test_list_and_download(api):
    assert api.get("/api/books").status_code == 401
    [book] = api.get("/api/books", headers=AUTH).json()["books"]
    assert book["name"] == "Te Kōrero - Author.epub" and book["size"] == 5

    url = api.post("/api/books/link", headers=AUTH, json={"name": book["name"]}).json()["url"]
    res = api.get(url)  # no token header: the signed link is enough
    assert res.status_code == 200 and res.content == b"EPUB!"
    assert res.headers["content-type"] == "application/epub+zip"
    assert "attachment" in res.headers["content-disposition"]


def test_download_needs_a_valid_link(api):
    name = "Te Kōrero - Author.epub"
    assert api.get(f"/download/{name}").status_code == 403
    url = api.post("/api/books/link", headers=AUTH, json={"name": name}).json()["url"]
    assert api.get(url.replace("signature=", "signature=0")).status_code == 403
    assert api.post("/api/books/link", headers=AUTH, json={"name": "../settings.json"}).status_code == 404


def test_delete(api, tmp_path):
    name = "Te Kōrero - Author.epub"
    assert api.delete(f"/api/books/{name}").status_code == 401
    assert api.delete(f"/api/books/{name}", headers=AUTH).status_code == 200
    assert not (tmp_path / name).exists()
    assert api.delete(f"/api/books/{name}", headers=AUTH).status_code == 404


def test_downloads_page_served(api):
    assert api.get("/downloads").status_code == 200
