"""Web app: the upload page, the settings page, and a small JSON API behind a shared token."""

import hmac
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import logs, mailer, pipeline, settings
from .config import Config
from .drive import DriveWatcher
from .jobs import JobStore

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
HOUSEKEEPING_SECONDS = 3600

cfg = Config.load()
logs.setup(cfg.log_dir, cfg.retention_days)
store = JobStore(cfg.history_file, cfg.retention_days)
watcher: DriveWatcher | None = None
_watcher_stop: threading.Event | None = None
_apply_lock = threading.Lock()


def apply_config(new: Config) -> None:
    """Switch to new settings and restart the Drive watcher to match."""
    global cfg, watcher, _watcher_stop
    with _apply_lock:
        if _watcher_stop:
            _watcher_stop.set()
        cfg = new
        logs.set_retention(cfg.retention_days)
        store.retention_days = cfg.retention_days
        watcher = DriveWatcher(cfg, store) if cfg.drive_enabled else None
        _watcher_stop = watcher.start() if watcher and cfg.google_credentials.exists() else None


def housekeeping(stop: threading.Event) -> None:
    """Hourly: delete logs and history entries older than the retention period."""
    while not stop.is_set():
        try:
            logs.prune(cfg.log_dir, cfg.retention_days)
            store.prune()
        except Exception:
            log.exception("Housekeeping failed")
        stop.wait(HOUSEKEEPING_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("libtokindle starting")
    apply_config(cfg)
    stop_housekeeping = threading.Event()
    threading.Thread(target=housekeeping, args=(stop_housekeeping,), name="housekeeping", daemon=True).start()
    yield
    stop_housekeeping.set()
    if _watcher_stop:
        _watcher_stop.set()


app = FastAPI(title="libtokindle", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def require_token(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not cfg.upload_token or not hmac.compare_digest(token.encode(), cfg.upload_token.encode()):
        raise HTTPException(status_code=401, detail="Wrong or missing access token.")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/settings", include_in_schema=False)
def settings_page() -> FileResponse:
    return FileResponse(STATIC / "settings.html")


@app.get("/logs", include_in_schema=False)
def logs_page() -> FileResponse:
    return FileResponse(STATIC / "logs.html")


@app.get("/healthz")
def healthz() -> dict:
    problems = cfg.problems()
    return {"ok": not problems, "problems": problems}


def drive_status() -> dict:
    if not cfg.drive_enabled:
        return {"enabled": False}
    if not watcher or not _watcher_stop:
        return {"enabled": True, "last_check": None, "last_error": "The Google key hasn't been uploaded (see Settings)."}
    return watcher.status()


@app.get("/api/status", dependencies=[Depends(require_token)])
def status() -> dict:
    return {
        "kindle_email": cfg.kindle_email,
        "problems": cfg.problems(),
        "drive": drive_status(),
        "recent": [job.to_dict() for job in store.recent()],
    }


@app.post("/api/upload", dependencies=[Depends(require_token)])
async def upload(file: UploadFile, background: BackgroundTasks) -> dict:
    data = await file.read(pipeline.MAX_ACSM_BYTES + 1)
    try:
        pipeline.validate_acsm(data)
    except pipeline.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    job = store.create(file.filename or "loan.acsm")
    log.info("Upload received: %s", job.filename)
    background.add_task(pipeline.process, cfg, store, job, data)
    return job.to_dict()


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
def get_job(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return job.to_dict()


# --- First run: choose the access token (only while none is set)

@app.get("/api/setup")
def setup_state() -> dict:
    return {"needs_setup": not cfg.upload_token}


@app.post("/api/setup")
def setup(token: str = Body(embed=True)) -> dict:
    if cfg.upload_token:
        raise HTTPException(status_code=409, detail="An access token is already set.")
    return _save_settings({"UPLOAD_TOKEN": token})


# --- Settings

def _save_settings(changes: dict) -> dict:
    try:
        clean = settings.normalise(changes)
    except settings.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    settings.save(cfg, clean)
    log.info("Settings changed: %s", ", ".join(sorted(clean)) or "nothing")
    apply_config(Config.load())
    return settings.current(cfg)


@app.get("/api/settings", dependencies=[Depends(require_token)])
def get_settings() -> dict:
    return settings.current(cfg)


@app.put("/api/settings", dependencies=[Depends(require_token)])
def put_settings(changes: dict = Body()) -> dict:
    return _save_settings(changes)


@app.post("/api/settings/google-key", dependencies=[Depends(require_token)])
async def upload_google_key(file: UploadFile) -> dict:
    try:
        settings.save_google_key(cfg, await file.read(64 * 1024))
    except settings.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    log.info("Google service account key uploaded")
    apply_config(Config.load())
    return settings.current(cfg)


@app.post("/api/settings/activate", dependencies=[Depends(require_token)])
def activate(adobe_id: str = Body("", embed=True), password: str = Body("", embed=True)) -> dict:
    try:
        settings.activate_adobe(cfg, adobe_id.strip(), password)
    except settings.SettingsError as exc:
        log.warning("Adobe activation failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from None
    log.info("Adobe device activated (%s)", "Adobe ID" if adobe_id.strip() else "anonymous")
    return settings.current(cfg)


@app.post("/api/settings/test-email", dependencies=[Depends(require_token)])
def test_email() -> dict:
    if not cfg.notify_email:
        raise HTTPException(status_code=400, detail="Result emails are turned off, so there's nowhere to send a test.")
    try:
        mailer.send_notification(
            cfg,
            "libtokindle test email",
            "Your email settings work. Result emails for each book will arrive here.",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't send: {exc}") from None
    return {"sent_to": cfg.notify_email}


# --- Logs and history

@app.get("/api/logs", dependencies=[Depends(require_token)])
def get_logs(file: str | None = None, problems_only: bool = False) -> dict:
    files = logs.list_files(cfg.log_dir) if cfg.log_dir.exists() else []
    name = file or (files[0]["name"] if files else None)
    lines: list[str] = []
    if name:
        try:
            lines = logs.read(cfg.log_dir, name, problems_only=problems_only)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No such log file.") from None
    return {"retention_days": cfg.retention_days, "files": files, "file": name, "lines": lines}


@app.get("/api/history", dependencies=[Depends(require_token)])
def get_history() -> dict:
    return {"retention_days": cfg.retention_days, "jobs": [job.to_dict() for job in store.recent(limit=1000)]}
