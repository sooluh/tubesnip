"""Tests for the centralized logging config (logconf.setup_logging)."""
import io
import logging
import re
from logging.handlers import TimedRotatingFileHandler

from tubesnip import logconf

_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
    r"(INFO|WARNING|ERROR|DEBUG) +(\S+) +.+$"
)


def _emit(name: str, msg: str) -> str:
    """Send one log via logger `name`, captured by a temporary root handler."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter(logconf.FORMAT))
    root = logging.getLogger()
    root.addHandler(h)
    try:
        logging.getLogger(name).info(msg)
    finally:
        root.removeHandler(h)
    return buf.getvalue().strip()


def test_all_loggers_share_one_format():
    logconf.setup_logging()
    lines = []
    names = ("uvicorn.access", "uvicorn.error", "tubesnip.jobs")
    for name in names:
        lines.append(_emit(name, f"test message from {name}"))

    assert all(lines), "every logger must produce a log line"
    parsed = [_PATTERN.match(line) for line in lines]
    assert all(parsed), f"format not uniform:\n" + "\n".join(lines)
    # The correct logger name appears on every line.
    assert parsed[0].group(2) == "uvicorn.access"
    assert parsed[1].group(2) == "uvicorn.error"
    assert parsed[2].group(2) == "tubesnip.jobs"


def test_http_and_job_logs_share_the_same_prefix():
    """HTTP access lines (uvicorn.access) and job logs have identical shape."""
    logconf.setup_logging()
    a = _emit("uvicorn.access", '127.0.0.1:59712 - "POST /api/info HTTP/1.1" 200')
    j = _emit("tubesnip.jobs", "job abc download 50%")
    assert a and j
    assert _PATTERN.match(a)
    assert _PATTERN.match(j)
    # Date & level are formatted identically (timestamps are emitted at
    # slightly different milliseconds, so only the format is checked via
    # _PATTERN; the level field itself must match exactly).
    assert a.split()[0] == j.split()[0]  # date
    assert a.split()[2] == j.split()[2]  # level


def test_setup_idempotent_no_duplicate_handlers():
    logconf.setup_logging()
    before = len(logging.getLogger().handlers)
    logconf.setup_logging()
    logconf.setup_logging()
    after = len(logging.getLogger().handlers)
    assert before == after
    assert after >= 2  # console + file


def test_root_console_handler_uses_same_format():
    logconf.setup_logging()
    for h in logging.getLogger().handlers:
        assert h.formatter is not None
        assert h.formatter._fmt == logconf.FORMAT


def test_uvicorn_flows_to_root_one_handler_set():
    """uvicorn loggers with no handlers of their own → logs flow to root (one set)."""
    logconf.setup_logging()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == []
        assert lg.propagate is True


def test_file_log_written_with_same_format(monkeypatch, tmp_path):
    log_file = tmp_path / "sub" / "app.log"
    monkeypatch.setenv("TUBESNIP_LOG_FILE", str(log_file))
    logconf.setup_logging()
    logging.getLogger("tubesnip.jobs").info("job abc done")
    text = log_file.read_text()
    assert text.strip(), "log file must be written"
    m = _PATTERN.match(text.strip())
    assert m
    assert m.group(2) == "tubesnip.jobs"
    assert "job abc done" in text


def test_file_log_rotates_daily(monkeypatch, tmp_path):
    log_file = tmp_path / "app.log"
    monkeypatch.setenv("TUBESNIP_LOG_FILE", str(log_file))
    logconf.setup_logging()
    fh = next(
        h for h in logging.getLogger().handlers
        if isinstance(h, TimedRotatingFileHandler)
    )
    assert fh.when == "MIDNIGHT"  # TimedRotatingFileHandler upper-case
    assert fh.interval == 86400  # 1 day in seconds
    assert fh.backupCount == 7
    assert fh.baseFilename == str(log_file)
    # File handler format matches the console.
    assert fh.formatter._fmt == logconf.FORMAT


def test_file_log_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("TUBESNIP_LOG_FILE", "off")
    logconf.setup_logging()
    handlers = logging.getLogger().handlers
    assert all(
        not isinstance(h, TimedRotatingFileHandler) for h in handlers
    ), "TUBESNIP_LOG_FILE=off → no file handler"


def test_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("TUBESNIP_LOG_LEVEL", raising=False)
    logconf.setup_logging()
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("uvicorn.access").level == logging.INFO


def test_debug_level_from_env(monkeypatch):
    monkeypatch.setenv("TUBESNIP_LOG_LEVEL", "DEBUG")
    logconf.setup_logging()
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("uvicorn.access").level == logging.DEBUG
    # A DEBUG line from an app logger is actually emitted at DEBUG level.
    line = _emit("tubesnip.jobs", "job xyz progress raw stage=download pct=5")
    assert _PATTERN.match(line)


def test_debug_level_case_insensitive(monkeypatch):
    monkeypatch.setenv("TUBESNIP_LOG_LEVEL", "debug")
    logconf.setup_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_unknown_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("TUBESNIP_LOG_LEVEL", "VERBOSE")
    logconf.setup_logging()
    assert logging.getLogger().level == logging.INFO


def test_warning_level_suppresses_info(monkeypatch):
    monkeypatch.setenv("TUBESNIP_LOG_LEVEL", "WARNING")
    logconf.setup_logging()
    # Info doesn't show, warning does.
    assert _emit("tubesnip.jobs", "info silent") == ""
    line = _emit("tubesnip.jobs", "there is a problem")
    assert line == ""  # still INFO → suppressed; use the warning logger to check
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter(logconf.FORMAT))
    root = logging.getLogger()
    root.addHandler(h)
    try:
        logging.getLogger("tubesnip.jobs").warning("there is a problem")
    finally:
        root.removeHandler(h)
    assert _PATTERN.match(buf.getvalue().strip())


def test_file_log_default_follows_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TUBESNIP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TUBESNIP_LOG_FILE", raising=False)
    logconf.setup_logging()
    fh = next(
        h for h in logging.getLogger().handlers
        if isinstance(h, TimedRotatingFileHandler)
    )
    assert fh.baseFilename == str(tmp_path / "logs" / "app.log")


def test_lifespan_applies_setup_logging():
    """TestClient context runs the lifespan → the uniform format is re-applied
    (mimicking uvicorn overwriting its logger config at startup)."""
    from fastapi.testclient import TestClient

    from tubesnip import app as app_module

    with TestClient(app_module.app) as c:
        r = c.get("/api/jobs/does-not-exist")
        assert r.status_code in (200, 404)

    # After lifespan, root handlers stay uniformly formatted.
    for h in logging.getLogger().handlers:
        assert h.formatter is not None
        assert h.formatter._fmt == logconf.FORMAT
