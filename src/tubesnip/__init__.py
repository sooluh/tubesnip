"""TubeSnip — the fast and modern way to cut and download YouTube videos."""

from __future__ import annotations

import os

import uvicorn

__version__ = "0.1.0"


def main() -> None:
    # TUBESNIP_HOST defaults to loopback (safe local default); containers /
    # remote access must set it to 0.0.0.0 (see README → Docker).
    host = os.environ.get("TUBESNIP_HOST", "127.0.0.1")
    port = int(os.environ.get("TUBESNIP_PORT", "8000"))
    uvicorn.run("tubesnip.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
