# Tests for jobs.py: job store, worker, TTL cleanup, and the _process path (all mocked).
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

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

    def test_update_job(self):
        jid = jobs.create_job(_payload())
        jobs.update_job(jid, status="running", percent=42)
        assert jobs.get_job(jid)["status"] == "running"
        assert jobs.get_job(jid)["percent"] == 42
        assert json.loads(jobs.JOBS_FILE.read_text())[jid]["percent"] == 42

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
