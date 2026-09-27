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
    monkeypatch.setattr(pipeline.mailer, "send_to_kindle", lambda c, book: books.append((book.name, zipfile.ZipFile(book).namelist())))
    return books


@pytest.fixture
def cfg(tmp_path, sent):
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
        dst = os.path.join(sys.argv[sys.argv.index("--output-dir") + 1], sys.argv[sys.argv.index("--output-file") + 1])
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
