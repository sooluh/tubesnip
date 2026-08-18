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
# Concurrent cut jobs. Kept small on purpose: each job already downloads video
# + audio in parallel, so N jobs ≈ 2N googlevideo streams. A low cap keeps the
# box (and the YouTube connection) from being overwhelmed. Set 1 for serial.
WORKER_COUNT = int(os.environ.get("TUBESNIP_CONCURRENCY", "2"))
# Optional multi-node mode: when TUBESNIP_REDIS_URL is set, the job store,
# queue, lease and SSE fan-out live in Redis so many containers share them.
# Empty → the in-memory + jobs.json single-node mode.
REDIS_URL = os.environ.get("TUBESNIP_REDIS_URL", "")
# Optional shared result storage (mount NFS/EFS/S3 here across all nodes). When
# set, per-job dirs live under <SHARED_DIR>/jobs so any node can serve a cut.
SHARED_DIR: Path | None = Path(os.environ["TUBESNIP_SHARED_DIR"]) if os.environ.get("TUBESNIP_SHARED_DIR") else None
_REDIS_LEASE_S = 120  # worker lease: a job whose lease expires is re-queued
_REDIS_SWEEP_S = 30.0  # dead-worker / TTL sweeper interval
_REDIS_SWEEP_LOCK_S = 25

import redis as _redis_lib  # type: ignore[no-redef]

_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()
_jobs: dict[str, dict] = {}
_worker_started = False

# SSE subscribers: job_id → list[queue.Queue]. update_job pushes the latest
# snapshot to all subscribers so /events can stream without polling.
_subscribers: dict[str, list[queue.Queue]] = {}

# Redis client state: None = not connected yet, otherwise active. A failed
# first connection marks `_redis_failed` and the app runs single-node (memory).
_redis: "_redis_lib.Redis | None" = None
_redis_failed = False
_subscriptions_started = False


def _r() -> "_redis_lib.Redis | None":
    """Active Redis client when TUBESNIP_REDIS_URL is configured, else None.

    Connects lazily on first use; a failed connection falls back to single-node
    memory mode with a loud log (no retry loop — restart after fixing Redis).
    """
    global _redis, _redis_failed
    if not REDIS_URL or _redis_failed:
        return None
    if _redis is None:
        try:
            c = _redis_lib.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=None,      # BLPOP(5s) needs blocking reads
                retry_on_timeout=True,
                health_check_interval=30,
            )
            c.ping()
            _redis = c
            _start_redis_listener()
        except Exception:
            logger.exception("Redis unavailable — running single-node (jobs are NOT shared)")
            _redis_failed = True
    return _redis


def _start_redis_listener() -> None:
    """One per node: forwards tubesnip:events:* pubsub to local SSE subscribers,
    so an update made by a worker on another node still reaches this node's
    EventSource streams."""
    global _subscriptions_started
    if _subscriptions_started:
        return
    _subscriptions_started = True
    threading.Thread(target=_redis_event_loop, name="redis-events", daemon=True).start()


def _redis_event_loop() -> None:
    """Forward tubesnip:events:* pubsub to local SSE subscribers. If the Redis
    connection drops, reconnect instead of dying (a dead listener would freeze
    progress on this node's EventSource streams)."""
    while True:
        try:
            r = _r()
            if r is None:
                time.sleep(2)
                continue
            ps = r.pubsub()
            ps.psubscribe("tubesnip:events:*")
            for msg in ps.listen():
                if msg.get("type") != "pmessage":
                    continue
                try:
                    job = json.loads(msg["data"])
                except Exception:
                    continue
                jid = job.get("id")
                if not jid:
                    continue
                with _lock:
                    subs = list(_subscribers.get(jid, ()))
                for q in subs:
                    try:
                        q.put_nowait(job)
                    except queue.Full:
                        pass
        except Exception:
            logger.exception("redis events listener dropped — reconnecting")
            time.sleep(2)


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
    if SHARED_DIR:
        (SHARED_DIR / "jobs").mkdir(parents=True, exist_ok=True)


def load() -> None:
    """Load persisted jobs and recover after a restart (single-node mode).

    Jobs that were mid-flight (`running`) are re-queued so they get re-cut
    instead of being lost or stuck forever — cutting is idempotent (same params
    → same result). `queued` jobs are simply put back on the queue. Redis mode
    is a no-op: the store is shared and the sweeper does the recovery.
    """
    global _jobs
    ytdlp_service.load_calibration()
    if REDIS_URL:
        return
    try:
        if JOBS_FILE.exists():
            _jobs = json.loads(JOBS_FILE.read_text())
    except Exception:
        _jobs = {}
    with _lock:
        requeued = 0
        for jid, job in _jobs.items():
            if not isinstance(job, dict):
                continue
            status = job.get("status")
            if status in ("queued", "running"):
                job["status"] = "queued"
                job["stage"] = "queued"
                job["percent"] = 0
                job["error"] = None
                job["message"] = "Re-queued after restart"
                job["download_url"] = None
                job["file"] = None
                _queue.put(jid)
                requeued += 1
        if requeued:
            _save()
    if requeued:
        logger.info("recovery: re-queued %d interrupted job(s) after restart", requeued)


def _save() -> None:
    """Persist jobs atomically: write to a temp file, then rename over the
    target. A crash mid-write leaves the previous valid file in place (no torn
    JSON), and the rename is atomic on POSIX. Callers hold `_lock`."""
    tmp = JOBS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_jobs, ensure_ascii=False, indent=1))
    os.replace(tmp, JOBS_FILE)


# A cut request's identity (exact-param match for job reuse). Normalized so a
# request without an explicit format matches an older job that predates it.
_JOB_PARAM_KEYS = ("url", "start_ms", "end_ms", "resolution", "mode", "format")


def _estimate_steps(payload: dict) -> dict:
    """Per-stage estimates (ms) for the frontend pipeline, from cut params +
    the effective stream's bitrate/fps/codec/height (sent as `hints` by the
    frontend, which already fetched them from /api/info). Encode speeds come
    from the server's real benchmark (encode_benchmark), so the estimate matches
    the actual machine."""
    dur_s = max(0, (payload.get("end_ms", 0) - payload.get("start_ms", 0))) / 1000
    hints = payload.get("hints") or {}
    bitrate = float(hints.get("bitrate_kbps") or 0)
    fps = float(hints.get("fps") or 30)
    codec = (hints.get("codec") or "h264").lower().replace(".", "")  # "H.264" -> "h264"
    height = float(hints.get("height") or 1080)
    factor = (height * (height * 16 / 9)) / (1080 * 1920)

    bench = ytdlp_service.estimate_params()
    x264_fps = bench["x264_fps"]
    vp9_fps = bench["vp9_fps"]
    throughput = bench["throughput_bps"]

    extract = bench.get("extract_ms") or 2000
    verify = bench.get("verify_ms") or 1000
    download = int((dur_s * bitrate * 1000 / 8) / throughput * 1000) if bitrate > 0 else 0
    encode = (
        int(dur_s * fps * factor / x264_fps * 1000)
        if payload.get("mode") == "accurate" else 0
    )

    fmt = payload.get("format") or "mp4"
    if fmt == "webm":
        convert = int(dur_s * fps * factor / vp9_fps * 1000)
    elif fmt == "mov":
        convert = 1000 if codec in ("h264", "hevc") else int(dur_s * fps * factor / x264_fps * 1000)
    else:
        convert = 0

    return {"extract": extract, "download": download, "encode": encode, "convert": convert, "verify": verify}


def _params_key(payload: dict) -> tuple:
    return (
        payload.get("url"),
        payload.get("start_ms"),
        payload.get("end_ms"),
        str(payload.get("resolution") or "best"),
        payload.get("mode") or "fast",
        payload.get("format") or "mp4",
    )


def _new_job(job_id: str, payload: dict) -> dict:
    return {
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
        "estimate_ms": _estimate_steps(payload),
        **payload,
    }


def _redis_find_existing(r, key: tuple) -> str | None:
    """Dedup scan over the shared store (all nodes see the same jobs)."""
    for jid, raw in r.hgetall("tubesnip:jobs").items():
        try:
            job = json.loads(raw)
        except Exception:
            continue
        if _params_key(job) != key:
            continue
        status = job.get("status")
        if status in ("queued", "running"):
            return jid
        if status == "done" and job.get("file"):
            result_file = job_dir(jid) / job["file"]
            if result_file.exists():
                return jid
    return None


def create_job(payload: dict) -> str:
    key = _params_key(payload)
    r = _r()
    if r is not None:
        job_id = _redis_find_existing(r, key)
        if job_id:
            logger.info("job %s reused (identical job, shared store)", job_id)
            return job_id
        job_id = uuid.uuid4().hex[:10]
        job = _new_job(job_id, payload)
        r.hset("tubesnip:jobs", job_id, json.dumps(job))
        r.sadd("tubesnip:ids", job_id)
        r.rpush("tubesnip:queue", job_id)
        logger.info(
            "job %s created (redis): url=%s start=%s end=%s resolution=%s mode=%s",
            job_id, payload.get("url"), _fmt_ms(payload.get("start_ms")),
            _fmt_ms(payload.get("end_ms")), payload.get("resolution", "best"),
            payload.get("mode", "fast"),
        )
        return job_id
    with _lock:
        # Reuse an existing job with identical params instead of re-cutting:
        # a queued/running job is followed; a finished one is served directly
        # as long as its result file still exists.
        for jid, job in _jobs.items():
            if _params_key(job) != key:
                continue
            status = job.get("status")
            if status in ("queued", "running"):
                logger.info("job %s reused (identical %s job)", jid, status)
                return jid
            if status == "done" and job.get("file"):
                result_file = job_dir(jid) / job["file"]
                if result_file.exists():
                    logger.info("job %s reused (identical completed job)", jid)
                    return jid
        job_id = uuid.uuid4().hex[:10]
        job = _new_job(job_id, payload)
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
    r = _r()
    if r is not None:
        try:
            raw = r.hget("tubesnip:jobs", job_id)
        except Exception:
            logger.exception("redis get failed — treating job as missing")
            return None
        return json.loads(raw) if raw else None
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _record_stage(fields: dict, job: dict) -> None:
    """Record the start time of each pipeline stage on transition.

    The frontend shows a per-step elapsed timer; that timer lives in the browser
    and is lost on reload. Persisting the stage start times server-side means a
    followed job (localStorage restore) and any node still get accurate per-step
    durations. `done` is recorded too, so total processing time =
    stage_started_at.done - created_at.
    """
    new_stage = fields.get("stage")
    if new_stage and new_stage != job.get("stage"):
        started = job.get("stage_started_at") or {}
        started.setdefault(new_stage, time.time())
        fields["stage_started_at"] = started


def update_job(job_id: str, **fields) -> float | None:
    r = _r()
    if r is not None:
        try:
            raw = r.hget("tubesnip:jobs", job_id)
            if raw is None:
                return None
            job = json.loads(raw)
            new_pct = fields.get("percent")
            cur_pct = job.get("percent")
            if isinstance(new_pct, (int, float)) and isinstance(cur_pct, (int, float)):
                fields["percent"] = max(new_pct, cur_pct)
            _record_stage(fields, job)
            job.update(fields)
            r.hset("tubesnip:jobs", job_id, json.dumps(job))
            # Heartbeat: keep the lease alive so the sweeper doesn't re-queue us.
            r.set(f"tubesnip:lease:{job_id}", "1", ex=_REDIS_LEASE_S)
            # Cross-node SSE: workers publish; every node's listener fans out.
            r.publish(f"tubesnip:events:{job_id}", json.dumps(job, ensure_ascii=False))
        except Exception:
            # Best-effort: a transient Redis error must not kill the cut or the
            # worker thread. Log and carry on — the final state is re-persisted
            # on the next call.
            logger.exception("redis update failed (best-effort, continuing)")
        return fields.get("percent")
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        # Monotonic percent: the video & audio streams download in parallel
        # and report out of order from multiple threads, so the bar must never
        # regress. Clamped here (under the lock) — a per-callback guard in the
        # worker would race between threads and still push lower values.
        new_pct = fields.get("percent")
        cur_pct = job.get("percent")
        if isinstance(new_pct, (int, float)) and isinstance(cur_pct, (int, float)):
            fields["percent"] = max(new_pct, cur_pct)
        _record_stage(fields, job)
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
    return fields.get("percent")


def job_dir(job_id: str) -> Path:
    base = SHARED_DIR / "jobs" if SHARED_DIR else JOBS_DIR
    return base / job_id


def _cleanup() -> None:
    """Remove jobs & temp dirs past their TTL (single-node mode).

    Redis mode: TTL + dead-worker recovery run in the shared sweeper."""
    if _r() is not None:
        return
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
    # Bounded pool (WORKER_COUNT, default 2): jobs run in parallel, but the
    # cap keeps CPU/bandwidth/YouTube-connections light. All workers drain the
    # same thread-safe queue. Redis mode also runs one shared-store sweeper
    # and a supervisor that respawns a worker if it ever dies.
    if _r() is not None:
        threading.Thread(target=_sweep_loop, name="cut-sweeper", daemon=True).start()
        threading.Thread(target=_supervisor_loop, name="cut-supervisor", daemon=True).start()
    for _ in range(max(1, WORKER_COUNT)):
        threading.Thread(target=_worker_loop, name="cut-worker", daemon=True).start()


def _supervise_once() -> None:
    """Respawn any cut-worker threads that died despite the guards (safety net
    — Redis-mode only; memory workers block on the queue and never die)."""
    alive = sum(1 for t in threading.enumerate() if t.name == "cut-worker")
    for _ in range(max(1, WORKER_COUNT) - alive):
        logger.warning("respawned a dead cut-worker thread")
        threading.Thread(target=_worker_loop, name="cut-worker", daemon=True).start()


def _supervisor_loop() -> None:
    while True:
        time.sleep(10)
        try:
            _supervise_once()
        except Exception:
            logger.exception("worker supervisor failed")


def _claim() -> str | None:
    """Block for the next job. Redis: shared list + a worker lease (the lease
    is the heartbeat that proves the worker is alive; the sweeper re-queues
    jobs whose lease expired — a dead node never leaves a job stuck)."""
    r = _r()
    if r is not None:
        item = r.blpop("tubesnip:queue", timeout=5)
        if item is None:
            return None
        job_id = item[1]
        r.set(f"tubesnip:lease:{job_id}", "1", ex=_REDIS_LEASE_S)
        return job_id
    return _queue.get()


def _release(job_id: str) -> None:
    r = _r()
    if r is not None:
        r.delete(f"tubesnip:lease:{job_id}")


def _worker_loop() -> None:
    while True:
        try:
            job_id = _claim()
        except Exception:
            # A transient Redis error (network blip / overloaded server) must
            # NOT kill the worker thread — log, back off, keep polling. Before
            # this guard, a timeout in BLPOP permanently killed every worker
            # and jobs silently stopped being processed.
            logger.exception("worker claim failed (redis hiccup) — retrying")
            time.sleep(1)
            continue
        if job_id is None:
            continue  # redis BLPOP timeout — loop and poll again
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
            try:
                _release(job_id)
                _cleanup()
            except Exception:
                logger.exception("worker cleanup failed (redis hiccup)")


def _sweep_loop() -> None:
    """Redis-mode sweeper: re-queue jobs whose worker lease expired (crashed
    node) and delete jobs/files past their TTL. Runs on every node, but the
    sweep lock makes only one node sweep at a time."""
    while True:
        time.sleep(_REDIS_SWEEP_S)
        try:
            _sweep_once()
        except Exception:
            logger.exception("redis sweeper failed")


def _sweep_once() -> None:
    r = _r()
    if r is None:
        return
    if not r.set("tubesnip:sweep_lock", "1", nx=True, ex=_REDIS_SWEEP_LOCK_S):
        return  # another node is already sweeping
    try:
        now = time.time()
        for jid, raw in r.hgetall("tubesnip:jobs").items():
            try:
                job = json.loads(raw)
            except Exception:
                continue
            if job.get("status") == "running" and not r.exists(f"tubesnip:lease:{jid}"):
                # Worker died mid-cut → re-queue (cutting is idempotent).
                job["status"] = "queued"
                job["stage"] = "queued"
                job["percent"] = 0
                job["message"] = "Re-queued after worker loss"
                r.hset("tubesnip:jobs", jid, json.dumps(job))
                r.rpush("tubesnip:queue", jid)
                logger.warning("job %s re-queued (lease expired)", jid)
            elif now - job.get("created_at", 0) > TTL_SECONDS:
                r.hdel("tubesnip:jobs", jid)
                r.srem("tubesnip:ids", jid)
                r.delete(f"tubesnip:lease:{jid}")
                shutil.rmtree(job_dir(jid), ignore_errors=True)
                logger.info("job %s expired (TTL) and removed", jid)
    finally:
        r.delete("tubesnip:sweep_lock")


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
    log_state: dict = {"stage": None, "pct": None}

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
        elif stage == "convert":
            display = 80 + (pct or 0) * 0.15
            fields["percent"] = display
            fields["message"] = "Converting format…"
        elif stage == "extract":
            display = 5.0
            fields["percent"] = display
            fields["message"] = "Fetching video info…"
        else:
            display = pct or 0.0
        # Percent monotonicity is enforced atomically inside update_job (the
        # parallel streams report out of order; a per-callback clamp would race).
        display = update_job(job_id, **fields) or 0.0
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

    requested_ms = job["end_ms"] - job["start_ms"]

    # Post-process the cut into the requested container (mp4 is the default,
    # produced by the cut pipeline already). mov = remux, webm = re-encode.
    fmt = job.get("format") or "mp4"
    if fmt != "mp4":
        update_job(
            job_id, stage="convert", percent=80,
            message=f"Converting to {fmt.upper()}…",
        )
        logger.info("job %s converting result to %s", job_id, fmt)
        out_file = ytdlp_service.convert_format(
            src=out_file,
            fmt=fmt,
            out_dir=job_dir(job_id),
            progress_cb=lambda pct: progress("convert", pct),
            duration_s=requested_ms / 1000,
        )

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
        file_size=out_file.stat().st_size if out_file.exists() else None,
        title=info.get("title"),
        actual_duration_ms=actual_duration_ms,
        snap_delta_ms=snap_delta_ms,
    )
    # Feed this job's real stage durations back into the estimate parameters
    # (internet throughput + encode fps) so future estimates match reality.
    try:
        _done = get_job(job_id)
        if _done:
            ytdlp_service.update_calibration(_done)
    except Exception:
        logger.exception("calibration update failed")
    logger.info(
        "job %s done: duration=%.2fs (requested %.2fs, snap %.2fs) file=%s",
        job_id, actual_duration_ms / 1000, requested_ms / 1000,
        snap_delta_ms / 1000, out_file.name,
    )
