# PRD — TubeSnip (Server-Side YouTube Video Slicing/Trimming)

**Status:** Draft v2 — implementation M0–M9 partial complete (17 Aug 2026): M0–M8 + Redis-backed M9 done; shared result storage is infra (mount NFS/EFS/S3). Implementation findings in §19; multi-node/zero-downtime roadmap in §20
**Audience:** Single user (personal), self-hosted — with a scaling ambition to many servers
**Document purpose:** Research results + implementation plan before writing code.

---

## 1. Summary (TL;DR)

A **lightweight, fast, private** web app for slicing/trimming YouTube videos
server-side. The user just:

1. Pastes a YouTube link → the video embed appears.
2. Fills in **Start** and **End** (format `hh:mm:ss.mmm`), default `00:00:00.000` to the end of the video duration — plus a **slider** as an alternative input that stays in sync with the text fields.
3. Picks a **resolution** (from the list of resolutions actually available for that video, 144p–2160p).
4. Hits execute → the server cuts → the user **downloads** the result (video with audio intact and in sync).

The output is always a video file (mp4) that **keeps its audio**. There is no audio-only feature; audio is only a companion that must stay in sync with the video.

**Recommended stack:** Python (FastAPI) + `yt-dlp` + `ffmpeg` + vanilla JS frontend
(no build step). Full reasoning in §6.

---

## 2. Background & Problem

- The user wants a personal tool: lightweight, not heavy on a small server (VPS/Raspberry Pi), fast "sat set".
- Requirements: paste a YouTube link → trim with millisecond precision → pick a resolution 144p–2160p → the downloaded result **must not lose audio** and **audio must be in sync**.
- Two main technical challenges to solve with research:
  1. **Resolutions above 720p on YouTube are always separate video-only and audio-only streams** (DASH). There is no single muxed file. To get a result with audio, the server must download two streams and merge (mux) them — this is the main source of "audio missing / out of sync" issues.
  2. **Cutting with millisecond precision** requires re-encoding (slow, CPU-heavy), while cutting without re-encoding (stream copy) is only accurate to the keyframe (±2–6 seconds). These two needs conflict, so two modes are needed (see §9).

---

## 3. Research Results (with sources)

### 3.1 `yt-dlp --download-sections` — download only the cut part

- yt-dlp has a `--download-sections REGEX` option; the `*` prefix means a time range, e.g. `--download-sections "*00:01:30-00:02:45"`. This option **requires ffmpeg**.
- How it works: the download process is "handed over" to ffmpeg (ffmpeg downloader), which reads the stream via HTTP range requests and only downloads the data needed for that section — **it does not download the whole video**. This is the key to staying "lightweight" on bandwidth. (Source: yt-dlp README; yt-dlp GitHub issue #15036 — "we just hand the entire downloading process over to FFmpeg".)
- Note: cut points follow keyframes (see §3.3).

### 3.2 YouTube formats: progressive vs DASH (video-only + audio-only)

- **Progressive (muxed, video+audio in one file)** formats on YouTube practically only exist at ~360p (itag 18) and ~720p (itag 22). There is no reliable muxed version at 1080p+.
- All other resolutions (144p, 240p, 480p, 1080p, 1440p, 2160p, etc.) are served as separate **DASH video-only** (H.264/VP9/AV1) + **DASH audio-only** (AAC/Opus) streams. (Source: YouTube itag gists — sidneys/7095afe4da4ae58694d128b1034e01e2; MartinEesmaa/2f4b261cb90a47e9c41ba115a011a4aa.)
- Implication: **at almost every resolution, the server must download video-only + audio-only and then mux with ffmpeg `-c copy`**. This is normal and actually gives the best quality (DASH bitrates are higher than progressive at the same resolution).

### 3.3 Cut accuracy: stream copy (fast) vs re-encode (precise)

- Cutting with `-c copy` (no re-encode): very fast & light on CPU. **Correction from real practice (M5c):** with `-ss` input + `-c copy` WITHOUT `-avoid_negative_ts make_zero`, ffmpeg's `accurate_seek` (default) decodes-and-discards the keyframe→position segment, so the start is **frame-accurate (±1 frame)** — the claim "there is no frame-exact way without re-encoding" (video.stackexchange.com/q/16750) applies to naive tools, not ffmpeg accurate_seek. Verified: a 52s cut had a 0.035s delta, first frame a B-frame (not I).
- **Real trap:** `-avoid_negative_ts make_zero` on a cut/mux `-c copy` command DISABLES accurate_seek → snaps to keyframe (video 60→75 frames, first frame becomes I). Don't use it on the stream-copy path.
- Cutting with re-encode (`-ss` before `-i` + `-c:v libx264` / fast preset): frame/millisecond precision, but slower & heavier on CPU (especially 4K), plus a little generation loss.
- Conclusion: the app provides **two cut modes** (details in §9). User input stays in milliseconds; fast mode is now frame-accurate at the start (tail ±0.2s due to container rounding), precise mode is fully frame-accurate.

### 3.4 Audio-video sync when muxing two streams

- When cutting video-only and audio-only with **the same absolute timestamp range** and then muxing with ffmpeg `-c copy`, sync is preserved because ffmpeg aligns by the original stream timestamps (not byte order).
- Video `-ss input + -c copy` without make_zero → frame-accurate start (accurate_seek). Audio m4a `-ss input` → precision via index. Both are cut at the same absolute timestamp → after muxing, the A/V content lines up exactly.
- Safe practice used: use the same `-ss`/`-t` values for both streams, mux with `-c copy` **without** `-avoid_negative_ts make_zero` (that flag forces video back to a keyframe — see §3.3), then verify with `ffprobe` (check both streams' start timestamps).

### 3.5 Modern yt-dlp dependencies (important for deployment)

- yt-dlp needs **ffmpeg + ffprobe** for muxing and `--download-sections`.
- **Full YouTube support** now requires `yt-dlp-ejs` + a JavaScript runtime (deno, node, bun, or quickjs — this project uses **yt-dlp's default = deno**; no explicit `--js-runtimes` flags). (Source: yt-dlp README — "yt-dlp-ejs — Required for full YouTube support".)
- Recommended install: `yt-dlp[default,curl-cffi]` — `curl_cffi` for browser impersonation (TLS fingerprinting) sometimes needed by YouTube.
- yt-dlp often "breaks" because YouTube changes something → update regularly (`yt-dlp -U` / `pip install -U --pre "yt-dlp[default]"`; the **nightly** channel is recommended).

### 3.6 Getting duration & resolution list from the server

- `yt-dlp -J --no-download <url>` (single JSON dump) provides `duration`, the `formats` list (with `height`, `fps`, `vcodec`, `acodec`, `format_id`, `ext`), `title`, `video_id`.
- From that the server builds the list of resolutions **actually available** for that video (not a static list) — matching the "pick an available resolution" requirement.

---

## 4. Functional Requirements (from the user spec)

| ID  | Feature            | Detail                                                                                                                                                          |
| --- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| F1  | YouTube link input | Accept `youtube.com/watch?v=…`, `youtu.be/…`, `shorts/…`, `embed/…`, `live/…` URLs. Parse `video_id` server-side.                                               |
| F2  | Video embed        | Show a YouTube embed iframe (`enablejsapi=1`) once the link is valid.                                                                                           |
| F3  | Start & End fields | Format `hh:mm:ss.mmm` (e.g. `00:01:23.456`). Default Start = `00:00:00.000`, default End = the video's full duration.                                           |
| F4  | Start–end slider   | Dual-handle range slider as an alternative input. **Two-way sync** with the F3 text fields (moving the slider updates the fields, editing a field updates the slider). |
| F5  | Resolution picker  | **Button stack** with the resolutions available for that video (144p to 2160p + a "Best / Auto" option), rendered from `/api/info`.                              |
| F6  | No audio feature   | No audio-only mode / separate audio controls. Output is always a video that **still contains audio**.                                                           |
| F7  | Cut execution      | Click the button → the server cuts per start–end + resolution → progress is visible → a download link appears when done.                                         |
| F8  | Download result    | The cut mp4 file; audio is not lost and is in sync (see §10).                                                                                                   |
| F9  | Validation         | Start < End, End ≤ video duration, valid time format, video is not a live stream.                                                                               |

---

## 5. Non-Functional Requirements

- **Lightweight:** small memory footprint; the core process only shells out to `yt-dlp` + `ffmpeg` as subprocesses; no database (the job registry is a JSON file, written atomically — see §20).
- **Fast / bandwidth-efficient:** use `--download-sections` so only the cut part is downloaded (not the whole video); video metadata is cached (1-day TTL); identical requests reuse a finished job instead of re-cutting.
- **Private:** no accounts, no telemetry; all data stays local.
- **Concurrent:** a small worker pool (default 2, `TUBESNIP_CONCURRENCY`) processes jobs in parallel — capped deliberately because each job already pulls 2 googlevideo streams.
- **Error-tolerant:** clear error messages (private video, age-restricted, geo-restricted, live, etc.).
- **No lost jobs:** atomic job persistence + restart recovery — a job interrupted by a restart is re-queued, never stuck (`running` forever) and never silently dropped.
- **Clean:** temp files are deleted automatically (TTL) after download / failure.
- **Scaling ambition (future):** the user wants Dockerized, multi-node, multi-container, zero-downtime deployment. The current single-node in-memory design must evolve (see §20 for the honest gap + roadmap).

---

## 6. Architecture & Stack

### 6.1 Recommended stack

| Layer            | Choice                                                                  | Reason                                                                                                                                                                                               |
| ---------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backend          | **Python + FastAPI** (uvicorn)                                          | yt-dlp itself is Python-based → one runtime only; lightweight; easy async job control. More minimal alternative: Flask. Other option: Node.js/Express (still must shell out to yt-dlp + ffmpeg binaries). |
| Environment      | **uv (Astral)**                                                         | Fast, simple Python & dependency management (`uv venv` / `uv add`); already agreed with the user.                                                                                                     |
| Downloader       | **yt-dlp** (pip, extra `[default,curl-cffi]`, nightly channel)          | The only reliable tool for YouTube; `--download-sections` + automatic muxing.                                                                                                                         |
| Video processing | **ffmpeg + ffprobe** (system binaries)                                  | Cut, mux, verify.                                                                                                                                                                                    |
| Frontend         | **HTML + CSS + vanilla JS** single page, served by FastAPI (StaticFiles) | No framework, no build step, no Node — the most "sat set".                                                                                                                                            |
| Job store        | JSON file + per-job temp directory (atomic writes)                    | Single-node: a database is overkill. Multi-node needs a shared store — see §20. |
| JS runtime       | **deno** (yt-dlp's default for `yt-dlp-ejs`)                            | Agreed with the user: use yt-dlp's default runtime (deno already installed on the system); no `--js-runtimes` flags (see §3.5).                                                                        |

Estimated load: idle < 100 MB RAM; while a job runs, the main load is just ffmpeg/yt-dlp
(CPU is used during re-encode — precise mode; fast mode uses almost no CPU).

### 6.2 Workflow (end-to-end)

```
User            Server (FastAPI)                    yt-dlp / ffmpeg
│ paste URL      │                                    │
├───────────────►│ POST /api/info {url}               │
│                │── yt-dlp -J --no-download ────────►│ (duration, formats, title)
│                │◄── JSON ───────────────────────────│
│◄───────────────┤ {video_id, title, duration,        │
│                │  resolutions:[...]}                │
│ embed + form   │                                    │
│ fill start/end │                                    │
│ pick resolution│                                    │
│ click Cut      │                                    │
├───────────────►│ POST /api/cut {url,start,end,res}  │
│                │── yt-dlp --download-sections ... ─►│ download video+audio part
│                │── ffmpeg mux -c copy ─────────────►│ merge into .mp4
│◄───────────────┤ {job_id}                           │
│ poll status    │                                    │
├───────────────►│ GET /api/jobs/{id} ──► progress    │
│◄───────────────┤ {status, percent, url}             │
│ download       │                                    │
├───────────────►│ GET /api/download/{id} ──► file    │
│◄───────────────┤ (stream, then job+temp deleted)    │
```

### 6.3 File structure (plan)

```
tubesnip/
├── app.py            # FastAPI: /api/info, /api/cut, /api/jobs/{id}, /api/download/{id}
├── ytdlp_service.py  # yt-dlp subprocess wrapper (info, cut, progress parsing)
├── jobs.py           # JSON job store + TTL cleanup
├── static/
│   ├── index.html    # single page (embed + form + slider + button)
│   ├── app.js        # frontend logic, slider⇄field sync, polling
│   └── style.css
├── data/             # runtime: jobs.json + per-job temp dirs (gitignored)
├── requirements.txt
└── README.md         # install & run instructions
```

---

## 7. API Design

| Endpoint                 | Method | Request                                                                                       | Response                                                                                |
| ------------------------ | ------ | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `/api/info`              | POST   | `{ "url": "…" }`                                                                              | `{ video_id, title, duration_ms, resolutions: [{height, fps, codec, ext}], has_audio }` |
| `/api/cut`               | POST   | `{ "url", "start_ms", "end_ms", "resolution": 1080 \| "best", "mode": "fast" \| "accurate" }` | `{ job_id }`                                                                            |
| `/api/jobs/{job_id}`     | GET    | –                                                                                             | `{ status: queued\|running\|done\|error, stage, percent, download_url, error }`         |
| `/api/download/{job_id}` | GET    | –                                                                                             | mp4 file (streamed), then job + temp deleted                                            |

- Convention: `start_ms`/`end_ms` are sent in milliseconds (not strings) — parsing of `hh:mm:ss.mmm` happens on the frontend, re-validated on the server.
- Progress: the frontend uses **SSE** (`/api/jobs/{id}/events` + EventSource) — the server pushes on every `update_job`, the connection closes automatically on done/error; 1s polling fallback if EventSource is unavailable. Progress parsing from yt-dlp's `--progress-template` + ffmpeg `-progress pipe:1` (see §19.5).

---

## 8. UI/UX Spec (single page)

```
┌ Header: ✂ TubeSnip · The Fast and Modern Way to Cut and Download YouTube Videos ┐
├─────────────────────────────────────────────────────────────────┤
│ YouTube Video URL  [ https://…watch?v=…              ] [Load]   │
├─────────────────────────────────────────────────────────────────┤
│ ┌ Preview Embed Stream ────────────┐  ┌ Cut Parameters ────────┐ │
│ │  YouTube embed (iframe)          │  │ Resolution:             │ │
│ │                                  │  │  (•) Best (auto)        │ │
│ │  video title                     │  │  ( ) 1080p · 30fps      │ │
│ │ Start [00:00:00.000] End […]     │  │  ( ) 720p · 30fps       │ │
│ │ ├─●──────────────●───────────┤   │  Cut Mode:                │ │
│ │ 0:00         video duration      │  │  (Auto/Fast/Accurate)    │ │
│ └──────────────────────────────────┘  └────────────────────────┘ │
│                        [ Start Trim Process ]                    │
├─────────────────────────────────────────────────────────────────┤
│ Progress: percent 0%→100% · 4-step pipeline (extract → download │
│ → re-encode* → verify) · result card + download button           │
└─────────────────────────────────────────────────────────────────┘
(* re-encode step is skipped in fast mode)
```

- The design follows `prototype.htm` (light theme, cards, studio grid 1.2fr/0.8fr, Inter + JetBrains Mono, Font Awesome). CSS and JS stay separate (`style.css`, `app.js` + `time.js`); all IDs used by the logic are preserved.
- Embed: **plyr.io** (self-hosted vendor, no CDN). The playhead is read by **polling `plyr.currentTime`** (250 ms → "Playhead Position" chip); readiness is detected via `plyr.embed` + `plyr.duration > 0`. **Set Start / Set End** buttons take the playhead position as the clip boundary. Duration always comes from `/api/info` (source of truth).
- Slider: two handles with **1 ms** step (the text fields still allow manual typing); on long videos the handles stay precise because the ms step is supported by `<input type=range step=1>`.
- The cut mode is shown directly as **3 cards** (Auto/Fast/Accurate, like `prototype.htm` — not an accordion): default **Auto** (the system picks, fast preferred). "Accurate" mode: *"re-encode → exact cut down to the millisecond, slower"*; "Fast" mode: *"no re-encode → fast, frame-accurate start (tail ±0.2s)"*.

---

## 9. Cut Modes (important design decision)

|               | **Fast** mode (default)                     | **Accurate** mode                                                       |
| ------------- | ------------------------------------------- | ----------------------------------------------------------------------- |
| Core command  | `--download-sections "*S-E"` + mux `-c copy` | `--download-sections` (±3s margin) + local re-encode `-ss`/`-t` libx264 CRF18 |
| Accuracy      | Frame-accurate start (accurate_seek, ±1 frame); tail ±0.2s | Fully frame-accurate (millisecond precision)                |
| CPU / time    | Very light, fast                            | Heavy for 4K (can take minutes)                                         |
| Quality       | Lossless (stream copy)                      | Slight generation loss (set CRF ~18–20, veryfast/ultrafast preset)      |
| A/V sync      | Preserved (see §10)                         | Preserved, identical cut on both streams                                |

- Default = **Fast** (aligned with "lightweight, fast"). The user can pick **Accurate** if they need the cut exactly at the milliseconds they typed.
- Fast mode is now frame-accurate at the start, so there is no "snapped to keyframe" warning. The warning only appears when the result deviates from the request (anomaly > 0.3s).

---

## 10. Audio–Video Sync Guarantee (user's hard requirement)

1. **Always pick the best audio (`ba`) and mux** — never download muted video without audio. For any resolution, the format selector: `bv*[height≤H]+ba/b[height≤H]`.
2. **Cut both streams at the same absolute timestamp range** (`--download-sections` or identical `-ss`/`-to` for video & audio), then mux `-c copy -avoid_negative_ts make_zero`.
3. **Automatic verification before serving to the user:** `ffprobe` the output, ensure the video stream exists and (when the source has audio) the audio stream exists; the audio-vs-video start-timestamp delta must be under the threshold: **3 seconds (fast mode) / 0.5 seconds (accurate mode)**; plus a result-vs-request duration guard (0.5s accurate / 15s fast). On violation the job is marked `error` — out-of-sync files are **never delivered** to the user.
4. **Prefer mp4-compatible codecs:** pick H.264 video (`vcodec^=avc1`) + AAC audio when available; VP9/AV1 fallback is still muxed to mp4 (ffmpeg supports it); last resort `.mkv`.
5. Videos without audio (very rare on YouTube): the result is still produced as video-only with a note "this video has no audio".

---

## 11. Resolutions 144p–2160p: how it works

- The server builds the resolution list from the `formats` of `yt-dlp -J` (unique per `height`, taking the best variant: highest fps, best codec, highest bitrate).
- User picks H → format selector: `bv*[height=H]+ba/b[height=H]` (if exact H doesn't exist, fallback `[height≤H]`).
- **"Best"** option: `bv*+ba/b` (yt-dlp default) — the highest resolution available.
- No audio-only in the UI (per F6) — the `ba` audio is always merged automatically.

---

## 12. Error Handling & Edge Cases

| Case                                          | Handling                                                                                |
| --------------------------------------------- | --------------------------------------------------------------------------------------- |
| Not a YouTube URL / video not found           | 400 with a clear message                                                                |
| Private / age-restricted / members-only video | Specific message; document the `--cookies-from-browser` option (optional, off by default) |
| Geo-restricted                                | Specific message (proxy option noted in README)                                         |
| Live stream / premiere                         | Detect `is_live` → reject                                                               |
| Start ≥ End / End > duration                  | Server + client validation                                                              |
| Very long video (> 3 hours)                   | Note the ffmpeg seek risk (yt-dlp#5372); fallback: full download then local cut         |
| No audio in the source video                  | Produce video-only + note (see §10.5)                                                   |
| Job fails midway                              | `error` status + concise log; temp cleaned up                                           |
| yt-dlp outdated (YouTube break)               | "update yt-dlp" message; README includes the update command; run `yt-dlp -U` regularly  |

---

## 13. Security (even for single user)

- Parse `video_id` from the URL **on the server**; never pass a raw URL string to the shell (subprocess with a list of args, no shell).
- Restrict domains: only accept `youtube.com` / `youtu.be` URLs.
- Per-job temp directory + `job_id`-based result filenames (avoid path traversal).
- Re-validate all inputs server-side (don't trust the client).

---

## 14. Configuration & Deployment

- Env vars: `TUBESNIP_DATA_DIR` (default `./data`), `TUBESNIP_JOB_TTL_H` (default 24), `TUBESNIP_CONCURRENCY` (default 2), `TUBESNIP_COOKIES` / `TUBESNIP_COOKIES_FROM_BROWSER`, `TUBESNIP_LOG_LEVEL`, `TUBESNIP_LOG_FILE`, port.
- System dependencies: `python3.12+`, `ffmpeg`, `deno` (yt-dlp-ejs runtime + frontend tests); Python via **uv**: `uv venv` +
  `uv add fastapi uvicorn "yt-dlp[default,curl-cffi]"` (nightly channel).
- Run: `uv run tubesnip` (or `uv run uvicorn tubesnip.app:app --host 127.0.0.1 --port 8000`) (local access; if remote is needed, put a reverse proxy + simple auth in front — out of scope).
- `.gitignore` file: `data/`, `*.part`, `.venv/`.
- **Single process per container.** Do NOT use `uvicorn --workers N` — job state, the worker pool, and SSE subscribers live in-process; multiple uvicorn workers in one process tree would each have their own isolated state (broken job tracking). Scale by running more containers, not more uvicorn workers (see §20).
- **Docker (planned, M8):** multi-stage image (uv installs deps → runtime stage with `deno` + `ffmpeg`/`ffprobe` + the venv), `data/` on a volume, `SIGTERM` graceful stop. On restart, `jobs.load()` re-queues any interrupted `running` job — nothing is lost.

---

## 15. Roadmap / Milestones

| Milestone | Content                                                                             | Done criteria                                                       | Status |
| --------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------ |
| M0        | FastAPI scaffold + `POST /api/info` (yt-dlp -J, resolution list)                    | `curl` to `/api/info` returns a real video's duration & resolutions | ✅ |
| M1        | Frontend: URL input → embed → start/end fields + synced slider + resolution dropdown | Moving the slider changes the fields and vice versa; embed shows    | ✅ |
| M2        | `POST /api/cut` fast mode (`--download-sections` + mux) + progress + download       | Cuts a real video; result has in-sync audio; download works         | ✅ |
| M3        | Accurate mode (re-encode) + automatic ffprobe verification                          | Millisecond-precise cut; sync verification active                   | ✅ |
| M4        | Complete error handling, TTL cleanup, README                                        | All §12 cases handled; temp clean after finishing                   | ✅ |
| M5        | UX fixes: start+end sliders together, preview auto-seek/play, auto mode (fast>accurate) in advanced options, job restored from localStorage + multi-tab dialog, fast `_proxy_cut` path (closed-Range proxy) for in-video cuts | Sliders don't jump; preview follows start/end; auto mode without user intervention; reload doesn't lose the job; deep cuts (e.g. minute 82) much faster | ✅ |
| M5b       | **SSE** progress (replacing long polling), fix reload+dialog "Follow" showing nothing (player-section hidden), fix progress stuck at 5% (`-loglevel warning` suppressed stats → `-progress pipe:1`), audio output-seek read whole file → **input-seek** m4a, resolve yt-dlp to venv binary (Homebrew without curl_cffi → impersonate unavailable) | SSE stream flows in real time; reload + Follow shows progress; percent moves to 100; deep cuts finish in tens of seconds (not frozen); impersonate works | ✅ |
| M5c       | Fast mode **frame-accurate at the start**: `-ss input + -c copy` without `-avoid_negative_ts make_zero` (that flag disables accurate_seek → keyframe snap; on merge it also pushes video back to a keyframe) | 52s cut: 0.035s delta (was ±0.6–1s); first frame a B-frame; E2E tolerance lowered 2.5→1.0s; fast duration guard 15→2s | ✅ |
| M6        | Apply `prototype.htm` design to the frontend: light theme + brand header, studio grid (preview + controls left, parameters right), resolution becomes a **button stack** (from `/api/info`), cut mode becomes **3 directly-visible cards** (not an accordion), processing card + **4-step pipeline** from SSE stages, **result card** (resolution/duration/**est. size from bitrate** badges + download) | Old structure & IDs preserved (CSS/JS separate); frontend ≥ 95% coverage; verified live (embed, res-stack, pipeline, result card) | ✅ |
| M6b       | **Set Start / Set End** reading the playhead from the **plyr player** (polling `currentTime` 250 ms → "Playhead Position" chip); **est. file size** in the result card from `bitrate` (yt-dlp `tbr` kbps, sent by `/api/info`) × result duration | Real-time playhead; set start/end buttons change the cut range; accurate size estimate (verified live: 30s @ 1080p → 12.3 MB); backend + frontend ≥ 95% coverage | ✅ |
| M7        | **Parallel + robust single-node**: worker pool (`TUBESNIP_CONCURRENCY`, default 2), video+audio downloaded in parallel per job, weighted combined progress, video-info cache (1-day TTL), job dedup (identical params → reuse finished/follow running), atomic `jobs.json` writes (temp+rename), restart recovery (running jobs re-queued) | Jobs process concurrently; identical requests don't re-cut; restart never loses/sticks a job; crash never corrupts `jobs.json` | ✅ |
| M8        | **Dockerize** (multi-stage Alpine image: uv-built musl venv, ffmpeg via `apk --no-cache`, deno COPYed from `denoland/deno:alpine` — no apt/curl layers; `data/` volume; `/api/health`; `compose.yml` Swarm stack with external Redis + `start-first` rolling updates) | `docker compose up` runs TubeSnip; verified live in-container: yt-dlp nightly + deno JS runtime + ffmpeg h264/vp9/opus + `/api/info` end-to-end | ✅ |
| M9        | **Multi-node / zero-downtime**: shared job store + queue + SSE fan-out in **Redis** (`TUBESNIP_REDIS_URL`, optional), worker **lease + heartbeat + sweeper** (dead node → job re-queued, idempotent re-cut), cross-node dedup, shared result storage hook (`TUBESNIP_SHARED_DIR`) | Two+ containers share jobs/queue/leases/SSE; a crashed node's jobs are re-claimed; rolling deploys don't lose a job. Cross-node downloads need shared storage mounted | 🟨 store/queue/lease/SSE done — shared storage is infra (mount NFS/EFS/S3) |

---

## 16. Risks & Mitigation

| Risk                                                                                     | Impact                        | Mitigation                                                                   |
| ---------------------------------------------------------------------------------------- | ----------------------------- | ---------------------------------------------------------------------------- |
| YouTube changes its API / blocks server-side downloads                                   | Main feature breaks           | yt-dlp nightly + `curl_cffi`; scheduled updates; optional cookies docs        |
| `--download-sections` + mux has bugs in certain cases (long-video seek, weird streams)   | Result fails / out of sync    | Automatic ffprobe verification (§10.3); layered fallbacks: byte-range `[0,end_byte]` → full download (§19.3) |
| 4K re-encode slow on a small server                                                      | Bad UX in accurate mode       | Fast mode default; duration warning; ultrafast preset for 4K                  |
| Millisecond field precision vs keyframes (fast mode)                                     | User thinks the cut is exact  | Snap + transparency to the user (§9)                                          |

---

## 17. Decisions & Interpretations to Confirm

1. **"No audio support, video only"** — interpreted as: no audio-only feature/mode; the output is always a video that **still contains audio** (in sync). It does not mean audio is discarded. *(If the intent is that audio may be dropped, just say so — F8/§10 is cancelled.)*
2. **Precision vs speed** — resolved with two modes (Fast default, Accurate optional).
3. **Stack** — the user left the choice open ("I don't know what to use") → recommended Python/FastAPI; the Node alternative is noted in §6.1. **Already confirmed:** env uses **uv (Astral)**, the JS runtime (yt-dlp-ejs + frontend tests) uses **deno** — already installed; bun removed from the project.
4. **Output format** — mp4 (most compatible), mkv fallback.

---

## 18. Appendix: core command examples (implementation reference)

```bash
# Info (duration + formats + resolutions)
yt-dlp -J --no-download --no-playlist <url>

# Fast-mode cut: download only the 01:30–02:45 part, resolution ≤1080p, merge best audio
yt-dlp --no-playlist \
  -f "bv*[height<=1080]+ba/b[height<=1080]" \
  --download-sections "*00:01:30-00:02:45" \
  --merge-output-format mp4 \
  -o "data/jobs/<id>/out.%(ext)s" <url>

# Accurate-mode cut (actual implementation): download a wider part (±3s margin),
# then re-encode locally so the cut is frame-accurate to the millisecond
yt-dlp --no-playlist \
  -f "bv*[height<=1080]+ba/b[height<=1080]" \
  --download-sections "*00:01:27-00:02:48" \
  --merge-output-format mp4 \
  -o "data/jobs/<id>/raw.%(ext)s" <url>
ffmpeg -y -ss 00:01:30.000 -t 75.000 -i "data/jobs/<id>/raw.mp4" \
  -map 0:v:0 -map 0:a:0? -c:v libx264 -preset ultrafast -crf 18 \
  -c:a aac -b:a 192k -movflags +faststart "data/jobs/<id>/out.mp4"

# Sync verification (audio stream must exist; start-timestamp delta small)
ffprobe -v error -show_entries stream=codec_type,start_time -of json data/jobs/<id>/out.mp4
```

---

## 19. Implementation Findings (M0–M4)

### 19.1 Progress parsing: `--progress-template` + strict parser

- Progress is taken from yt-dlp stderr with `--newline` + `--progress-template "download:%(progress._percent_str)s"` so each line contains just the percent number.
- **Real bug found:** the ffmpeg downloader's stderr leaks through and contains googlevideo URLs with `%2C` / `%3D` (URL-encoded). A naive regex `(\d+\.?\d*)%` wrongly grabs the number before `%` in those URLs → progress jumps to a giant value (e.g. 1.3 billion %).
- Fix: a **strict** parser — `re.fullmatch` for `NN.N%` lines (template output) + a `[download] NN%` pattern fallback. Encoded URLs never match.

### 19.2 Sync verification & duration guard

- Every result is probed with `ffprobe` before serving: the video stream must exist; audio must exist when the source has it; the audio-vs-video `start_time` delta is under the threshold (3s fast / 0.5s accurate); the result-vs-request duration is guarded (0.5s / 15s).
- Violation → job `error`; out-of-sync files can never be downloaded.
- **Note:** ffprobe `start_time` on copy-merged output is always normalized to 0, so it can't report the cut position in the source video — the UI reports `snap_delta_ms` (result-vs-request duration delta). In fast mode this value is now ±0.2s (final container rounding), no longer a keyframe shift.

### 19.3 googlevideo 403 (`rqh=1`) & layered fallbacks

- Some streams (real example: a popular 4K video) are served with `rqh=1` — the server **requires a Range header on every request**. The ffmpeg downloader always fails with 403 because its first request has no Range; `--downloader-args` for adding Range is unreliable.
- Fix: **layered fallback** when `--download-sections` fails with an HTTP access error:
  1. **Byte-range `[0, end_byte]`** via `curl_cffi` (Range + impersonate) — always includes the file header so it can be parsed; size ≈ end position in the file (for a 2s cut from a 314s video: only ~2 MB, not 200 MB).
  2. **Full download** via the native downloader + local cut (last resort).
- **Impersonation is mandatory:** `--impersonate chrome` (curl_cffi) is baked into all yt-dlp calls — datacenter IPs are easily throttled/blocked by YouTube.

### 19.5 SSE progress & the "stuck at 5%" fix

- **SSE replaces long polling:** `jobs.py` has per-job pub/sub (`_subscribers: job_id → list[queue.Queue]`); `update_job` pushes the latest snapshot; `GET /api/jobs/{id}/events` streams `text/event-stream` (a `: ping` heartbeat every 15s) until `done`/`error`. The frontend uses `EventSource`; 1s polling fallback when EventSource is unavailable (old proxy).
- **Real bug — progress frozen at 5%:** the ffmpeg command used `-loglevel warning`, which **suppresses the `time=` stats lines** parsed by `_run_ffmpeg_progress` → percent never rises even while ffmpeg works (example: `sec0.mp4` 23.9 MB already fully downloaded but the UI sat at 5%). Fix: `-progress pipe:1` (machine-readable `key=value` format, `progress=end` on completion) — still works with `-loglevel warning`, and the update period is configurable via `-stats_period`.
- **Real bug — pathological audio output-seek:** audio seek used `-ss` after `-i` (output-seek) → ffmpeg reads the **entire audio file** (example: 69 Range requests, 327s for ~84 MB) while input-seek took only 2 requests/7s. DASH m4a audio has an index (sidx/moov) so input-seek is efficient; webm/opus without cues is not. Fix: prefer **m4a (AAC)** audio in the format selector + **input-seek** for audio.

### 19.6 Empty streams (YouTube throttling) & retry

- **Symptom:** the job fails at the merge step with the confusing message `Stream merge failed: Stream map '' matches no streams … Failed to set value '0:v:0' for option 'map'`. Cause: googlevideo streams are sometimes throttled → ffmpeg writes an **empty** result file (moov without tracks, ~260 bytes) but **exits 0**, so the `st_size == 0` guard doesn't catch it and the empty part slips through to merge. Intermittent (happens ~50% of the time while throttling is active), not tied to a specific resolution.
- **Fix 1 — part verification:** `_verify_part()` uses `probe()` (ffprobe) after each video/audio cut; if the expected stream is missing → a clear error ("result has no video stream — the YouTube stream may be throttled/empty, try again later").
- **Fix 2 — fresh-URL retry:** `_proxy_cut` wraps `_proxy_cut_once` with retries (max 3×) for "no stream"/"empty result" errors; each attempt fetches a fresh signed URL via `yt-dlp -g` (single-use URLs). Verified live: the user scenario (Lcvt7agiBmI, 01:12:45–01:13:39, 1080, fast) finished in 24.02s, video+audio valid.
- **Fix 3 — progressive-format fallback + backoff:** attempt 2 uses `_progressive_selector(f_sel)` → the combined `b` format (video+audio in one file), which is served from a different path than separate DASH — often available even when the DASH stream is empty. There's a `_RETRY_BACKOFF_S` (3s) pause between attempts so throttling can ease. The empty-DASH-audio scenario is also saved: video ok + audio empty → the `b` retry produces a complete file (audio in one file).

### 19.4 Misc

- **Default JS runtime:** yt-dlp uses its default runtime (**deno** — already installed); no explicit `--js-runtimes` flags on any call. (Note: briefly used `--no-js-runtimes --js-runtimes bun`, then reverted to the default at the user's request.)
- **Accurate mode ≠ `--force-keyframes-at-cuts`:** the actual implementation downloads a wider part (±3s margin) then re-encodes locally with `-ss`/`-t` — easier to verify and doesn't depend on a poorly documented flag's behavior.
- **Killing the server during testing:** `kill` on the `uv run` wrapper doesn't kill the uvicorn child (a leftover process keeps the port); use `pkill -f "uvicorn tubesnip.app"`.
- Automated tests: `uv run pytest` (160 tests = 156 unit + 4 E2E, coverage ≥ 95%) + `deno test` (74 steps, app.js coverage 98.83% / time.js 100%) — the 95% minimum is enforced automatically (see README "Tests & coverage").
- **yt-dlp on the system PATH can break impersonation:** Homebrew binaries are built with Python without `curl_cffi` → all impersonate targets "unavailable" → videos rejected ("Impersonate target chrome is not available"). Fix: `_run`/`_run_streaming` use a `PATH` env that favors the project venv's `bin/` (the venv binary is installed with `yt-dlp[default,curl-cffi]`).

---

## 20. Multi-node / zero-downtime plan (M8–M9, honest gap analysis)

### 20.1 Where we are (17 Aug 2026)

Redis-backed shared state is **implemented** (`TUBESNIP_REDIS_URL`, optional): the job
store, queue, worker lease + heartbeat, dedup, and SSE fan-out all run in Redis when
configured, and fall back to the in-process single-node design when it isn't. What still
needs infra is the result files.

| Component    | Single-node (no Redis)                              | Multi-node (Redis)                            |
| ------------ | --------------------------------------------------- | --------------------------------------------- |
| Job store    | `_jobs` dict + `jobs.json` (atomic writes)          | Redis hash `tubesnip:jobs` ✅                  |
| Job queue    | in-process `queue.Queue`                            | Redis list + BLPOP + **lease** (heartbeat) ✅  |
| Dead worker  | restart recovery re-queues                          | sweeper re-queues expired leases ✅            |
| SSE          | in-process pub/sub                                  | Redis pub/sub → per-node listener fan-out ✅   |
| Dedup        | in-process scan                                     | Redis scan (all nodes see the same jobs) ✅    |
| TTL cleanup  | worker `_cleanup`                                   | sweeper (single node sweeps via lock) ✅       |
| Result files | local `data/jobs/<id>/`                             | **needs `TUBESNIP_SHARED_DIR`** (NFS/EFS/S3 mount) — ⏳ infra |
| Cut scratch  | local temp + subprocesses                           | node-local scratch, final file on shared storage ⏳ |

The **lease is the load-bearing piece** for "no lost job": every `update_job` (every
progress tick) refreshes `tubesnip:lease:{id}` (120s TTL). If a node dies mid-cut, the
lease expires and the next sweep re-queues the job — idempotent cutting means it just gets
re-cut by another node. Rolling deploys work: `SIGTERM` an old container, its abandoned
jobs are re-claimed automatically.

### 20.3 What "no lost data/job" means today (already guaranteed single-node)

- **Crash during write:** `jobs.json` is written atomically (temp file + `os.replace`) — a crash mid-write leaves the previous valid file, never torn JSON.
- **Crash between writes:** the authoritative state is the in-memory dict; a killed process loses only the last seconds of progress ticks, and the job's *request* is what matters — it's persisted at creation (`create_job` → `_save`) and re-queued on restart if it was mid-flight.
- **Result never served broken:** ffprobe verification + sync/duration guards reject bad output before it can be downloaded (§10, §19.2).
- **Dedup re-serves** a finished job only while its result file still exists; otherwise it re-cuts (no stale/broken downloads).
