# Test entry point (__init__.py: main + the __main__ block).
import runpy
from pathlib import Path

import uvicorn

import tubesnip


def test_main_runs_uvicorn(monkeypatch):
    called = {}

    def fake_run(app, **kw):
        called["app"] = app
        called["kw"] = kw

    monkeypatch.setattr(uvicorn, "run", fake_run)
    tubesnip.main()
    assert called["app"] == "tubesnip.app:app"
    assert called["kw"]["host"] == "127.0.0.1"
    assert called["kw"]["port"] == 8000


def test_main_respects_host_port_env(monkeypatch):
    called = {}
    monkeypatch.setenv("TUBESNIP_HOST", "0.0.0.0")
    monkeypatch.setenv("TUBESNIP_PORT", "9000")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: called.update(kw))
    tubesnip.main()
    assert called["host"] == "0.0.0.0"
    assert called["port"] == 9000


def test_main_block(monkeypatch):
    """Run __init__.py as __main__ → main() is called."""
    called = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.setdefault("x", True))
    path = str(Path(tubesnip.__file__).parent / "__init__.py")
    runpy.run_path(path, run_name="__main__")
    assert called.get("x") is True
