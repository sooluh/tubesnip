"""Job store + cutting worker (single-threaded queue).

One job is processed at a time; others wait in the queue. Job status is saved
to data/jobs.json so it survives server restarts. Result files live in
data/jobs/<id>/ and are cleaned up after TTL (TUBESNIP_JOB_TTL_H, default 24h).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import logconf, ytdlp_service

logger = logging.getLogger("tubesnip.jobs")

# Uniform format (with root/uvicorn) — see logconf.setup_logging().
logconf.setup_logging()


def _fmt_ms(ms: int | None) -> str:
    """Format milliseconds → HH:MM:SS.mmm (for server logs)."""
    if ms is None:
        return "-"
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms_ = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms_:03d}"

DATA_DIR = Path(os.environ.get("TUBESNIP_DATA_DIR", "data"))
JOBS_DIR = DATA_DIR / "jobs"
JOBS_FILE = DATA_DIR / "jobs.json"
TTL_SECONDS = float(os.environ.get("TUBESNIP_JOB_TTL_H", "24")) * 3600

_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()
_jobs: dict[str, dict] = {}
_worker_started = False

# SSE subscribers: job_id → list[queue.Queue]. update_job pushes the latest
# snapshot to all subscribers so /events can stream without polling.
_subscribers: dict[str, list[queue.Queue]] = {}


def subscribe(job_id: str) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=10)
    with _lock:
        _subscribers.setdefault(job_id, []).append(q)
    return q


def unsubscribe(job_id: str, q: queue.Queue) -> None:
    with _lock:
        subs = _subscribers.get(job_id)
        if subs:
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                _subscribers.pop(job_id, None)


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def load() -> None:
    global _jobs
    try:
        if JOBS_FILE.exists():
            _jobs = json.loads(JOBS_FILE.read_text())
    except Exception:
        _jobs = {}


def _save() -> None:
    JOBS_FILE.write_text(json.dumps(_jobs, ensure_ascii=False, indent=1))


def create_job(payload: dict) -> str:
    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id,
        "status": "queued",
        "stage": "queued",
        "percent": 0,
        "message": "Queued…",
        "error": None,
        "download_url": None,
        "file": None,
        "title": None,
        "actual_duration_ms": None,
        "snap_delta_ms": None,
        "created_at": time.time(),
        **payload,
    }
    with _lock:
        _jobs[job_id] = job
        _save()
    _queue.put(job_id)
    logger.info(
        "job %s created: url=%s start=%s end=%s resolution=%s mode=%s (queue=%d)",
        job_id, payload.get("url"), _fmt_ms(payload.get("start_ms")),
        _fmt_ms(payload.get("end_ms")), payload.get("resolution", "best"),
        payload.get("mode", "fast"), _queue.qsize(),
    )
    return job_id


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def update_job(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        _save()
        snapshot = dict(job)
        subs = list(_subscribers.get(job_id, ()))
    # Notify subscribers outside the lock (queues are thread-safe).
    for q in subs:
        try:
            q.put_nowait(snapshot)
        except queue.Full:
            pass


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _cleanup() -> None:
    """Remove jobs & temp dirs past their TTL."""
    now = time.time()
    with _lock:
        expired = [
            jid for jid, j in _jobs.items()
            if now - j.get("created_at", 0) > TTL_SECONDS
        ]
        for jid in expired:
            _jobs.pop(jid, None)
        _save()
    for jid in expired:
        shutil.rmtree(job_dir(jid), ignore_errors=True)


def start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, name="cut-worker", daemon=True).start()


def _worker_loop() -> None:
    while True:
        job_id = _queue.get()
        logger.debug("worker picked up job %s from queue", job_id)
        try:
            _process(job_id)
        except ytdlp_service.YtError as e:
            logger.error("job %s failed: %s", job_id, e)
            update_job(
                job_id, status="error", stage="error",
                percent=None, error=str(e), message="Failed",
            )
        except Exception as e:  # last-resort safety net
            logger.exception("job %s failed (internal error)", job_id)
            update_job(
                job_id, status="error", stage="error",
                percent=None, error=f"Internal error: {e}", message="Failed",
            )
        finally:
            _cleanup()


def _process(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    update_job(job_id, status="running", message="Fetching video info…")
    logger.info(
        "job %s started: url=%s start=%s end=%s resolution=%s mode=%s",
        job_id, job["url"], _fmt_ms(job["start_ms"]), _fmt_ms(job["end_ms"]),
        job["resolution"], job["mode"],
    )

    # Real-time log: new line when the stage changes or the integer percent
    # ticks up (throttled to 1% — ffmpeg can emit dozens of updates per second).
    log_state = {"stage": None, "pct": None}

    def progress(stage: str, pct: float | None) -> None:
        fields: dict = {"stage": stage}
        # DEBUG: every raw callback (unthrottled) for troubleshooting.
        logger.debug("job %s progress raw stage=%s pct=%s", job_id, stage, pct)
        if stage == "download":
            display = 5 + (pct or 0) * 0.75
            fields["percent"] = display
            fields["message"] = "Downloading & cutting…"
        elif stage == "encode":
            display = 80 + (pct or 0) * 0.15
            fields["percent"] = display
            fields["message"] = "Re-encoding (precise mode)…"
        elif stage == "extract":
            display = 5.0
            fields["percent"] = display
            fields["message"] = "Fetching video info…"
        else:
            display = pct or 0.0
        update_job(job_id, **fields)
        int_pct = int(display)
        if stage != log_state["stage"]:
            logger.info(
                "job %s stage=%s %.1f%% — %s", job_id, stage,
                display, fields.get("message", ""),
            )
        elif int_pct != log_state["pct"]:
            logger.info("job %s %s %d%%", job_id, stage, int_pct)
        log_state["stage"] = stage
        log_state["pct"] = int_pct

    try:
        out_file, info = ytdlp_service.cut_section(
            url=job["url"],
            start_ms=job["start_ms"],
            end_ms=job["end_ms"],
            height=job["resolution"],
            mode=job["mode"],
            out_dir=job_dir(job_id),
            progress_cb=progress,
        )
    except subprocess.TimeoutExpired:
        raise ytdlp_service.YtError("Process took too long — aborted.")

    # Verify result: video stream present, audio not lost, A/V in sync.
    update_job(job_id, stage="verify", percent=97, message="Verifying result…")
    logger.info("job %s stage=verify — verifying result (ffprobe)", job_id)
    p = ytdlp_service.probe(str(out_file))
    src_audio = bool(info.get("has_audio", True))
    if not p["has_video"]:
        raise ytdlp_service.YtError("Result contains no video stream.")
    if src_audio and not p["has_audio"]:
        raise ytdlp_service.YtError(
            "Audio lost during cutting — result discarded."
        )
    if src_audio and p["video_start"] is not None and p["audio_start"] is not None:
        diff = abs(p["video_start"] - p["audio_start"])
        limit = 0.5 if job.get("mode") == "accurate" else 3.0
        if diff > limit:
            raise ytdlp_service.YtError(
                f"Audio and video out of sync ({diff:.2f}s apart) — result discarded."
            )

    # Result duration must be sane vs. the request (guards wrong cuts).
    requested_ms = job["end_ms"] - job["start_ms"]
    actual_duration_ms = round(p["duration"] * 1000)
    snap_delta_ms = abs(actual_duration_ms - requested_ms)
    # Fast mode is already frame-accurate at start (accurate_seek) — the rest
    # is just container rounding at the end (±0.2s). A 2s limit still catches
    # wrong cuts.
    duration_limit = 0.5 if job.get("mode") == "accurate" else 2.0
    if snap_delta_ms / 1000 > duration_limit:
        raise ytdlp_service.YtError(
            f"Result duration ({actual_duration_ms / 1000:.2f}s) deviates too far "
            f"from request ({requested_ms / 1000:.2f}s) — result discarded."
        )

    update_job(
        job_id,
        status="done",
        stage="done",
        percent=100,
        error=None,
        message="Done",
        download_url=f"/api/download/{job_id}",
        file=out_file.name,
        title=info.get("title"),
        actual_duration_ms=actual_duration_ms,
        snap_delta_ms=snap_delta_ms,
    )
    logger.info(
        "job %s done: duration=%.2fs (requested %.2fs, snap %.2fs) file=%s",
        job_id, actual_duration_ms / 1000, requested_ms / 1000,
        snap_delta_ms / 1000, out_file.name,
    )
