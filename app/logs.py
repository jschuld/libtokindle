"""Log files in /config/logs: one file per day, deleted after the retention period."""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
BASENAME = "libtokindle.log"
# Rotated files are named libtokindle.log.YYYY-MM-DD.
ROTATED = re.compile(r"^libtokindle\.log\.(\d{4}-\d{2}-\d{2})$")
LEVEL = re.compile(r"^\S+ \S+ (WARNING|ERROR|CRITICAL) ")

_file_handler: TimedRotatingFileHandler | None = None


def setup(log_dir: Path, retention_days: int) -> None:
    """Log to the console (for `docker compose logs`) and to a daily file."""
    global _file_handler
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(console)

    if _file_handler:
        root.removeHandler(_file_handler)
        logging.getLogger("uvicorn").removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            log_dir / BASENAME, when="midnight", backupCount=retention_days, encoding="utf-8", delay=True
        )
    except OSError as exc:
        log.warning("Not writing a log file to %s: %s", log_dir, exc)
        return
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)
    _file_handler = handler
    # uvicorn's loggers don't pass messages up to the root logger, so attach to them
    # directly. Server errors go to the file; per-request access lines ("uvicorn.access")
    # stay on the console only.
    logging.getLogger("uvicorn").addHandler(handler)


def set_retention(retention_days: int) -> None:
    if _file_handler:
        _file_handler.backupCount = retention_days


def _file_date(path: Path) -> dt.date | None:
    match = ROTATED.match(path.name)
    if match:
        return dt.date.fromisoformat(match.group(1))
    if path.name == BASENAME:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).date() if path.exists() else dt.date.today()
    return None


def prune(log_dir: Path, retention_days: int, today: dt.date | None = None) -> list[str]:
    """Delete log files older than the retention period. Returns the deleted names."""
    cutoff = (today or dt.date.today()) - dt.timedelta(days=retention_days)
    deleted = []
    for path in log_dir.glob(BASENAME + ".*"):
        date = _file_date(path)
        if date and date < cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path.name)
    if deleted:
        log.info("Deleted %d log file(s) older than %d days", len(deleted), retention_days)
    return deleted


def list_files(log_dir: Path) -> list[dict]:
    """Newest first: [{"name": ..., "date": "YYYY-MM-DD", "size": bytes}]."""
    files = []
    for path in log_dir.glob(BASENAME + "*"):
        date = _file_date(path)
        if date:
            files.append({"name": path.name, "date": date.isoformat(), "size": path.stat().st_size})
    return sorted(files, key=lambda f: (f["date"], f["name"] == BASENAME), reverse=True)


def read(log_dir: Path, name: str, max_lines: int = 2000, problems_only: bool = False) -> list[str]:
    """The last lines of one log file. Only names from list_files() are accepted."""
    if name != BASENAME and not ROTATED.match(name):
        raise FileNotFoundError(name)
    path = log_dir / name
    lines: deque[str] = deque(maxlen=max_lines)
    keep = False
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if problems_only:
                # Keep warning/error lines and the traceback lines that follow them.
                if re.match(r"^\d{4}-\d{2}-\d{2} ", line):
                    keep = bool(LEVEL.match(line))
                if not keep:
                    continue
            lines.append(line)
    return list(lines)
