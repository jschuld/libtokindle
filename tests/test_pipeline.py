import stat
import sys
import textwrap
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from app import pipeline
from app.config import Config
from app.jobs import JobStore

ACSM = b"""<?xml version="1.0"?>
<fulfillmentToken fulfillmentType="loan" xmlns="http://ns.adobe.com/adept">
  <operatorURL>https://acs.example/fulfillment</operatorURL>
</fulfillmentToken>"""


def make_epub(path: Path, title="A Book", author="Some Author", drm=True) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata></package>",
        )
        if drm:
            zf.writestr("META-INF/rights.xml", "<rights/>")


def fake_tool(path: Path, body: str) -> str:
    script = f"#!{sys.executable}\nimport sys, shutil, os, zipfile\n" + textwrap.dedent(body)
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def sent(monkeypatch):
    books = []

    def send(cfg, book):
        entries = zipfile.ZipFile(book).namelist() if book.suffix == ".epub" else None
        books.append((book.name, entries))

    monkeypatch.setattr(pipeline.mailer, "send_to_kindle", send)
    return books


@pytest.fixture
def notices(monkeypatch):
    class Notices(list):
        """(subject, body) per email; .attachments has each email's attached file name or None."""
        attachments: list

    messages = Notices()
    messages.attachments = []

    def send(cfg, subject, body, attachment=None):
        messages.append((subject, body))
        messages.attachments.append(attachment.name if attachment else None)

    monkeypatch.setattr(pipeline.mailer, "send_notification", send)
    return messages


@pytest.fixture
def cfg(tmp_path, sent, notices):
    epub_src = tmp_path / "source.epub"
    make_epub(epub_src, title="Te Kōrero: A Story?")
    downloader = fake_tool(
        tmp_path / "acsmdownloader",
        f"""
        if os.environ.get("FAKE_FAIL") == "fulfil":
            print("Error : E_LIC_ALREADY_FULFILLED_BY_ANOTHER_USER", file=sys.stderr); sys.exit(1)
        out = sys.argv[sys.argv.index("--output-dir") + 1]
        assert sys.argv[-1].endswith(".acsm")
        shutil.copyfile({str(epub_src)!r}, os.path.join(out, "Te Korero.epub"))
        """,
    )
    remover = fake_tool(
        tmp_path / "adept_remove",
        """
        src = sys.argv[-1]
        if {"-O", "--output-dir"} & set(sys.argv) and {"-o", "--output-file"} & set(sys.argv):
            print("Error : you cannot use both -o and -O", file=sys.stderr); sys.exit(1)
        dst = sys.argv[sys.argv.index("--output-file") + 1]
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
            for item in zin.namelist():
                if item != "META-INF/rights.xml":
                    zout.writestr(item, zin.read(item))
        """,
    )
    adept = tmp_path / "adept"
    adept.mkdir()
    (adept / "activation.xml").write_text("<activation/>")
    return replace(
        Config.from_env(),
        upload_token="secret",
        kindle_email="me@kindle.com",
        smtp_user="me@gmail.com",
        smtp_password="pw",
        adept_dir=adept,
        work_dir=tmp_path / "work",
        acsmdownloader=downloader,
        adept_remove=remover,
        notify_email="me@gmail.com",
        drive_folder_id="",
        drive_state_file=tmp_path / "drive-state.json",
        books_dir=tmp_path / "books",
    )


def test_validate_acsm_accepts_fulfillment_token():
    pipeline.validate_acsm(ACSM)


@pytest.mark.parametrize("data", [b"", b"not xml", b"<html></html>", b"x" * (pipeline.MAX_ACSM_BYTES + 1)])
def test_validate_acsm_rejects_other_files(data):
    with pytest.raises(pipeline.PipelineError):
        pipeline.validate_acsm(data)


def test_process_sends_drm_free_book(cfg, sent):
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)

    assert job.status == "done", job.error
    assert job.title == "Te Kōrero: A Story?"
    [(name, entries)] = sent
    assert name == "Te Kōrero A Story - Some Author.epub"
    assert "META-INF/rights.xml" not in entries
    assert list((cfg.work_dir).iterdir()) == []  # work files cleaned up


def test_process_reports_fulfilment_error(cfg, sent, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "fulfil")
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)

    assert job.status == "failed"
    assert "E_LIC_ALREADY_FULFILLED" in job.error
    assert "download it again from Libby" in job.error
    assert sent == []


def test_unprotected_epub_skips_adept_remove(cfg, tmp_path):
    book = tmp_path / "free.epub"
    make_epub(book, drm=False)
    workdir = tmp_path / "w"
    workdir.mkdir()
    out = pipeline.remove_drm(replace(cfg, adept_remove="/nonexistent"), workdir, book)
    assert out.read_bytes() == book.read_bytes()


def test_nice_filename_fallbacks():
    assert pipeline.nice_filename(None, None, ".PDF") == "book.pdf"
    assert pipeline.nice_filename("a/b", None, ".epub") == "ab.epub"


def test_success_sends_notification(cfg, notices):
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)
    [(subject, body)] = notices
    assert subject == "✅ Sent to Kindle: Te Kōrero: A Story?"
    assert "me@kindle.com" in body


def test_failure_sends_notification(cfg, notices, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "fulfil")
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)
    [(subject, body)] = notices
    assert subject == "❌ Not sent to Kindle: loan.acsm"
    assert "E_LIC_ALREADY_FULFILLED" in body


def test_notifications_can_be_turned_off(cfg, notices):
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(replace(cfg, notify_email=""), store, job, ACSM)
    assert job.status == "done"
    assert notices == []


def test_notification_failure_does_not_fail_job(cfg, monkeypatch):
    def boom(*args):
        raise OSError("smtp down")

    monkeypatch.setattr(pipeline.mailer, "send_notification", boom)
    store = JobStore()
    job = store.create("loan.acsm")
    pipeline.process(cfg, store, job, ACSM)
    assert job.status == "done"


def test_plain_epub_is_sent_as_is(cfg, sent, tmp_path):
    book = tmp_path / "free.epub"
    make_epub(book, title="Free Book", author="Anon", drm=False)
    store = JobStore()
    job = store.create("whatever.epub", source="drive")
    pipeline.process(cfg, store, job, book.read_bytes())
    assert job.status == "done", job.error
    assert sent[0][0] == "Free Book - Anon.epub"


def test_pdf_named_after_file(cfg, sent):
    store = JobStore()
    job = store.create("My Notes.pdf")
    pipeline.process(cfg, store, job, b"%PDF-1.4 ...")
    assert job.status == "done", job.error
    assert sent == [("My Notes.pdf", None)]


def test_drm_epub_is_rejected(cfg, sent, tmp_path):
    book = tmp_path / "locked.epub"
    make_epub(book, drm=True)
    store = JobStore()
    job = store.create("locked.epub")
    pipeline.process(cfg, store, job, book.read_bytes())
    assert job.status == "failed"
    assert ".acsm" in job.error
    assert sent == []


def test_unsupported_file_is_rejected(cfg, sent):
    store = JobStore()
    job = store.create("song.mp3")
    pipeline.process(cfg, store, job, b"ID3")
    assert job.status == "failed"
    assert sent == []
