# TubeSnip ✂️

Slice/trim YouTube videos server-side: paste a link → pick start/end
(hh:mm:ss.mmm) via text fields or sliders → choose a resolution (144p–2160p) →
download the result. Audio is preserved and stays in sync.

Stack: Python FastAPI + [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg, with an
environment managed by [uv](https://docs.astral.sh/uv/). The JS runtime for
`yt-dlp-ejs` and the frontend tests is **deno** (already installed). The frontend is
vanilla single-page JS (no build step).

## Setup

Prerequisites: `uv`, `deno`, `ffmpeg` (on PATH).

```bash
uv sync            # install dependencies from uv.lock
```

## Running

```bash
uv run tubesnip            # alias: uvicorn on 127.0.0.1:8000
# or
uv run uvicorn tubesnip.app:app --port 8000
```

Open http://127.0.0.1:8000

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/info` | POST | `{ "url": "…" }` → duration, title, available resolutions, `has_audio` |
| `/api/cut` | POST | `{ url, start_ms, end_ms, resolution, mode }` → `{ job_id }` (mode: `fast` \| `accurate`) |
| `/api/jobs/{id}` | GET | Job status: `queued` → `running` → `done` \| `error` (+ percent & message) |
| `/api/download/{id}` | GET | Download the mp4/mkv result (files are cleaned up automatically after TTL) |

## Tests & coverage

```bash
uv run pytest              # backend: 160 tests = 156 unit + 4 E2E (target >= 95%)
deno install --allow-scripts   # one-time: install frontend deps (happy-dom, oxlint) via deno
                               # (produces a deno-managed node_modules/ — no npm/package.json)
deno task lint             # anti-slop lint (oxlint via deno, 0 errors)
deno test --allow-read --allow-env tests/   # frontend: 74 steps (time.js & app.js, target >= 95%)
deno run --allow-run --allow-read --allow-write --allow-env \
  scripts/check-frontend-coverage.ts        # enforce frontend >= 95% (exit non-zero when below)
./scripts/check-coverage.sh  # enforce backend + frontend + lint all at once (for CI)
```

The **real E2E tests** (`tests/test_e2e.py`, marker `e2e`) cut an actual YouTube
video ("Me at the zoo", 19s): info, fast & precise (accurate) cuts, and the full
API flow (cut → SSE job events → download → ffprobe verification). They are
skipped automatically (`pytest.skip`) when **offline** or ffmpeg is unavailable,
so they're safe to run anytime:

```bash
uv run pytest -m e2e --no-cov     # run only E2E (--no-cov: coverage isn't for subsets)
uv run pytest -m "not e2e"        # unit tests only (no network)
```

Backend tests (`tests/test_*.py`) cover the FastAPI endpoints, the job store/worker,
and the whole `ytdlp_service` pipeline with mocked subprocesses & requests (no
network). Frontend tests (`tests/*.test.js`) cover `time.js` (pure) and `app.js`
(DOM interaction via happy-dom + mocked fetch).

All server logs (uvicorn startup, HTTP access, job worker) use a **single uniform
format** via `tubesnip/logconf.py`:
`2026-08-16 19:59:54,586 INFO     tubesnip.jobs   job …` — consistent across
`uvicorn.error`, `uvicorn.access`, and app loggers. Written to the console **and** a
rotating daily file `data/logs/app.log` (midnight rotation, 7-day history). The file
location can be changed via env `TUBESNIP_LOG_FILE`; set it to `off` for console-only.
Log level via `TUBESNIP_LOG_LEVEL` (default `INFO`): `DEBUG` shows the
ffmpeg/yt-dlp commands, Range proxy requests, and raw progress per callback — useful
for troubleshooting jobs (`TUBESNIP_LOG_LEVEL=DEBUG uv run tubesnip`).

The 95% coverage threshold is enforced automatically on both sides:

- **Backend** — `[tool.coverage.report] fail_under = 95` in `pyproject.toml`; `uv run
  pytest` fails when total coverage < 95%.
- **Frontend** — `scripts/check-frontend-coverage.ts`: runs
  `deno test --coverage` (profile in `cov_profile/`), converts to lcov via
  `deno coverage`, and fails (exit 1) when `app.js` / `time.js` < 95%. (`DENO_DIR`
  is pointed at the local `.deno_cache/` because `deno coverage` on 2.9.x fails when
  there are npm-cache files outside the project.)
- **Combined** — `scripts/check-coverage.sh` runs both (suitable for CI).

## Advanced options (private / age-restricted / geo)

For private, age-restricted, members-only, or geo-restricted videos, provide login
cookies via env (forwarded automatically to all yt-dlp calls):

```bash
export TUBESNIP_COOKIES="/path/to/cookies.txt"       # cookies file (Netscape format)
export TUBESNIP_COOKIES_FROM_BROWSER="chrome"        # or grab them straight from a browser
```

Alternative: create a `yt-dlp.conf` file in the project directory (read automatically
by yt-dlp):

```conf
cookies-from-browser chrome        # use browser login cookies
# proxy http://user:pass@host:port  # for geo-restricted videos
```

Note: private/age-restricted videos only work if the logged-in account actually has
access to them. Videos **without audio** can still be cut — the result is video-only
with a UI warning.

## Notes

- yt-dlp needs a JS runtime for full YouTube support; this project uses yt-dlp's
  **default runtime (deno)** and **browser impersonation** (`--impersonate chrome` via
  `curl_cffi`) to avoid bot blocking — forwarded automatically by `ytdlp_service.py`.
- Layered download path: `--download-sections` (lightweight, only the cut part) →
  byte-range fallback `[0, end_byte]` via `curl_cffi` → full download fallback (last
  resort). Fallbacks kick in automatically when a stream requires a Range header the
  ffmpeg downloader doesn't support.
- Very long videos (> 3 hours) can be slow because the fallback must download a
  larger chunk; prefer cutting short segments.
- Result files & job directories are cleaned up automatically after TTL (env
  `TUBESNIP_JOB_TTL_H`, default 24 hours).
- If YouTube blocks access, update yt-dlp: `uv run yt-dlp -U` (the nightly channel is
  recommended: `uv run yt-dlp --update-to nightly`).
- Job progress uses **SSE** (`/api/jobs/{id}/events` + EventSource) — real-time push
  without polling; if EventSource is unavailable (old proxy), the frontend falls back
  to 1s polling.
- Cut modes: **fast** (stream copy via a closed-Range proxy, fast; frame-accurate start
  via `accurate_seek` — no `-avoid_negative_ts make_zero`, which would force a keyframe
  snap) and **accurate** (frame-accurate re-encode down to the millisecond, slower).
- yt-dlp is resolved with a PATH that favors the project's venv binary — Homebrew
  binaries (Python without curl_cffi) make all impersonate targets "unavailable" →
  videos get rejected.
- Every result is verified with `ffprobe` before serving: the video & audio streams
  must exist (when the source has audio) and the audio-video start-time delta must be
  under the threshold (3s fast / 0.5s accurate) — out-of-sync results are rejected.
- Status: M0–M3 done (video info, cut-control UI, fast/accurate execution, automatic
  ffprobe verification, layered fallbacks, progress + download).
