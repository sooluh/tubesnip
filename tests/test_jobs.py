# Tests for jobs.py: job store, worker, TTL cleanup, and the _process path (all mocked).
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

import fakeredis
import pytest

from tubesnip import jobs
from tubesnip import ytdlp_service as ys


def _payload(**over):
    p = {"url": "https://youtu.be/abc123", "start_ms": 0, "end_ms": 5000, "resolution": "best", "mode": "fast"}
    p.update(over)
    return p


def _insert(job_id, **over):
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
        **_payload(),
        **over,
    }
    jobs._jobs[job_id] = job
    return job


def _probe_ok(**over):
    p = {
        "has_video": True,
        "has_audio": True,
        "duration": 5.0,
        "video_start": 0.0,
        "audio_start": 0.0,
    }
    p.update(over)
    return p


class TestStore:
    def test_ensure_dirs(self):
        jobs.ensure_dirs()
        assert jobs.JOBS_DIR.is_dir()

    def test_load_without_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "none.json")
        jobs._jobs = {"old": 1}
        jobs.load()
        # No file → old state is kept (not reset).
        assert jobs._jobs == {"old": 1}

    def test_load_valid_file(self, monkeypatch, tmp_path):
        f = tmp_path / "jobs.json"
        f.write_text(json.dumps({"a": 1}))
        monkeypatch.setattr(jobs, "JOBS_FILE", f)
        jobs.load()
        assert jobs._jobs == {"a": 1}

    def test_load_corrupt_file(self, monkeypatch, tmp_path):
        f = tmp_path / "jobs.json"
        f.write_text("{broken!!")
        monkeypatch.setattr(jobs, "JOBS_FILE", f)
        jobs.load()
        assert jobs._jobs == {}

    def test_load_requeues_interrupted_jobs(self, monkeypatch, tmp_path):
        """A running job at restart is re-queued (re-cut) — never lost/stuck."""
        f = tmp_path / "jobs.json"
        f.write_text(json.dumps({
            "r1": {"id": "r1", "status": "running", "url": "https://youtu.be/x", "start_ms": 0, "end_ms": 1000},
            "d1": {"id": "d1", "status": "done", "url": "https://youtu.be/x", "start_ms": 0, "end_ms": 1000, "file": "x.mp4"},
        }))
        monkeypatch.setattr(jobs, "JOBS_FILE", f)
        jobs._queue = queue.Queue()
        jobs.load()
        r1 = jobs._jobs["r1"]
        assert r1["status"] == "queued"
        assert r1["stage"] == "queued"
        assert r1["message"] == "Re-queued after restart"
        assert jobs._queue.qsize() == 1  # re-queued for a worker
        assert jobs._jobs["d1"]["status"] == "done"  # finished jobs untouched

    def test_save_is_atomic(self, monkeypatch, tmp_path):
        """Write-to-temp + rename: a crash mid-write never leaves torn JSON."""
        monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "jobs.json")
        jobs._jobs = {"a": {"id": "a", "status": "done"}}
        jobs._save()
        assert (tmp_path / "jobs.json").exists()
        assert not (tmp_path / "jobs.json.tmp").exists()  # temp replaced by rename
        assert json.loads((tmp_path / "jobs.json").read_text()) == {"a": {"id": "a", "status": "done"}}

    def test_create_job(self):
        jid = jobs.create_job(_payload(start_ms=1000))
        assert len(jid) == 10
        job = jobs.get_job(jid)
        assert job["status"] == "queued"
        assert job["start_ms"] == 1000
        assert job["created_at"] > 0
        # Persisted to disk.
        assert json.loads(jobs.JOBS_FILE.read_text())[jid]["url"].startswith("https://youtu.be")

    def test_get_job_returns_copy(self):
        jid = jobs.create_job(_payload())
        job = jobs.get_job(jid)
        job["status"] = "changed"
        assert jobs.get_job(jid)["status"] == "queued"

    def test_get_job_missing(self):
        assert jobs.get_job("missing") is None

    def _done_job_with_file(self, jid, **over):
        _insert(jid, status="done", file="out.mp4", **over)
        f = jobs.JOBS_DIR / jid / "out.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        return f

    def test_create_job_reuses_completed_identical(self):
        self._done_job_with_file("c1")
        assert jobs.create_job(_payload()) == "c1"

    def test_create_job_reuses_running_identical(self):
        _insert("c2", status="running")
        assert jobs.create_job(_payload()) == "c2"

    def test_create_job_reuses_queued_identical(self):
        _insert("c2q", status="queued")
        assert jobs.create_job(_payload()) == "c2q"

    def test_create_job_new_when_done_file_missing(self):
        _insert("c3", status="done", file="gone.mp4")  # file deleted by TTL
        jid = jobs.create_job(_payload())
        assert jid != "c3"

    def test_create_job_new_when_error(self):
        _insert("c4", status="error", error="throttled")
        jid = jobs.create_job(_payload())
        assert jid != "c4"

    def test_create_job_new_on_different_params(self):
        _insert("c5", status="done", file="out.mp4", start_ms=5000)
        assert jobs.create_job(_payload(start_ms=6000)) != "c5"

    def test_create_job_reuse_normalizes_format_default(self):
        # A job created before the `format` field (None) still matches an mp4 request.
        self._done_job_with_file("c6")
        assert jobs.create_job(_payload(format="mp4")) == "c6"
        # ...but not a webm request.
        assert jobs.create_job(_payload(format="webm")) != "c6"

    def test_update_job(self):
        jid = jobs.create_job(_payload())
        jobs.update_job(jid, status="running", percent=42)
        assert jobs.get_job(jid)["status"] == "running"
        assert jobs.get_job(jid)["percent"] == 42
        assert json.loads(jobs.JOBS_FILE.read_text())[jid]["percent"] == 42

    def test_update_job_percent_monotonic(self):
        """Parallel video/audio report out of order → percent never regresses."""
        jid = jobs.create_job(_payload())
        jobs.update_job(jid, percent=70)
        jobs.update_job(jid, percent=30)  # lower (late/out-of-order report) → kept
        assert jobs.get_job(jid)["percent"] == 70
        jobs.update_job(jid, percent=90)
        assert jobs.get_job(jid)["percent"] == 90
        # Error state: percent=None is still allowed (isinstance guard).
        jobs.update_job(jid, percent=None)
        assert jobs.get_job(jid)["percent"] is None

    def test_update_job_missing_noop(self):
        jobs.update_job("missing", status="x")  # must not error

    def test_job_dir(self):
        assert jobs.job_dir("abc") == jobs.JOBS_DIR / "abc"

    def test_fmt_ms(self):
        assert jobs._fmt_ms(None) == "-"
        assert jobs._fmt_ms(0) == "00:00:00.000"
        assert jobs._fmt_ms(4_365_000) == "01:12:45.000"
        assert jobs._fmt_ms(-100) == "00:00:00.000"  # forced >= 0

    def test_unsubscribe(self):
        jid = "u1"
        _insert(jid)
        q1 = jobs.subscribe(jid)
        q2 = jobs.subscribe(jid)
        # A queue that isn't a subscriber → ValueError silently swallowed.
        jobs.unsubscribe(jid, queue.Queue())
        jobs.unsubscribe(jid, q1)
        # q2 still subscribed → key not popped.
        assert jid in jobs._subscribers
        jobs.unsubscribe(jid, q2)
        # Last subscriber removed → key popped.
        assert jid not in jobs._subscribers

    def test_update_job_full_subscriber(self):
        jid = "u2"
        _insert(jid)
        q = jobs.subscribe(jid)
        for _ in range(q.maxsize):  # fill the subscriber queue
            q.put_nowait({"x": 1})
        jobs.update_job(jid, percent=1)  # put_nowait Full → swallowed, no error
        assert jobs.get_job(jid)["percent"] == 1

    def test_cleanup_removes_expired(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jobs, "TTL_SECONDS", 1000)
        _insert("old", created_at=time.time() - 5000)
        (jobs.JOBS_DIR / "old").mkdir(parents=True, exist_ok=True)
        (jobs.JOBS_DIR / "old" / "x.mp4").write_text("x")
        _insert("fresh")
        jobs._cleanup()
        assert "old" not in jobs._jobs
        assert not (jobs.JOBS_DIR / "old").exists()
        assert "fresh" in jobs._jobs
        assert json.loads(jobs.JOBS_FILE.read_text()) == {"fresh": jobs._jobs["fresh"]}


class TestWorker:
    def _spawn_worker(self, process_stub):
        """Start a worker thread using the currently active queue."""
        thread = threading.Thread(target=jobs._worker_loop, daemon=True)
        thread.start()
        return thread

    def _wait_status(self, jid, want, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if jobs.get_job(jid)["status"] != "queued":
                return jobs.get_job(jid)
            time.sleep(0.02)
        return jobs.get_job(jid)

    def test_start_worker_idempotent(self):
        jobs._worker_started = False
        jobs.start_worker()
        assert jobs._worker_started is True
        n_after_first = sum(1 for t in threading.enumerate() if t.name == "cut-worker")
        jobs.start_worker()  # second call must not add a thread
        n_after_second = sum(1 for t in threading.enumerate() if t.name == "cut-worker")
        assert n_after_second == n_after_first

    def test_start_worker_spawns_configured_count(self, monkeypatch):
        """Parallel jobs: WORKER_COUNT workers drain the same queue."""
        before = sum(1 for t in threading.enumerate() if t.name == "cut-worker")
        jobs._worker_started = False
        monkeypatch.setattr(jobs, "WORKER_COUNT", 3)
        jobs.start_worker()
        after = sum(1 for t in threading.enumerate() if t.name == "cut-worker")
        assert after - before == 3  # exactly the configured pool size

    def test_worker_processes_job_and_calls_cleanup(self, monkeypatch):
        calls = []

        def fake_process(job_id):
            calls.append(job_id)
            jobs.update_job(job_id, status="done", percent=100)

        monkeypatch.setattr(jobs, "_process", fake_process)
        self._spawn_worker(fake_process)
        jid = jobs.create_job(_payload())
        job = self._wait_status(jid, "done")
        assert job["status"] == "done"
        assert calls == [jid]

    def test_two_workers_process_two_jobs_concurrently(self, monkeypatch):
        """Two workers drain the queue in parallel (Barrier(2) proves it:
        a serial worker would deadlock on the barrier and time out)."""
        barrier = threading.Barrier(2)
        entered: list[str] = []
        done = threading.Event()

        def fake_process(job_id):
            entered.append(job_id)
            barrier.wait(timeout=3)  # both must arrive at once
            jobs.update_job(job_id, status="done", percent=100)
            if len(entered) >= 2:
                done.set()

        monkeypatch.setattr(jobs, "_process", fake_process)
        threads = [
            threading.Thread(target=jobs._worker_loop, daemon=True)
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        jid1 = jobs.create_job(_payload(start_ms=1000))
        jid2 = jobs.create_job(_payload(start_ms=2000))
        assert done.wait(timeout=5), "two jobs did not run concurrently"
        for t in threads:
            t.join(timeout=2)
        assert jobs.get_job(jid1)["status"] == "done"
        assert jobs.get_job(jid2)["status"] == "done"

    def test_worker_yt_error(self, monkeypatch):
        def fake_process(job_id):
            raise ys.YtError("Private video — cannot be accessed without permission.")

        monkeypatch.setattr(jobs, "_process", fake_process)
        self._spawn_worker(fake_process)
        jid = jobs.create_job(_payload())
        job = self._wait_status(jid, "error")
        assert job["status"] == "error"
        assert job["stage"] == "error"
        assert job["error"] == "Private video — cannot be accessed without permission."
        assert job["message"] == "Failed"

    def test_worker_generic_exception(self, monkeypatch):
        def fake_process(job_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(jobs, "_process", fake_process)
        self._spawn_worker(fake_process)
        jid = jobs.create_job(_payload())
        job = self._wait_status(jid, "error")
        assert job["status"] == "error"
        assert "Internal error: boom" in job["error"]


class TestProcess:
    def _patch_pipeline(self, monkeypatch, out_file, info, probe_result):
        monkeypatch.setattr(jobs.ytdlp_service, "cut_section", lambda **kw: (out_file, info))
        monkeypatch.setattr(jobs.ytdlp_service, "probe", lambda path: probe_result)

    def test_job_missing(self):
        jobs._process("missing")  # must not error

    def test_success_fast(self, monkeypatch, tmp_path):
        jid = "j1"
        _insert(jid, mode="fast", start_ms=2000, end_ms=6000)
        out = tmp_path / "out.mp4"
        out.write_text("x")
        stages = []

        def fake_cut(**kw):
            kw["progress_cb"]("extract", None)
            kw["progress_cb"]("download", 50.0)
            kw["progress_cb"]("download", None)
            kw["progress_cb"]("encode", 30.0)
            kw["progress_cb"]("merge", 42.0)  # unknown stage → raw percent
            return out, {"title": "Title", "has_audio": True}

        monkeypatch.setattr(jobs.ytdlp_service, "cut_section", fake_cut)
        monkeypatch.setattr(jobs.ytdlp_service, "probe", lambda path: _probe_ok(duration=4.0))
        jobs._process(jid)
        job = jobs.get_job(jid)
        assert job["status"] == "done"
        assert job["stage"] == "done"
        assert job["percent"] == 100
        assert job["file"] == "out.mp4"
        assert job["download_url"] == "/api/download/j1"
        assert job["title"] == "Title"
        assert job["actual_duration_ms"] == 4000
        assert job["snap_delta_ms"] == 0

    def test_result_without_video(self, monkeypatch, tmp_path):
        jid = "j2"
        _insert(jid)
        self._patch_pipeline(monkeypatch, tmp_path / "out.mp4", {"title": "x", "has_audio": True}, _probe_ok(has_video=False))
        with pytest.raises(ys.YtError, match="contains no video stream"):
            jobs._process(jid)

    def test_audio_lost(self, monkeypatch, tmp_path):
        jid = "j3"
        _insert(jid)
        self._patch_pipeline(monkeypatch, tmp_path / "out.mp4", {"title": "x", "has_audio": True}, _probe_ok(has_audio=False))
        with pytest.raises(ys.YtError, match="Audio lost"):
            jobs._process(jid)

    def test_source_without_audio_not_checked(self, monkeypatch, tmp_path):
        jid = "j4"
        _insert(jid)
        out = tmp_path / "out.mp4"
        out.write_text("x")
        self._patch_pipeline(monkeypatch, out, {"title": "x", "has_audio": False}, _probe_ok(has_audio=False, duration=5.0))
        jobs._process(jid)
        assert jobs.get_job(jid)["status"] == "done"

    def test_out_of_sync_fast(self, monkeypatch, tmp_path):
        jid = "j5"
        _insert(jid, mode="fast")
        self._patch_pipeline(
            monkeypatch, tmp_path / "out.mp4", {"title": "x", "has_audio": True},
            _probe_ok(video_start=0.0, audio_start=4.0, duration=5.0),
        )
        with pytest.raises(ys.YtError, match="out of sync"):
            jobs._process(jid)

    def test_out_of_sync_accurate(self, monkeypatch, tmp_path):
        jid = "j6"
        _insert(jid, mode="accurate")
        self._patch_pipeline(
            monkeypatch, tmp_path / "out.mp4", {"title": "x", "has_audio": True},
            _probe_ok(video_start=0.0, audio_start=0.7, duration=5.0),
        )
        with pytest.raises(ys.YtError, match="out of sync"):
            jobs._process(jid)

    def test_start_time_none_skips_sync_check(self, monkeypatch, tmp_path):
        jid = "j7"
        _insert(jid)
        out = tmp_path / "out.mp4"
        out.write_text("x")
        self._patch_pipeline(
            monkeypatch, out, {"title": "x", "has_audio": True},
            _probe_ok(video_start=None, audio_start=None, duration=5.0),
        )
        jobs._process(jid)
        assert jobs.get_job(jid)["status"] == "done"

    def test_duration_deviates(self, monkeypatch, tmp_path):
        jid = "j8"
        _insert(jid, mode="accurate")
        self._patch_pipeline(
            monkeypatch, tmp_path / "out.mp4", {"title": "x", "has_audio": True},
            _probe_ok(duration=30.0),  # requested 5s, result 30s
        )
        with pytest.raises(ys.YtError, match="deviates too far"):
            jobs._process(jid)

    def test_timeout(self, monkeypatch, tmp_path):
        jid = "j9"
        _insert(jid)

        def fake_cut(**kw):
            raise subprocess.TimeoutExpired("yt-dlp", 3600)

        monkeypatch.setattr(jobs.ytdlp_service, "cut_section", fake_cut)
        with pytest.raises(ys.YtError, match="Process took too long"):
            jobs._process(jid)

    def test_webm_format_converts_after_cut(self, monkeypatch, tmp_path):
        jid = "jfmt"
        _insert(jid, format="webm")
        out = tmp_path / "out.mp4"
        out.write_text("x")
        converted = tmp_path / "final.webm"
        converted.write_text("x")
        calls = {}

        def fake_convert(src, fmt, out_dir, progress_cb, duration_s):
            calls.update(src=src, fmt=fmt, duration_s=duration_s)
            progress_cb(100.0)
            return converted

        monkeypatch.setattr(
            jobs.ytdlp_service, "cut_section",
            lambda **kw: (out, {"title": "T", "has_audio": True}),
        )
        monkeypatch.setattr(jobs.ytdlp_service, "convert_format", fake_convert)
        monkeypatch.setattr(jobs.ytdlp_service, "probe", lambda path: _probe_ok(duration=5.0))
        jobs._process(jid)
        job = jobs.get_job(jid)
        assert job["status"] == "done"
        assert job["file"] == "final.webm"  # converted file is what's served
        assert calls["src"] == out
        assert calls["fmt"] == "webm"
        assert calls["duration_s"] == 5.0  # requested clip duration

    def test_mp4_format_skips_conversion(self, monkeypatch, tmp_path):
        jid = "jfmt2"
        _insert(jid, format="mp4")
        out = tmp_path / "out.mp4"
        out.write_text("x")
        monkeypatch.setattr(
            jobs.ytdlp_service, "cut_section",
            lambda **kw: (out, {"title": "T", "has_audio": True}),
        )
        monkeypatch.setattr(
            jobs.ytdlp_service, "convert_format",
            lambda *a, **k: pytest.fail("mp4 must not trigger conversion"),
        )
        monkeypatch.setattr(jobs.ytdlp_service, "probe", lambda path: _probe_ok(duration=5.0))
        jobs._process(jid)
        assert jobs.get_job(jid)["status"] == "done"
        assert jobs.get_job(jid)["file"] == "out.mp4"


class TestRedisMode:
    """Optional multi-node mode: shared store/queue/lease/pubsub (fakeredis)."""

    def _redis(self, monkeypatch):
        r = fakeredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(jobs, "REDIS_URL", "redis://test")
        monkeypatch.setattr(jobs, "_redis", r)
        monkeypatch.setattr(jobs, "_redis_failed", False)
        return r

    def test_create_get_update_roundtrip(self, monkeypatch):
        r = self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        assert len(jid) == 10
        assert jobs.get_job(jid)["status"] == "queued"
        jobs.update_job(jid, status="running", percent=50)
        assert jobs.get_job(jid)["percent"] == 50
        assert jid in r.hgetall("tubesnip:jobs")  # persisted in the shared store
        assert r.lindex("tubesnip:queue", 0) == jid  # queued for any worker

    def test_update_job_monotonic(self, monkeypatch):
        self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        jobs.update_job(jid, percent=70)
        jobs.update_job(jid, percent=30)  # out-of-order report → kept
        assert jobs.get_job(jid)["percent"] == 70

    def test_get_job_missing(self, monkeypatch):
        self._redis(monkeypatch)
        assert jobs.get_job("missing") is None

    def test_dedup_across_nodes(self, monkeypatch, tmp_path):
        r = self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        jobs.update_job(jid, status="done", percent=100, file="out.mp4")
        f = jobs.job_dir(jid) / "out.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        assert jobs.create_job(_payload()) == jid  # same params → reuse
        assert jobs.create_job(_payload(start_ms=1)) != jid  # different → new

    def test_sweeper_requeues_expired_lease(self, monkeypatch):
        r = self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        r.lpop("tubesnip:queue")  # a worker claimed it
        r.set(f"tubesnip:lease:{jid}", "1", ex=1)
        jobs.update_job(jid, status="running", percent=10)
        r.delete(f"tubesnip:lease:{jid}")  # worker died, lease expired
        jobs._sweep_once()
        assert jobs.get_job(jid)["status"] == "queued"
        assert r.lindex("tubesnip:queue", 0) == jid  # re-queued for another node

    def test_sweeper_ttl_removes_job_and_files(self, monkeypatch, tmp_path):
        r = self._redis(monkeypatch)
        monkeypatch.setattr(jobs, "TTL_SECONDS", 1000)
        jid = jobs.create_job(_payload())
        jobs.update_job(jid, status="done", file="out.mp4")
        f = jobs.job_dir(jid) / "out.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        raw = json.loads(r.hget("tubesnip:jobs", jid))
        raw["created_at"] = time.time() - 2000  # age past TTL
        r.hset("tubesnip:jobs", jid, json.dumps(raw))
        jobs._sweep_once()
        assert jobs.get_job(jid) is None
        assert not jobs.job_dir(jid).exists()

    def test_update_job_publishes_to_sse_subscribers(self, monkeypatch):
        r = self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        q = jobs.subscribe(jid)
        jobs._subscriptions_started = False
        jobs._start_redis_listener()
        jobs.update_job(jid, status="running", percent=42)
        deadline = time.time() + 2
        got = None
        while time.time() < deadline:
            try:
                got = q.get_nowait()
                break
            except queue.Empty:
                time.sleep(0.01)
        assert got is not None and got["percent"] == 42

    def test_load_is_noop_in_redis_mode(self, monkeypatch):
        r = self._redis(monkeypatch)
        r.hset("tubesnip:jobs", "x1", json.dumps({"id": "x1", "status": "running"}))
        jobs.load()
        assert jobs._jobs == {}  # memory stays empty; redis is authoritative

    def test_redis_client_connects_lazily(self, monkeypatch):
        fake = fakeredis.FakeRedis(decode_responses=True)

        class Lib:
            @staticmethod
            def from_url(url, **kw):
                return fake

        monkeypatch.setattr(jobs, "REDIS_URL", "redis://test")
        monkeypatch.setattr(jobs, "_redis", None)
        monkeypatch.setattr(jobs, "_redis_failed", False)
        monkeypatch.setattr(jobs, "_redis_lib", Lib)
        jobs._subscriptions_started = False
        assert jobs._r() is fake
        assert jobs._redis is fake  # cached for later calls

    def test_redis_client_connect_failure_falls_back(self, monkeypatch):
        class Boom:
            @staticmethod
            def from_url(url, **kw):
                raise ConnectionError("redis down")

        monkeypatch.setattr(jobs, "REDIS_URL", "redis://test")
        monkeypatch.setattr(jobs, "_redis", None)
        monkeypatch.setattr(jobs, "_redis_failed", False)
        monkeypatch.setattr(jobs, "_redis_lib", Boom)
        assert jobs._r() is None
        assert jobs._redis_failed is True  # no retry spam, single-node fallback

    def test_claim_redis_sets_lease_and_release(self, monkeypatch):
        r = self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        assert jobs._claim() == jid
        assert r.exists(f"tubesnip:lease:{jid}")  # worker is alive
        jobs._release(jid)
        assert not r.exists(f"tubesnip:lease:{jid}")

    def test_claim_redis_timeout_returns_none(self, monkeypatch):
        r = self._redis(monkeypatch)
        monkeypatch.setattr(r, "blpop", lambda *a, **k: None)
        assert jobs._claim() is None  # BLPOP timeout → worker loops again

    def test_cleanup_is_noop_in_redis(self, monkeypatch):
        self._redis(monkeypatch)
        jobs._cleanup()  # TTL handled by the sweeper, not the worker

    def test_sweep_once_memory_mode(self):
        jobs._sweep_once()  # no redis → no-op

    def test_sweep_once_skips_when_lock_held(self, monkeypatch):
        r = self._redis(monkeypatch)
        r.set("tubesnip:sweep_lock", "1", nx=True, ex=25)  # another node sweeping
        jid = jobs.create_job(_payload())
        jobs._sweep_once()  # returns early — no double sweep / no deleted jobs
        assert jobs.get_job(jid) is not None

    def test_sweep_skips_corrupt_jobs(self, monkeypatch):
        r = self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        r.hset("tubesnip:jobs", "bad", "{corrupt")
        jobs._sweep_once()  # must not crash on a malformed entry
        assert jobs.get_job(jid) is not None  # valid jobs still intact

    def test_update_job_missing_redis(self, monkeypatch):
        self._redis(monkeypatch)
        assert jobs.update_job("missing", percent=1) is None

    def test_dedup_skips_corrupt_jobs(self, monkeypatch):
        r = self._redis(monkeypatch)
        r.hset("tubesnip:jobs", "bad", "{corrupt")
        jid = jobs.create_job(_payload())
        assert jid != "bad"  # corrupt entry skipped, new job created

    def test_ensure_dirs_shared(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jobs, "SHARED_DIR", tmp_path)
        jobs.ensure_dirs()
        assert (tmp_path / "jobs").is_dir()

    def test_worker_survives_claim_errors(self, monkeypatch):
        """A Redis blip in _claim must NOT kill the worker thread (regression:
        it used to die permanently → jobs silently stopped processing)."""
        calls = {"n": 0}

        def flaky_claim():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise ConnectionError("redis down")
            raise SystemExit  # break the infinite loop after proving resilience

        monkeypatch.setattr(jobs, "_claim", flaky_claim)
        monkeypatch.setattr(jobs.time, "sleep", lambda _: None)
        with pytest.raises(SystemExit):
            jobs._worker_loop()
        assert calls["n"] == 4  # 3 errors handled + the exit signal

    def test_update_job_best_effort_on_redis_error(self, monkeypatch):
        """A transient Redis failure in update_job must not crash the cut."""
        r = self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        monkeypatch.setattr(r, "hget", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
        assert jobs.update_job(jid, status="running", percent=10) == 10  # no raise

    def test_supervisor_respawns_workers(self, monkeypatch):
        spawned: list[str] = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None):
                spawned.append(name)

            def start(self):
                pass

        monkeypatch.setattr(jobs.threading, "enumerate", lambda: [])  # all workers dead
        monkeypatch.setattr(jobs.threading, "Thread", FakeThread)
        monkeypatch.setattr(jobs, "WORKER_COUNT", 3)
        jobs._supervise_once()
        assert spawned.count("cut-worker") == 3  # back up to the pool size

    def test_dedup_reuses_running_redis(self, monkeypatch):
        self._redis(monkeypatch)
        jid = jobs.create_job(_payload())
        jobs.update_job(jid, status="running", percent=5)
        assert jobs.create_job(_payload()) == jid  # running → followed

    def test_get_job_redis_error_returns_none(self, monkeypatch):
        r = self._redis(monkeypatch)
        monkeypatch.setattr(r, "hget", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
        assert jobs.get_job("anything") is None  # no raise

    def test_start_worker_redis_spawns_sweeper_and_supervisor(self, monkeypatch):
        spawned: list[str] = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None):
                spawned.append(name)

            def start(self):
                pass

        monkeypatch.setattr(jobs.threading, "Thread", FakeThread)
        monkeypatch.setattr(jobs, "WORKER_COUNT", 2)
        jobs._worker_started = False
        self._redis(monkeypatch)
        jobs.start_worker()
        assert spawned.count("cut-sweeper") == 1
        assert spawned.count("cut-supervisor") == 1
        assert spawned.count("cut-worker") == 2

    def test_worker_loops_when_claim_empty(self, monkeypatch):
        calls = {"n": 0}

        def noop_claim():
            calls["n"] += 1
            if calls["n"] <= 2:
                return None  # BLPOP timeout → loop again
            raise SystemExit

        monkeypatch.setattr(jobs, "_claim", noop_claim)
        monkeypatch.setattr(jobs.time, "sleep", lambda _: None)
        with pytest.raises(SystemExit):
            jobs._worker_loop()
        assert calls["n"] == 3  # survived two empty polls

    def test_supervisor_loop_survives_errors(self, monkeypatch):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionError("down")
            raise SystemExit

        monkeypatch.setattr(jobs, "_supervise_once", boom)
        monkeypatch.setattr(jobs.time, "sleep", lambda _: None)
        with pytest.raises(SystemExit):
            jobs._supervisor_loop()
        assert calls["n"] == 3  # survived the errors

    def test_job_dir_uses_shared_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jobs, "SHARED_DIR", tmp_path)
        assert jobs.job_dir("abc") == tmp_path / "jobs" / "abc"
