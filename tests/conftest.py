"""Shared setup for all backend tests.

Point the data dir and TTL at a temp directory BEFORE importing the app/jobs
modules (module constants are read at import time). Global job state is reset
between tests.
"""
import os
import queue
import tempfile

os.environ.setdefault("TUBESNIP_DATA_DIR", tempfile.mkdtemp(prefix="tubesnip-test-"))
os.environ.setdefault("TUBESNIP_JOB_TTL_H", "24")

import pytest
from fastapi.testclient import TestClient

from tubesnip import app as app_module
from tubesnip import jobs
from tubesnip import ytdlp_service


@pytest.fixture()
def client():
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _reset_jobs_state(monkeypatch):
    """Isolate global state between tests.

    `_queue` is swapped for an empty queue so the real worker (a daemon thread
    started at import time) stays stuck on the old queue and never processes
    jobs; tests that need worker behavior start their own thread. The encode
    benchmark is also faked (a real ffmpeg benchmark would run per test and
    need ffmpeg installed).
    """
    jobs._jobs = {}
    jobs._queue = queue.Queue()
    jobs._redis = None
    jobs._redis_failed = False
    jobs._subscriptions_started = False
    jobs.REDIS_URL = ""
    jobs.SHARED_DIR = None
    ytdlp_service._info_cache.clear()
    ytdlp_service._calibration = {}
    ytdlp_service.encode_benchmark.cache_clear()
    monkeypatch.setattr(
        ytdlp_service, "encode_benchmark",
        lambda: {"x264_fps": 200.0, "vp9_fps": 80.0, "throughput_bps": 6_000_000},
    )
    yield
    # Drain leftover jobs so they don't leak into the next test.
    while True:
        try:
            jobs._queue.get_nowait()
        except queue.Empty:
            break
