# Tests for FastAPI endpoints (app.py) — service & jobs mocked, no network.
import json
import queue
import threading
import time
from pathlib import Path

import pytest

from tubesnip import app as app_module
from tubesnip import jobs, ytdlp_service


def _info_ok(**over):
    data = {
        "video_id": "abc123",
        "title": "Test Video",
        "duration_ms": 19000,
        "is_live": False,
        "has_audio": True,
        "resolutions": [{"height": 144, "fps": 30, "codec": "H.264", "ext": "mp4"}],
    }
    data.update(over)
    return data


class TestApiHealth:
    def test_ok_single_node(self, client, monkeypatch):
        monkeypatch.setattr(app_module.jobs, "_r", lambda: None)
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "redis": False}

    def test_ok_redis_connected(self, client, monkeypatch):
        monkeypatch.setattr(app_module.jobs, "_r", lambda: object())
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["redis"] is True


class TestApiInfo:
    def test_ok(self, client, monkeypatch):
        monkeypatch.setattr(app_module.ytdlp_service, "get_video_info", lambda url: _info_ok())
        r = client.post("/api/info", json={"url": "https://youtu.be/abc123"})
        assert r.status_code == 200
        assert r.json()["duration_ms"] == 19000

    def test_yt_error_400(self, client, monkeypatch):
        def boom(url):
            raise ytdlp_service.YtError("Video unavailable (private / removed / blocked).")

        monkeypatch.setattr(app_module.ytdlp_service, "get_video_info", boom)
        r = client.post("/api/info", json={"url": "https://youtu.be/abc123"})
        assert r.status_code == 400
        assert "unavailable" in r.json()["detail"]


class TestApiCut:
    def _mock_create(self, monkeypatch):
        monkeypatch.setattr(
            app_module.jobs, "create_job", lambda payload: "job12345"
        )
        monkeypatch.setattr(app_module.ytdlp_service, "extract_video_id", lambda url: "abc123")

    def test_ok(self, client, monkeypatch):
        self._mock_create(monkeypatch)
        r = client.post(
            "/api/cut",
            json={"url": "https://youtu.be/abc123", "start_ms": 0, "end_ms": 5000, "resolution": "best", "mode": "fast"},
        )
        assert r.status_code == 200
        assert r.json() == {"job_id": "job12345"}

    @pytest.mark.parametrize(
        "body,msg",
        [
            ({"url": "https://youtu.be/abc123", "start_ms": 0, "end_ms": 5000, "resolution": "best", "mode": "slow"},
             "mode must"),
            ({"url": "https://youtu.be/abc123", "start_ms": 0, "end_ms": 5000, "resolution": "best", "mode": "fast", "format": "avi"},
             "format must"),
            ({"url": "https://youtu.be/abc123", "start_ms": -1, "end_ms": 5000, "resolution": "best", "mode": "fast"},
             "start_ms must"),
            ({"url": "https://youtu.be/abc123", "start_ms": 5000, "end_ms": 5000, "resolution": "best", "mode": "fast"},
             "start_ms must"),
        ],
    )
    def test_mode_and_time_validation(self, client, body, msg):
        r = client.post("/api/cut", json=body)
        assert r.status_code == 400
        assert msg in r.json()["detail"]

    def test_format_default_mp4(self, client, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            app_module.jobs, "create_job",
            lambda payload: captured.update(payload=payload) or "job12345",
        )
        monkeypatch.setattr(app_module.ytdlp_service, "extract_video_id", lambda url: "abc123")
        r = client.post(
            "/api/cut",
            json={"url": "https://youtu.be/abc123", "start_ms": 0, "end_ms": 5000},
        )
        assert r.status_code == 200
        assert captured["payload"]["format"] == "mp4"

    @pytest.mark.parametrize("fmt", ["mp4", "mov", "webm"])
    def test_format_valid_values(self, client, monkeypatch, fmt):
        captured = {}
        monkeypatch.setattr(
            app_module.jobs, "create_job",
            lambda payload: captured.update(payload=payload) or "job12345",
        )
        monkeypatch.setattr(app_module.ytdlp_service, "extract_video_id", lambda url: "abc123")
        r = client.post(
            "/api/cut",
            json={"url": "https://youtu.be/abc123", "start_ms": 0, "end_ms": 5000, "format": fmt},
        )
        assert r.status_code == 200
        assert captured["payload"]["format"] == fmt

    def test_invalid_url(self, client, monkeypatch):
        monkeypatch.setattr(app_module.ytdlp_service, "extract_video_id", lambda url: None)
        r = client.post(
            "/api/cut",
            json={"url": "https://example.com/x", "start_ms": 0, "end_ms": 5000, "resolution": "best", "mode": "fast"},
        )
        assert r.status_code == 400
        assert "Invalid YouTube URL" in r.json()["detail"]

    def test_resolution_not_a_number(self, client, monkeypatch):
        monkeypatch.setattr(app_module.ytdlp_service, "extract_video_id", lambda url: "abc123")
        r = client.post(
            "/api/cut",
            json={"url": "https://youtu.be/abc123", "start_ms": 0, "end_ms": 5000, "resolution": "hd", "mode": "fast"},
        )
        assert r.status_code == 400
        assert "resolution must be a number" in r.json()["detail"]

    def test_resolution_out_of_range(self, client, monkeypatch):
        monkeypatch.setattr(app_module.ytdlp_service, "extract_video_id", lambda url: "abc123")
        r = client.post(
            "/api/cut",
            json={"url": "https://youtu.be/abc123", "start_ms": 0, "end_ms": 5000, "resolution": "99999", "mode": "fast"},
        )
        assert r.status_code == 400
        assert "range 144-4320" in r.json()["detail"]


class TestApiJob:
    def test_found(self, client, monkeypatch):
        monkeypatch.setattr(app_module.jobs, "get_job", lambda jid: {"id": jid, "status": "running"})
        r = client.get("/api/jobs/job12345")
        assert r.status_code == 200
        assert r.json()["status"] == "running"

    def test_not_found(self, client, monkeypatch):
        monkeypatch.setattr(app_module.jobs, "get_job", lambda jid: None)
        r = client.get("/api/jobs/missing")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"]


class TestApiJobEvents:
    def test_404_job_missing(self, client, monkeypatch):
        monkeypatch.setattr(app_module.jobs, "get_job", lambda jid: None)
        r = client.get("/api/jobs/missing/events")
        assert r.status_code == 404

    def test_stream_job_already_done(self, client, monkeypatch):
        """Job already finished → stream sends the snapshot then closes immediately."""
        monkeypatch.setattr(
            app_module.jobs, "get_job",
            lambda jid: {"id": jid, "status": "done", "percent": 100, "file": "x.mp4"},
        )
        monkeypatch.setattr(app_module.jobs, "subscribe", lambda jid: queue.Queue())
        monkeypatch.setattr(app_module.jobs, "unsubscribe", lambda *a: None)
        with client.stream("GET", "/api/jobs/job1/events") as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            data = r.read()
            assert b"data: {" in data
            assert b"done" in data

    def test_stream_updates_until_done(self, client, monkeypatch):
        """SSE: update_job → a new event is pushed; the stream ends at done."""
        import asyncio

        created = jobs.create_job({"url": "https://youtu.be/x", "start_ms": 0, "end_ms": 1000})
        try:
            events: list[dict] = []

            async def consume():
                # api_job_events subscribes internally; body_iterator = SSE generator.
                resp = await app_module.api_job_events(created)
                async for chunk in resp.body_iterator:
                    raw = chunk.decode() if isinstance(chunk, bytes) else chunk
                    for line in raw.splitlines():
                        if line.startswith("data: "):
                            events.append(json.loads(line[6:]))
                    if any(j.get("status") == "done" for j in events):
                        break

            # update_job (worker thread) runs on another thread → run the
            # consumer in asyncio, update from the main thread like the real worker.
            async def runner():
                task = asyncio.create_task(consume())
                for _ in range(100):
                    if events:
                        break
                    await asyncio.sleep(0.02)
                assert events, "stream did not send the initial snapshot"
                jobs.update_job(created, status="running", percent=42, message="Downloading…")
                await asyncio.sleep(0.05)
                jobs.update_job(created, status="done", percent=100, file="x.mp4")
                await asyncio.wait_for(task, timeout=5)

            asyncio.run(runner())
            statuses = [j.get("status") for j in events]
            assert "running" in statuses and "done" in statuses
            assert any(j.get("percent") == 42 for j in events)
        finally:
            jobs._jobs.pop(created, None)
            jobs._save()


class TestApiDownload:
    def _job_done(self, **over):
        job = {
            "id": "job12345",
            "status": "done",
            "file": "result.mp4",
            "title": 'Title "weird" : * ?',
            "resolution": "144",
            "start_ms": 2000,
            "end_ms": 6000,
        }
        job.update(over)
        return job

    def test_not_ready(self, client, monkeypatch):
        monkeypatch.setattr(app_module.jobs, "get_job", lambda jid: {"status": "running"})
        r = client.get("/api/download/job12345")
        assert r.status_code == 404

    def test_job_missing(self, client, monkeypatch):
        monkeypatch.setattr(app_module.jobs, "get_job", lambda jid: None)
        r = client.get("/api/download/job12345")
        assert r.status_code == 404

    def test_file_missing(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module.jobs, "get_job", lambda jid: self._job_done())
        monkeypatch.setattr(app_module.jobs, "job_dir", lambda jid: tmp_path)
        r = client.get("/api/download/job12345")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"]

    @pytest.mark.parametrize("suffix,expected_type", [
        ("mp4", "video/mp4"),
        ("mkv", "video/x-matroska"),
        ("mov", "video/quicktime"),
        ("webm", "video/webm"),
    ])
    def test_ok(self, client, monkeypatch, tmp_path, suffix, expected_type):
        f = tmp_path / f"result.{suffix}"
        f.write_bytes(b"FAKEVIDEO")
        monkeypatch.setattr(app_module.jobs, "get_job", lambda jid: self._job_done(file=f.name))
        monkeypatch.setattr(app_module.jobs, "job_dir", lambda jid: tmp_path)
        r = client.get("/api/download/job12345")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(expected_type)
        assert r.content == b"FAKEVIDEO"
        # Filename: sanitized title + time range + resolution.
        cd = r.headers["content-disposition"]
        assert 'Title "weird" : * ?' not in cd  # illegal chars dropped
        assert "00-00-02.000-00-00-06.000" in cd
        assert "144p" in cd


class TestDownloadName:
    def test_title_sanitized(self):
        job = {"title": 'a/b\\c:d*e?f"g<h>i|j', "file": "x.mp4", "resolution": "1080", "start_ms": 0, "end_ms": 1000}
        name = app_module._download_name(job)
        assert name.startswith("abcdefghij [")
        assert name.endswith("1080p.mp4")

    def test_empty_title_and_truncate(self):
        job = {"title": "   ", "file": "x.mp4", "resolution": "best", "start_ms": 0, "end_ms": 1000}
        assert app_module._download_name(job).startswith("video [")

        job2 = {"title": "x" * 200, "file": "x.mp4", "resolution": "best", "start_ms": 0, "end_ms": 1000}
        assert len(app_module._download_name(job2).split(" [")[0]) == 80

    def test_without_time(self):
        job = {"title": "T", "file": "x.mp4", "resolution": None}
        assert app_module._download_name(job) == "T [clip] best.mp4"

    def test_clock(self):
        assert app_module._clock(0) == "00-00-00.000"
        assert app_module._clock(2000) == "00-00-02.000"
        assert app_module._clock(61001) == "00-01-01.001"
        assert app_module._clock(-5) == "00-00-00.000"


class TestStatic:
    def test_index_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "TubeSnip" in r.text

    def test_assets(self, client):
        assert client.get("/app.js").status_code == 200
        assert client.get("/time.js").status_code == 200
        assert client.get("/style.css").status_code == 200
