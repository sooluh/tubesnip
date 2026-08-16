# Real E2E tests: info & cut a real YouTube video (needs network + ffmpeg).
#
# Skipped automatically (pytest.skip) when offline or ffmpeg/ffprobe is missing.
# Run:    uv run pytest -m e2e          (or included in a regular `uv run pytest`)
#         uv run pytest -m "not e2e"    (skip E2E)
import shutil
import socket
import threading
import time

import pytest

from tubesnip import jobs
from tubesnip import ytdlp_service as ys

# "Me at the zoo" — the oldest YouTube video (19s), stable & always available.
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _online() -> bool:
    """Check connectivity to YouTube (TCP 443). Called once per test session."""
    try:
        with socket.create_connection(("www.youtube.com", 443), timeout=3):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _skip_when_offline():
    if not _online():
        pytest.skip("offline — E2E tests skipped")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe unavailable — E2E tests skipped")


def _assert_sane_result(p: dict, expected_s: float, tolerance_s: float = 1.0) -> None:
    """Verify the cut result: has video, audio not lost, duration is sane."""
    assert p["has_video"], "result must have a video stream"
    assert p["has_audio"], "audio lost during cutting"
    assert abs(p["duration"] - expected_s) <= tolerance_s, (
        f"duration {p['duration']:.2f}s deviates from {expected_s}s"
    )
    if p["video_start"] is not None and p["audio_start"] is not None:
        assert abs(p["video_start"] - p["audio_start"]) <= 3.0, "A/V out of sync"


@pytest.mark.e2e
class TestInfo:
    def test_real_video_info(self):
        info = ys.get_video_info(TEST_URL)
        assert info["video_id"] == "jNQXAC9IVRw"
        assert abs(info["duration_ms"] - 19000) <= 2000
        assert info["resolutions"], "must have available resolutions"
        assert info["has_audio"] is True


@pytest.mark.e2e
class TestCutDirect:
    def test_fast(self, tmp_path):
        stages = []
        out, info = ys.cut_section(
            TEST_URL, 1000, 5000, "144", "fast", tmp_path,
            lambda st, p: stages.append((st, p)),
        )
        assert out.exists() and out.stat().st_size > 0
        assert ("extract", None) in stages and ("download", 0) in stages
        # Fast is frame-accurate at start (accurate_seek); the rest is only
        # container rounding at the end (±0.2s).
        _assert_sane_result(ys.probe(str(out)), expected_s=4.0, tolerance_s=1.0)

    def test_accurate(self, tmp_path):
        stages = []
        out, info = ys.cut_section(
            TEST_URL, 1000, 5000, "144", "accurate", tmp_path,
            lambda st, p: stages.append((st, p)),
        )
        assert out.exists() and out.stat().st_size > 0
        assert any(st == "encode" for st, _ in stages)
        # Frame-accurate re-encode: duration must be very close to 4.000s.
        _assert_sane_result(ys.probe(str(out)), expected_s=4.0, tolerance_s=0.5)


@pytest.mark.e2e
class TestApiFlow:
    def test_full_cut_poll_download(self, client, tmp_path):
        # The real worker (from app import) is stuck on the old queue; spawn a
        # worker using this test's active queue (see conftest._reset_jobs_state).
        threading.Thread(target=jobs._worker_loop, daemon=True).start()

        r = client.post(
            "/api/cut",
            json={"url": TEST_URL, "start_ms": 1000, "end_ms": 5000, "resolution": "144", "mode": "fast"},
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        # Poll until finished (300s safety cap for a real download).
        deadline = time.time() + 300
        job = None
        while time.time() < deadline:
            job = jobs.get_job(job_id)
            if job and job["status"] in ("done", "error"):
                break
            time.sleep(1)
        assert job is not None, "job was never processed"
        assert job["status"] == "done", f"job failed: {job.get('error')}"
        assert job["download_url"] == f"/api/download/{job_id}"

        d = client.get(f"/api/download/{job_id}")
        assert d.status_code == 200
        assert d.headers["content-type"].startswith("video/mp4")
        out = tmp_path / "e2e_result.mp4"
        out.write_bytes(d.content)
        assert out.stat().st_size > 0
        # Fast is frame-accurate at start (accurate_seek); the rest is only
        # container rounding at the end (±0.2s).
        _assert_sane_result(ys.probe(str(out)), expected_s=4.0, tolerance_s=1.0)
