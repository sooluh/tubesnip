# TubeSnip ✂️

Slice/trim YouTube videos server-side: paste a link → pick start/end
(hh:mm:ss.mmm) via text fields or sliders → choose a resolution (144p–2160p) →
download the result. Audio is preserved and stays in sync.

Stack: Python FastAPI + [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg, with an
environment managed by [uv](https://docs.astral.sh/uv/). The JS runtime for
`yt-dlp-ejs` and the frontend tests is **deno**. The frontend is vanilla single-page
JS (no build step).

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
| `/api/cut` | POST | `{ url, start_ms, end_ms, resolution, mode, format }` → `{ job_id }` (mode: `fast` \| `accurate`; format: `mp4` default \| `mov` \| `webm`) |
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

Note: `TUBESNIP_COOKIES_FROM_BROWSER` only works on a machine with that browser (your
local host). In **containers**, bind-mount `cookies.txt` instead — see the
"Cookies in containers" section under Deployment.

Alternative: create a `yt-dlp.conf` file in the project directory (read automatically
by yt-dlp):

```conf
cookies-from-browser chrome        # use browser login cookies
# proxy http://user:pass@host:port  # for geo-restricted videos
```

Note: private/age-restricted videos only work if the logged-in account actually has
access to them. Videos **without audio** can still be cut — the result is video-only
with a UI warning.

## Configuration (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `TUBESNIP_HOST` / `TUBESNIP_PORT` | `127.0.0.1` / `8000` | Bind address. In containers set `TUBESNIP_HOST=0.0.0.0` |
| `TUBESNIP_CONCURRENCY` | `2` | Concurrent cut jobs. Each job already pulls 2 googlevideo streams, so keep it small; `1` = strictly serial |
| `TUBESNIP_JOB_TTL_H` | `24` | Result files / job records retention (hours) |
| `TUBESNIP_DATA_DIR` | `data` | Runtime dir: `jobs.json` + per-job temp files |
| `TUBESNIP_REDIS_URL` | – | **Optional multi-node mode.** When set, the job store, queue, worker lease and SSE fan-out live in Redis (shared across containers). Empty = single-node in-memory + JSON |
| `TUBESNIP_SHARED_DIR` | – | **Optional shared result storage** (mount NFS/EFS/S3 here across nodes). When set, per-job dirs live under `<shared>/jobs` so any node can serve a download. Required for cross-node downloads |
| `TUBESNIP_COOKIES` / `TUBESNIP_COOKIES_FROM_BROWSER` | – | yt-dlp cookies (login — fixes YouTube throttling on servers) |
| `TUBESNIP_LOG_LEVEL` | `INFO` | `DEBUG` shows ffmpeg/yt-dlp commands + raw progress |
| `TUBESNIP_LOG_FILE` | `data/logs/app.log` | `off` = console only |

## Deployment: Docker / multi-node / zero downtime

**Single container (recommended for personal use):**
- Run exactly **one app process per container**. Do **not** use `uvicorn --workers N` — job state, the worker pool, and SSE subscribers are in-process; extra uvicorn workers each get their own isolated (broken) state. Scale by running more containers, not more uvicorn workers.
- Persist `data/` on a volume so jobs survive container restarts.

The image is **Alpine-based** (small): ffmpeg/ffprobe via `apk --no-cache`, deno COPYed
from the official `denoland/deno:alpine` image (no curl install), the venv built with uv
on Alpine (musl wheels), nightly yt-dlp pinned via `[tool.uv.sources]`, plus a `/api/health`
healthcheck. `build_editable` no longer requires `README.md` in the image (removed the
`readme` field from `pyproject.toml`).

**Local / single host (Compose v2):**
```bash
docker compose -f compose.yml build
docker compose -f compose.yml up -d        # app + your external redis
```

**Swarm (multi-node, zero-downtime rolling updates):** Redis is **external** by design
— run it yourself (container on the same overlay network, host, or managed service) and
point `TUBESNIP_REDIS_URL` at it; it is not defined in `compose.yml`.
```bash
docker network create -d overlay tubesnip-net
# (deploy redis:7 to that network, or use an external host)
docker stack deploy -c compose.yml tubesnip   # replicas: 2, start-first rolling update
docker service update --image tubesnip:v2 tubesnip_tubesnip   # zero-downtime roll
```
`compose.yml` has `replicas: 2` + `update_config.order: start-first` — new tasks start
before old ones stop, so a deploy never drops a client or a job.

**Cookies in containers (bind mount, not browser):** `TUBESNIP_COOKIES_FROM_BROWSER`
needs a browser profile — there is none inside a container. Export `cookies.txt` from a
browser logged into Google (Netscape format, "Get cookies.txt LOCALLY" extension) and put
it in `./data/cookies.txt`; `compose.yml` bind-mounts it read-only:
```bash
cp cookies.txt data/cookies.txt
docker stack deploy -c compose.yml tubesnip
```
The app copies the source to the writable `data/cookies-cache.txt` before use (yt-dlp saves
cookies back at the end of a run — a read-only mount would fail with `EROFS`). **Refresh =
overwrite the host file and redeploy** — the app re-copies it automatically (mtime check),
so there's no config versioning dance:
```bash
cp cookies-baru.txt data/cookies.txt
docker stack deploy -c compose.yml tubesnip
```

**Teardown (stop everything, including external redis):**
```bash
docker stack rm tubesnip
docker service rm redis
docker network rm tubesnip-net                          # optional, full cleanup
```

**Restart safety (built in, both modes):**
- `jobs.json` (single-node) is written **atomically** (temp file + `os.replace`) — a crash mid-write never corrupts it.
- Single-node: a job that was `running`/`queued` at restart is **re-queued automatically**.
- Redis mode: every worker holds a **lease** (heartbeat via `update_job`); a job whose lease expires (node crashed) is **re-queued by the shared sweeper** — no job is ever stuck or lost.

**Multi-node (Redis mode):** set `TUBESNIP_REDIS_URL` on every container → jobs/queue/lease/SSE are shared; a load balancer can route anywhere. For **cross-node downloads**, the result files must also be reachable from any node — mount shared storage at `TUBESNIP_SHARED_DIR` (NFS/EFS/S3). Without shared storage, downloads only work from the node that cut the file (use sticky sessions). A container that dies mid-cut has its job re-queued by another node's sweeper; cutting is idempotent, so nothing is lost. Rolling deploys: bring up the new version, drain old nodes, `SIGTERM` — abandoned jobs are re-claimed automatically.

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
- Video metadata (`/api/info`) is cached per video_id (1-day TTL) — repeated loads of
  the same video skip the slow yt-dlp extract. Jobs with **identical params**
  (url + start/end + resolution + mode + format) are deduplicated: a running/queued one
  is followed, a finished one serves its cached result directly (no re-cut).
- If YouTube blocks access, update yt-dlp: `uv run yt-dlp -U` (the nightly channel is
  recommended: `uv run yt-dlp --update-to nightly`).
- Job progress uses **SSE** (`/api/jobs/{id}/events` + EventSource) — real-time push
  without polling; if EventSource is unavailable (old proxy), the frontend falls back
  to 1s polling.
- Cut modes: **fast** (stream copy via a closed-Range proxy, fast; frame-accurate start
  via `accurate_seek` — no `-avoid_negative_ts make_zero`, which would force a keyframe
  snap) and **accurate** (frame-accurate re-encode down to the millisecond, slower).
- Output **format**: `mp4` (default), `mov` (container remux — fast, H.264/AAC kept), or
  `webm` (re-encode to VP9/Opus — slower; H.264/AAC can't live in a webm container).
- Fast mode downloads video **and** audio in parallel (two ffmpeg processes) and uses a
  4 MiB proxy window — halves the download wall-time vs. the old sequential 1 MiB path.
- Jobs run **concurrently** in a small bounded pool (`TUBESNIP_CONCURRENCY`, default 2).
  Set `1` for strictly serial processing. The cap is deliberate: each job already pulls
  2 googlevideo streams, so a bigger pool can trip YouTube throttling and overload a
  small VM — raise it only if the box has headroom.
- **Hardware encoding (dynamic)**: when a GPU is exposed via `/dev/dri/renderD*` (Intel Arc /
  AMD VA-API — e.g. PCI passthrough into a VM), the precise-mode H.264 re-encode uses
  `h264_vaapi` and webm uses `av1_vaapi`/`vp9_vaapi` automatically. Detection is gated by a
  0.5s test encode — a broken driver/encoder silently falls back to CPU. No `/dev/dri`
  (e.g. QEMU's virtual VGA `1234:1111`) means CPU, exactly as before. GPU helps the
  *encode* steps; it does **not** fix the YouTube download throttling below.
- If a cut "stalls" at a low percent on a server, that's usually **YouTube throttling a
  datacenter IP** (a bot-check would instead abort with an error message). Fix: logged-in
  cookies via `TUBESNIP_COOKIES_FROM_BROWSER` or a residential proxy (see below).
- yt-dlp is resolved with a PATH that favors the project's venv binary — Homebrew
  binaries (Python without curl_cffi) make all impersonate targets "unavailable" →
  videos get rejected.
- Every result is verified with `ffprobe` before serving: the video & audio streams
  must exist (when the source has audio) and the audio-video start-time delta must be
  under the threshold (3s fast / 0.5s accurate) — out-of-sync results are rejected.
- Status: M0–M3 done (video info, cut-control UI, fast/accurate execution, automatic
  ffprobe verification, layered fallbacks, progress + download).
