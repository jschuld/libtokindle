"""The .acsm → DRM-free book → Kindle pipeline, built on libgourou's command-line tools."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from . import mailer
from .config import Config
from .jobs import Job, JobStore

log = logging.getLogger(__name__)

MAX_ACSM_BYTES = 256 * 1024


class PipelineError(Exception):
    """A failure with a message meant for the person using the web page."""


def validate_acsm(data: bytes) -> None:
    if not data:
        raise PipelineError("The file is empty.")
    if len(data) > MAX_ACSM_BYTES:
        raise PipelineError("That doesn't look like an .acsm file (too big). Upload the small .acsm file from Libby, not the book.")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise PipelineError("That doesn't look like an .acsm file (not XML).") from None
    if not root.tag.endswith("fulfillmentToken"):
        raise PipelineError("That doesn't look like an .acsm file (no fulfillment token).")


# Formats Send to Kindle accepts by email; these are sent as they are.
SEND_AS_IS = {".epub", ".pdf", ".doc", ".docx", ".txt", ".rtf", ".htm", ".html", ".png", ".gif", ".jpg", ".jpeg", ".bmp"}


def is_supported(filename: str) -> bool:
    suffix = Path(filename).suffix.lower()
    return suffix == ".acsm" or suffix in SEND_AS_IS


def process(cfg: Config, store: JobStore, job: Job, data: bytes) -> None:
    """Run one job start to finish, recording progress and errors on the job.

    An .acsm loan is downloaded and its DRM removed; any other supported file is sent as it is.
    """
    log.info("Processing %s (from %s)", job.filename, job.source)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{job.id}-", dir=cfg.work_dir))
    try:
        suffix = Path(job.filename).suffix.lower()
        if suffix == ".acsm":
            validate_acsm(data)
            store.update(job, status="fulfilling")
            encrypted = fulfil(cfg, workdir, data)
            store.update(job, status="removing_drm")
            book = remove_drm(cfg, workdir, encrypted)
        elif suffix in SEND_AS_IS:
            book = workdir / "out" / f"book{suffix}"
            book.parent.mkdir()
            book.write_bytes(data)
            if suffix == ".epub" and is_drm_protected(book):
                raise PipelineError("This EPUB has Adobe DRM. Upload the loan's .acsm file instead.")
        else:
            raise PipelineError(f"Can't send {suffix or 'files without an extension'} to a Kindle. Use an .acsm, EPUB or PDF.")

        title, author = read_metadata(book)
        if title is None and suffix != ".acsm":
            title = Path(job.filename).stem
        book = book.rename(book.parent / nice_filename(title, author, book.suffix))
        store.update(job, title=title)

        if book.stat().st_size > cfg.max_attachment_bytes:
            raise PipelineError(f"The book is {book.stat().st_size // (1024 * 1024)} MB, too big to email to a Kindle.")

        store.update(job, status="sending")
        try:
            mailer.send_to_kindle(cfg, book)
        except Exception as exc:  # smtplib raises a zoo of exception types
            raise PipelineError(f"Couldn't email the book: {exc}") from exc

        store.update(job, status="done")
        log.info("Sent to Kindle: %s (%s, from %s)", title or book.name, job.filename, job.source)
    except PipelineError as exc:
        store.update(job, status="failed", error=str(exc))
        log.warning("Not sent: %s (from %s): %s", job.filename, job.source, exc)
    except Exception as exc:
        log.exception("job %s failed", job.id)
        store.update(job, status="failed", error=f"Unexpected error: {exc}")
    finally:
        if not cfg.keep_files:
            shutil.rmtree(workdir, ignore_errors=True)
    notify(cfg, job)


def fail(cfg: Config, store: JobStore, job: Job, error: str) -> None:
    """Mark a job failed before the pipeline could start, and tell the user."""
    store.update(job, status="failed", error=error)
    log.warning("Not sent: %s (from %s): %s", job.filename, job.source, error)
    notify(cfg, job)


def notify(cfg: Config, job: Job) -> None:
    """Email the pass/fail result. A failed notice is logged, never raised."""
    if not cfg.notify_email:
        return
    name = job.title or job.filename
    via = {"drive": "Google Drive", "libby": "Libby"}.get(job.source, "the web page")
    if job.status == "done":
        subject = f"✅ Sent to Kindle: {name}"
        body = f"“{name}” was sent to {cfg.kindle_email}. It should appear on your Kindle in a few minutes.\n\nFile: {job.filename} (from {via})"
    else:
        subject = f"❌ Not sent to Kindle: {name}"
        body = f"“{job.filename}” (from {via}) could not be sent to your Kindle.\n\n{job.error}"
    try:
        mailer.send_notification(cfg, subject, body)
        log.info("Result email sent to %s", cfg.notify_email)
    except Exception:
        log.exception("couldn't send notification for job %s", job.id)


def _run(cfg: Config, args: list[str], step: str) -> None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=cfg.tool_timeout)
    except FileNotFoundError:
        raise PipelineError(f"{step}: {args[0]} is not installed.") from None
    except subprocess.TimeoutExpired:
        raise PipelineError(f"{step}: timed out after {cfg.tool_timeout}s (are Adobe's servers reachable?).") from None
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip().splitlines()
        detail = " ".join(output[-3:]) if output else f"exit code {result.returncode}"
        log.warning("%s failed: %s", step, result.stderr or result.stdout)
        raise PipelineError(f"{step} failed: {detail}")


def fulfil(cfg: Config, workdir: Path, acsm: bytes) -> Path:
    """Exchange the .acsm ticket for the (still encrypted) book."""
    acsm_path = workdir / "loan.acsm"
    acsm_path.write_bytes(acsm)
    fulfilled = workdir / "fulfilled"
    fulfilled.mkdir()
    try:
        _run(
            cfg,
            [cfg.acsmdownloader, "--adept-directory", str(cfg.adept_dir), "--output-dir", str(fulfilled), str(acsm_path)],
            "Downloading the book",
        )
    except PipelineError as exc:
        raise PipelineError(
            f"{str(exc).rstrip('.')}. If you've already used this .acsm or it's more than a few days old, "
            "download it again from Libby (Loans → Read With… → EPUB)."
        ) from None
    books = [p for p in fulfilled.iterdir() if p.suffix.lower() in {".epub", ".pdf"}]
    if not books:
        raise PipelineError("Downloading the book produced no EPUB or PDF.")
    return books[0]


def is_drm_protected(book: Path) -> bool:
    if book.suffix.lower() != ".epub":
        return True  # adept_remove handles PDFs and leaves unprotected ones alone
    with zipfile.ZipFile(book) as zf:
        return "META-INF/rights.xml" in zf.namelist()


def remove_drm(cfg: Config, workdir: Path, encrypted: Path) -> Path:
    out_dir = workdir / "out"
    out_dir.mkdir()
    output = out_dir / f"book{encrypted.suffix.lower()}"
    if not is_drm_protected(encrypted):
        shutil.copyfile(encrypted, output)
        return output
    _run(
        cfg,
        # adept_remove refuses --output-dir together with --output-file, so give it the full path.
        [cfg.adept_remove, "--adept-directory", str(cfg.adept_dir), "--output-file", str(output), str(encrypted)],
        "Removing DRM",
    )
    if not output.exists():
        raise PipelineError("Removing DRM produced no file.")
    return output


def read_metadata(book: Path) -> tuple[str | None, str | None]:
    """Title and first author from an EPUB's OPF file. PDFs and broken EPUBs give (None, None)."""
    if book.suffix.lower() != ".epub":
        return None, None
    ns = {
        "c": "urn:oasis:names:tc:opendocument:xmlns:container",
        "opf": "http://www.idpf.org/2007/opf",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    try:
        with zipfile.ZipFile(book) as zf:
            container = ElementTree.fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(".//c:rootfile", ns)
            opf = ElementTree.fromstring(zf.read(rootfile.get("full-path")))
    except (KeyError, AttributeError, zipfile.BadZipFile, ElementTree.ParseError):
        return None, None
    title = opf.findtext(".//dc:title", default=None, namespaces=ns)
    author = opf.findtext(".//dc:creator", default=None, namespaces=ns)
    return (title.strip() if title else None), (author.strip() if author else None)


def nice_filename(title: str | None, author: str | None, suffix: str) -> str:
    name = " - ".join(part for part in (title, author) if part) or "book"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")[:120] or "book"
    return f"{name}{suffix.lower()}"
