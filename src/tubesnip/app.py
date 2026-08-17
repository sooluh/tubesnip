"""TubeSnip FastAPI app.

Run with: uv run uvicorn tubesnip.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import queue
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, logconf, ytdlp_service

# One log format for every logger (including the worker started below).
logconf.setup_logging()

jobs.ensure_dirs()
jobs.load()
jobs.start_worker()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # uvicorn overrides its own logger config on startup — re-apply the
    # uniform format afterwards.
    logconf.setup_logging()
    yield


app = FastAPI(title="TubeSnip", version="0.2.0", lifespan=lifespan)


class InfoRequest(BaseModel):
    url: str


class CutRequest(BaseModel):
    url: str
    start_ms: int
    end_ms: int
    resolution: str = "best"
    mode: str = "fast"
    format: str = "mp4"


@app.get("/api/health")
def api_health():
    """Liveness/readiness for container orchestration. `redis` is false in
    single-node mode or when the configured Redis is unreachable (the app then
    runs in-memory — healthy, just not multi-node)."""
    return {"ok": True, "redis": jobs._r() is not None}


@app.post("/api/info")
def api_info(req: InfoRequest):
    """Video info: duration, title, available resolutions, and whether it has audio."""
    try:
        return ytdlp_service.get_video_info(req.url)
    except ytdlp_service.YtError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/cut")
def api_cut(req: CutRequest):
    """Create a cut job; processed by the worker (one at a time)."""
    if req.mode not in ("fast", "accurate"):
        raise HTTPException(400, "mode must be 'fast' or 'accurate'.")
    if req.format not in ("mp4", "mov", "webm"):
        raise HTTPException(400, "format must be 'mp4', 'mov', or 'webm'.")
    if req.start_ms < 0 or req.end_ms <= req.start_ms:
        raise HTTPException(400, "start_ms must be >= 0 and end_ms > start_ms.")
    if not ytdlp_service.extract_video_id(req.url):
        raise HTTPException(400, "Invalid YouTube URL.")
    if req.resolution != "best":
        try:
            h = int(req.resolution)
        except ValueError:
            raise HTTPException(400, "resolution must be a number or 'best'.")
        if not 144 <= h <= 4320:
            raise HTTPException(400, "resolution out of range 144-4320.")

    job_id = jobs.create_job(
        {
            "url": req.url,
            "start_ms": req.start_ms,
            "end_ms": req.end_ms,
            "resolution": req.resolution,
            "mode": req.mode,
            "format": req.format,
        }
    )
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    """Job status (polled by the frontend ~1s)."""
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str):
    """Server-Sent Events: stream job status until done/error (replaces long polling).

    Frontend uses EventSource; backend pushes whenever update_job is called
    (worker thread). A `: ping` heartbeat every 15s keeps the connection alive.
    """
    if not jobs.get_job(job_id):
        raise HTTPException(404, "Job not found.")

    q = jobs.subscribe(job_id)

    async def gen():
        try:
            # Initial snapshot (current status) before any further updates.
            # If already done/error, send it and close — don't wait for updates.
            first = jobs.get_job(job_id)
            if first:
                yield _sse_event(first)
                if first.get("status") in ("done", "error"):
                    return
            while True:
                try:
                    # put_nowait from the worker thread → wait here (async).
                    job = await asyncio.to_thread(q.get, timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"  # heartbeat: keep the connection alive
                    continue
                yield _sse_event(job)
                if job.get("status") in ("done", "error"):
                    return
        finally:
            jobs.unsubscribe(job_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse_event(job: dict) -> str:
    return f"data: {json.dumps(job, ensure_ascii=False)}\n\n"


@app.get("/api/download/{job_id}")
def api_download(job_id: str):
    """Download the cut result (the file is later cleaned up by TTL)."""
    job = jobs.get_job(job_id)
    if not job or job.get("status") != "done" or not job.get("file"):
        raise HTTPException(404, "Result not ready.")
    path = jobs.job_dir(job_id) / job["file"]
    if not path.exists():
        raise HTTPException(404, "Result file not found.")

    suffix = path.suffix.lower()
    media_type = {
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }.get(suffix, "video/mp4")
    return FileResponse(path, media_type=media_type, filename=_download_name(job))


def _download_name(job: dict) -> str:
    title = re.sub(r'[\\/:*?"<>|]+', "", job.get("title") or "video")
    title = (title.strip() or "video")[:80]
    res = job.get("resolution")
    res_label = f"{res}p" if res not in ("best", None) else "best"
    st = job.get("start_ms")
    en = job.get("end_ms")
    if st is not None and en is not None:
        seg = f"{_clock(st)}-{_clock(en)}"
    else:
        seg = "clip"
    suffix = Path(job["file"]).suffix
    return f"{title} [{seg}] {res_label}{suffix}"


def _clock(ms: int) -> str:
    t = max(0, ms)
    h, rem = divmod(t, 3600000)
    m, s = divmod(rem, 60000)
    sec, milli = divmod(s, 1000)
    return f"{h:02d}-{m:02d}-{sec:02d}.{milli:03d}"


# Static frontend (index.html + app.js + style.css) — mounted last so the
# /api/* routes still win.
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
