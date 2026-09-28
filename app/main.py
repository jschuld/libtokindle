"""Web app: the upload page, the settings page, and a small JSON API behind a shared token."""

import hmac
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import mailer, pipeline, settings
from .config import Config
from .drive import DriveWatcher
from .jobs import JobStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).parent / "static"

cfg = Config.load()
store = JobStore()
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
        watcher = DriveWatcher(cfg, store) if cfg.drive_enabled else None
        _watcher_stop = watcher.start() if watcher and cfg.google_credentials.exists() else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    apply_config(cfg)
    yield
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
    apply_config(Config.load())
    return settings.current(cfg)


@app.post("/api/settings/activate", dependencies=[Depends(require_token)])
def activate(adobe_id: str = Body("", embed=True), password: str = Body("", embed=True)) -> dict:
    try:
        settings.activate_adobe(cfg, adobe_id.strip(), password)
    except settings.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
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
