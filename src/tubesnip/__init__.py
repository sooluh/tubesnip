"""TubeSnip — the fast and modern way to cut and download YouTube videos."""

from __future__ import annotations

import uvicorn

__version__ = "0.1.0"


def main() -> None:
    uvicorn.run("tubesnip.app:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
