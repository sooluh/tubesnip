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
    assert called["kw"]["port"] == 8000


def test_main_block(monkeypatch):
    """Run __init__.py as __main__ → main() is called."""
    called = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.setdefault("x", True))
    path = str(Path(tubesnip.__file__).parent / "__init__.py")
    runpy.run_path(path, run_name="__main__")
    assert called.get("x") is True
