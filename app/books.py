"""Converted books kept on the server for the Downloads page, deleted after the retention period.

Download links are signed and short-lived, so a plain link works in Safari (which can't
send the access token as a header) without making the files public.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

EXTENSIONS = {".epub", ".pdf", ".doc", ".docx", ".txt", ".rtf", ".htm", ".html", ".png", ".gif", ".jpg", ".jpeg", ".bmp"}
LINK_SECONDS = 10 * 60


def save(books_dir: Path, book: Path) -> str:
    """Copy a finished book into the library; the same book again replaces the old copy."""
    books_dir.mkdir(parents=True, exist_ok=True)
    target = books_dir / book.name
    tmp = books_dir / f".{book.name}.tmp"
    shutil.copyfile(book, tmp)
    tmp.replace(target)
    return target.name


def path_for(books_dir: Path, name: str) -> Path:
    """The file for a name from the page. Only plain names of existing books are accepted."""
    if not name or name != Path(name).name or name.startswith(".") or Path(name).suffix.lower() not in EXTENSIONS:
        raise FileNotFoundError(name)
    path = books_dir / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def list_books(books_dir: Path, retention_days: int) -> list[dict]:
    """Newest first."""
    if not books_dir.is_dir():
        return []
    books = []
    for path in books_dir.iterdir():
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in EXTENSIONS:
            stat = path.stat()
            books.append({
                "name": path.name,
                "size": stat.st_size,
                "saved": stat.st_mtime,
                "deleted_after": stat.st_mtime + retention_days * 86400,
            })
    return sorted(books, key=lambda b: b["saved"], reverse=True)


def delete(books_dir: Path, name: str) -> None:
    path_for(books_dir, name).unlink()


def prune(books_dir: Path, retention_days: int, now: float | None = None) -> list[str]:
    """Delete books older than the retention period. Returns the deleted names."""
    if not books_dir.is_dir():
        return []
    cutoff = (now or time.time()) - retention_days * 86400
    deleted = []
    for path in books_dir.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path.name)
    if deleted:
        log.info("Deleted %d book(s) older than %d days", len(deleted), retention_days)
    return deleted


# --- Signed download links

def _signature(secret: str, name: str, expires: int) -> str:
    return hmac.new(secret.encode(), f"{name}\n{expires}".encode(), hashlib.sha256).hexdigest()


def sign(secret: str, name: str, now: float | None = None) -> tuple[int, str]:
    expires = int((now or time.time()) + LINK_SECONDS)
    return expires, _signature(secret, name, expires)


def verify(secret: str, name: str, expires: int, signature: str, now: float | None = None) -> bool:
    if not secret or expires < (now or time.time()):
        return False
    return hmac.compare_digest(_signature(secret, name, expires), signature or "")
