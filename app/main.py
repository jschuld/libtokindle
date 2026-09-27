"""Web app: one upload page plus a small JSON API behind a shared token."""

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline
from .config import Config
from .drive import DriveWatcher
from .jobs import JobStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).parent / "static"

cfg = Config.from_env()
store = JobStore()
watcher = DriveWatcher(cfg, store) if cfg.drive_enabled else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = watcher.start() if watcher and cfg.google_credentials.exists() else None
    yield
    if stop:
        stop.set()


app = FastAPI(title="libtokindle", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def require_token(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not cfg.upload_token or not hmac.compare_digest(token.encode(), cfg.upload_token.encode()):
        raise HTTPException(status_code=401, detail="Wrong or missing access token.")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    problems = cfg.problems()
    return {"ok": not problems, "problems": problems}


@app.get("/api/status", dependencies=[Depends(require_token)])
def status() -> dict:
    return {
        "kindle_email": cfg.kindle_email,
        "problems": cfg.problems(),
        "drive": watcher.status() if watcher else {"enabled": False},
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
