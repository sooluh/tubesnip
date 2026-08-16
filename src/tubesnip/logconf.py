"""Centralized logging config — one consistent format, console + daily file.

The format used to be inconsistent: uvicorn had its own (``INFO:     127.0.0.1:... -
"GET ..." 200 OK``) while app logs used ``%(asctime)s %(levelname)s %(name)s: ...`` —
mixed together on the same console, messy. This module applies ONE format to all
logs (server startup, HTTP access, worker jobs) and writes them to the console
PLUS a rotating daily file (``data/logs/app.log``, kept 7 days).

Format: ``2026-08-16 19:28:56,079 INFO     tubesnip.jobs         job ...``

Log file:
- Default location ``<TUBESNIP_DATA_DIR>/logs/app.log`` (data/ is gitignored).
- Override with env ``TUBESNIP_LOG_FILE``; set ``off``/``none``/``0`` for
  console-only (no file).
- Rotates daily at midnight, keeps 7 days (``TimedRotatingFileHandler``).

Log level:
- Env ``TUBESNIP_LOG_LEVEL`` (default ``INFO``): ``DEBUG`` for job
  troubleshooting (ffmpeg/yt-dlp commands, proxy Range requests, raw progress),
  or ``WARNING``/``ERROR`` to reduce noise.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# One format for every logger. Logger names are padded so columns line up.
FORMAT = "%(asctime)s %(levelname)-8s %(name)-30s %(message)s"

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Daily rotation; keep the last 7 days.
_ROTATE_WHEN = "midnight"
_ROTATE_INTERVAL = 1
_ROTATE_BACKUP_COUNT = 7


def _log_level() -> int:
    """Log level from env ``TUBESNIP_LOG_LEVEL``; unknown values → INFO."""
    raw = (os.environ.get("TUBESNIP_LOG_LEVEL", "") or "").strip().upper()
    if not raw:
        return logging.INFO
    return getattr(logging, raw, logging.INFO)


def _log_file_path() -> Path | None:
    """Log file path; None = console-only. Empty `TUBESNIP_LOG_FILE` → default
    ``<TUBESNIP_DATA_DIR>/logs/app.log`` (follows data dir, follows gitignore)."""
    raw = os.environ.get("TUBESNIP_LOG_FILE", "")
    if raw.lower() in ("off", "none", "0"):
        return None
    if raw:
        return Path(raw)
    data_dir = os.environ.get("TUBESNIP_DATA_DIR", "data")
    return Path(data_dir) / "logs" / "app.log"


def setup_logging() -> None:
    """Attach uniformly formatted console + file handlers to the root logger.

    All loggers (uvicorn, uvicorn.error/access, tubesnip.*) propagate to the
    root — one handler set, same format, no double printing. Idempotent: old
    handlers are cleared then rebuilt (called on app import and in the
    lifespan startup because uvicorn overwrites its own logger config).
    """
    fmt = logging.Formatter(FORMAT)
    level = _log_level()
    root = logging.getLogger()
    root.setLevel(level)

    # Rebuild root handlers (idempotent).
    root.handlers.clear()
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    path = _log_file_path()
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            path,
            when=_ROTATE_WHEN,
            interval=_ROTATE_INTERVAL,
            backupCount=_ROTATE_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # uvicorn: drop its own handlers, let logs flow to root.
    for name in _UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers.clear()
        lg.propagate = True
