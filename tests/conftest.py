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


@pytest.fixture()
def client():
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _reset_jobs_state():
    """Isolate global state between tests.

    `_queue` is swapped for an empty queue so the real worker (a daemon thread
    started at import time) stays stuck on the old queue and never processes
    jobs; tests that need worker behavior start their own thread.
    """
    jobs._jobs = {}
    jobs._queue = queue.Queue()
    yield
    # Drain leftover jobs so they don't leak into the next test.
    while True:
        try:
            jobs._queue.get_nowait()
        except queue.Empty:
            break
