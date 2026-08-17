"""Wrapper for calling yt-dlp + ffmpeg as subprocesses.

Modern yt-dlp needs `yt-dlp-ejs` + a JavaScript runtime for full YouTube
support; this project uses yt-dlp's default runtime (**deno**, already
installed) — no explicit `--js-runtimes` flags.
"""
from __future__ import annotations

import functools
import http.server
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as crequests

logger = logging.getLogger("tubesnip.ytdlp")

def _ytdlp_env() -> dict:
    """PATH that prioritizes the current venv's binary.

    `yt-dlp` on the system PATH (e.g. Homebrew) may be built with Python
    without curl_cffi → all impersonate targets are "unavailable" → videos are
    rejected ("Impersonate target chrome is not available"). The venv binary
    (installed via uv/pip) always has curl_cffi, so we prefer it. `sys.prefix`
    is the venv dir regardless of whether the package is editable or copied
    into site-packages (important inside a Docker image).
    """
    env = dict(os.environ)
    venv_bin = Path(sys.prefix) / "bin"
    if venv_bin.exists():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


YTDLP = "yt-dlp"


def _cookie_args() -> list[str]:
    """Optional cookies from env — for private / age-restricted / members-only
    videos. Set via TUBESNIP_COOKIES (cookies.txt file) or
    TUBESNIP_COOKIES_FROM_BROWSER (e.g. "chrome"). Empty when not configured."""
    args: list[str] = []
    cf = os.environ.get("TUBESNIP_COOKIES")
    if cf:
        args += ["--cookies", _writable_cookies(cf)]
    cb = os.environ.get("TUBESNIP_COOKIES_FROM_BROWSER")
    if cb:
        args += ["--cookies-from-browser", cb]
    return args


def _writable_cookies(src: str) -> str:
    """yt-dlp SAVES the cookie jar back at the end of a run (it captures fresh
    cookies like PO tokens). A docker swarm config is mounted READ-ONLY at
    /run/secrets → that save fails with EROFS. Copy the source once into the
    writable data dir and hand yt-dlp the copy; the mounted config stays the
    immutable source. On local dev (writable source) it returns the path as-is."""
    src_path = Path(src)
    if not src_path.exists():
        return src
    data_dir = Path(os.environ.get("TUBESNIP_DATA_DIR", "data"))
    target = data_dir / "cookies-cache.txt"
    try:
        if not target.exists() or src_path.stat().st_mtime > target.stat().st_mtime:
            data_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src_path.read_bytes())
            target.chmod(0o600)
    except OSError:
        return src  # can't copy — fall back to the original path
    return str(target)


# yt-dlp-ejs uses yt-dlp's default JS runtime (deno — already installed on the
# system). Browser impersonation via curl_cffi to avoid bot blocking by YouTube
# + optional cookies.
BASE_ARGS = [
    "--impersonate", "chrome",
    *_cookie_args(),
]

# Supported YouTube URL patterns → extract video_id.
_YT_PATTERNS = [
    r"youtu\.be/([\w-]{6,})",
    r"youtube\.com/(?:watch\?.*v=|embed/|shorts/|live/|v/)([\w-]{6,})",
]

# get_video_info runs a full yt-dlp metadata extract (seconds on YouTube) —
# cache per video_id. Title/duration/resolutions rarely change, and caching
# avoids repeated bot-check/throttle exposure on reloads.
_INFO_CACHE_TTL_S = 86400.0  # 1 day
_info_cache: dict[str, tuple[float, dict]] = {}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class YtError(Exception):
    """Error from yt-dlp/ffmpeg with a user-friendly message."""


def extract_video_id(url: str) -> str | None:
    """Extract video_id from a YouTube URL; None if it's not a known YouTube URL."""
    for pat in _YT_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    logger.debug("yt-dlp %s", " ".join(args))
    return subprocess.run(
        [YTDLP, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_ytdlp_env(),
    )


def _friendly_error(stderr: str) -> str:
    """Translate yt-dlp stderr into a message the user can understand.

    Order matters: more specific patterns come first (e.g. age-check before
    bot-check because both start with "Sign in to confirm").
    """
    text = _ANSI_RE.sub("", stderr or "")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    last = lines[-1] if lines else "An unknown error occurred."
    low = text.lower()
    for frag, msg in [
        ("is not a valid url", "Invalid YouTube URL."),
        ("private video", "Private video — cannot be accessed without permission."),
        (
            "sign in to confirm your age",
            "Age-restricted video — requires age verification (login). See README for cookie options.",
        ),
        ("age-restricted", "Age-restricted video — requires age verification (login). See README for cookie options."),
        (
            "available to this channel's members",
            "Members-only video — requires login/subscription. See README for cookie options.",
        ),
        ("members-only", "Members-only video — requires login/subscription. See README for cookie options."),
        (
            "not available in your country",
            "Video is geo-restricted — try via proxy/VPN (see README).",
        ),
        ("geo-restricted", "Video is geo-restricted — try via proxy/VPN (see README)."),
        ("video unavailable", "Video unavailable (private / removed / blocked)."),
        ("video is unavailable", "Video unavailable (private / removed / blocked)."),
        (
            "unable to extract video data",
            "Failed to extract video data — yt-dlp may be outdated. Update: uv run yt-dlp -U",
        ),
        (
            "sign in to confirm",
            "YouTube is blocking automated access (bot-check). Try again, or update yt-dlp: uv run yt-dlp -U",
        ),
        (
            "could not find chrome cookies database",
            "TUBESNIP_COOKIES_FROM_BROWSER points to a browser profile that doesn't exist "
            "here (no Chrome installed — common in containers). Export cookies.txt from "
            "your browser and use TUBESNIP_COOKIES instead.",
        ),
        (
            "cookies database",
            "Browser cookies unavailable (TUBESNIP_COOKIES_FROM_BROWSER). In a "
            "server/container, export cookies.txt and use TUBESNIP_COOKIES instead.",
        ),
    ]:
        if frag in low:
            return msg
    return last[:300]


def _codec_label(vcodec: str) -> str:
    v = (vcodec or "").lower()
    if v.startswith("av01") or v.startswith("av1"):
        return "AV1"
    if v.startswith("vp9"):
        return "VP9"
    if v.startswith("avc1") or v.startswith("h264"):
        return "H.264"
    return vcodec or "?"


def _collect_resolutions(formats: list[dict]) -> list[dict]:
    """Unique available resolutions; per height keep the best variant (fps & bitrate)."""
    best: dict[int, dict] = {}
    for f in formats:
        vcodec = f.get("vcodec")
        if not vcodec or vcodec == "none":
            continue
        h = f.get("height")
        if not h:
            continue
        entry = {
            "height": h,
            "fps": f.get("fps") or 30,
            "codec": _codec_label(vcodec),
            "ext": f.get("ext") or "mp4",
            "bitrate": f.get("tbr") or 0,  # kbps (yt-dlp tbr) — for file size estimates
        }
        cur = best.get(h)
        if cur is None or (entry["fps"], entry["bitrate"]) > (cur["fps"], cur["bitrate"]):
            best[h] = entry
    return sorted(best.values(), key=lambda e: e["height"])


def get_video_info(url: str) -> dict:
    """Duration, title, resolution list, and audio info for a YouTube video."""
    vid = extract_video_id(url)
    if not vid:
        raise YtError("Invalid YouTube URL.")
    cached = _info_cache.get(vid)
    if cached and time.time() - cached[0] < _INFO_CACHE_TTL_S:
        return cached[1]
    watch_url = f"https://www.youtube.com/watch?v={vid}"

    proc = _run(
        [*BASE_ARGS, "-J", "--no-playlist", "--no-warnings", watch_url],
        timeout=120,
    )
    if proc.returncode != 0:
        raise YtError(_friendly_error(proc.stderr))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise YtError("Failed to parse yt-dlp response.")

    if data.get("is_live"):
        raise YtError("Live streams are not supported.")

    formats = data.get("formats", [])
    result = {
        "video_id": data.get("id") or vid,
        "title": data.get("title") or "",
        "duration_ms": int((data.get("duration") or 0) * 1000),
        "is_live": bool(data.get("is_live")),
        "has_audio": any(
            f.get("acodec") not in (None, "none") for f in formats
        ),
        "resolutions": _collect_resolutions(formats),
    }
    _info_cache[vid] = (time.time(), result)
    return result


# ---------------------------------------------------------------------------
# Cut pipeline (M2)
# ---------------------------------------------------------------------------

def _run_streaming(args: list[str], on_line, timeout: int = 3600) -> str:
    """Run yt-dlp, stream stderr line by line; return the stderr text."""
    logger.debug("yt-dlp (streaming) %s", " ".join(args))
    proc = subprocess.Popen(
        [YTDLP, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=_ytdlp_env(),
    )
    assert proc.stderr is not None
    err_lines: list[str] = []
    for raw in proc.stderr:
        line = raw.strip()
        if not line:
            continue
        err_lines.append(line)
        on_line(line)
    proc.wait(timeout=timeout)
    stderr = "\n".join(err_lines)
    if proc.returncode != 0:
        raise YtError(_friendly_error(stderr))
    return stderr


def _parse_percent(line: str) -> float | None:
    """Parse a yt-dlp progress line. Strict: don't misread encoded URLs
    (e.g. `met=1786855101%2C`) that leak through from the ffmpeg downloader stderr."""
    s = line.strip()
    # --progress-template: line is just a percent number, e.g. "42.3%"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)%", s)
    if m:
        return float(m.group(1))
    # Fallback default pattern: "[download]  42.3% of ..."
    m = re.match(r"\[download\]\s+(\d+(?:\.\d+)?)%", s)
    if m:
        return float(m.group(1))
    return None


def _fmt_section(ms: int) -> str:
    """ms → "H:MM:SS" (rounded to seconds, floor)."""
    t = max(0, ms) // 1000
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Closed-Range proxy (M5: fix slow deep cuts)
# ---------------------------------------------------------------------------
#
# googlevideo throttles **open** Range requests (`bytes=X-`) that ffmpeg uses
# when seeking deep into long fMP4 videos (ffmpeg scans hundreds of moof atoms
# to build an index → 38s for a 52s cut). Closed Range (`bytes=X-(X+1MB-1)`)
# stays fast. This local proxy closes ffmpeg's open ranges before forwarding
# them to googlevideo, so ffmpeg never hits throttling.

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")
_PROXY_WINDOW = 4 << 20  # 4 MiB per request — fewer googlevideo round-trips (1 MiB was 4x more)
_BYTE_POLL_INTERVAL = 0.5  # seconds: sample proxy bytes while decode is idle
_RETRY_BACKOFF_S = 3.0  # pause between empty-stream retries so throttling subsides


class _RangeProxy:
    """Local one-stream HTTP server: ffmpeg → 127.0.0.1:port → googlevideo.

    Every ffmpeg request is forwarded with a closed Range (always includes a
    Range header — googlevideo with `rqh=1` rejects the first request without
    one). Used as a context manager: `with _RangeProxy(url) as p:`.
    """

    def __init__(self, target_url: str):
        self.target = target_url
        self.url: str | None = None
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.requests = 0
        self.bytes_served = 0  # total bytes served to ffmpeg
        self.total_size: int | None = None  # total stream size (from Content-Range)
        self.expected_total: int | None = None  # fallback from the URL's `clen` param
        self.expected_frac: float | None = None  # fraction of the file that will be read
        self.first_start: int | None = None  # byte offset of ffmpeg's first request

    def byte_pct(self) -> float | None:
        """Real stream progress: bytes served / estimated bytes to read.

        ffmpeg input-seek does NOT read the file from byte 0 — it jumps to the
        cut position, so the denominator is not the total file size but the
        fraction actually read (clip + GOP margin). ffmpeg out_time is NOT
        used for -c copy cuts: source timestamps are preserved, so out_time
        jumps straight to the cut position (100%) from the first packet —
        proxy bytes are the honest, monotonic signal.
        """
        total = self.total_size or self.expected_total
        frac = self.expected_frac
        # No `dur` param in the URL → estimate the read fraction from ffmpeg's
        # first Range (bytes from the seek offset to the end of the file). Keeps
        # the bar moving during the input-seek decode phase instead of freezing.
        if frac is None and total and self.first_start is not None:
            frac = max(0.0, (total - self.first_start) / total)
        if not total or not frac:
            return None
        return min(100.0, self.bytes_served / (total * frac) * 100)

    def _make_handler(self):
        target = self.target
        counter = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet — don't spam stderr
                pass

            def handle_error(self, *a):  # ffmpeg closes the connection when done —
                pass  # expected, don't print a traceback

            def do_GET(self):
                # ffmpeg sends "Connection: close" but still reuses the
                # connection for the next request — force keep-alive.
                self.close_connection = False
                try:
                    self._handle(target)
                except Exception:
                    try:
                        self.send_response(502)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    except Exception:
                        pass

            def _handle(self, tgt: str):
                counter.requests += 1
                rng = self.headers.get("Range")
                logger.debug("proxy %s request #%d range=%s", tgt[:40], counter.requests, rng)
                headers = {"User-Agent": _UA}
                if rng:
                    m = _RANGE_RE.match(rng)
                    if m:
                        start = int(m.group(1))
                        if counter.first_start is None:
                            counter.first_start = start
                        end = m.group(2)
                        if end:
                            headers["Range"] = f"bytes={start}-{end}"
                        else:
                            headers["Range"] = f"bytes={start}-{start + _PROXY_WINDOW - 1}"
                else:
                    # rqh=1: the first request must carry a Range
                    headers["Range"] = f"bytes=0-{_PROXY_WINDOW - 1}"
                try:
                    resp = crequests.get(tgt, headers=headers, impersonate="chrome", timeout=120)
                except Exception:
                    raise
                body = resp.content
                if counter.total_size is None:
                    cr = resp.headers.get("Content-Range", "")
                    m = re.search(r"/(\d+)\s*$", cr)
                    if m:
                        counter.total_size = int(m.group(1))
                counter.bytes_served += len(body)
                self.send_response(resp.status_code)
                for k in ("Content-Type", "Content-Range", "Accept-Ranges"):
                    v = resp.headers.get(k)
                    if v:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def __enter__(self) -> "_RangeProxy":
        handler = self._make_handler()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # ffmpeg closes the connection when done → server treats it as an
        # error; silence it so it doesn't spam tracebacks to stderr.
        self._server.handle_error = lambda *a: None
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/s"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def find_output(out_dir: Path, prefix: str = "out") -> Path | None:
    cands = [
        p for p in out_dir.glob(f"{prefix}.*")
        if p.suffix.lower() not in (".part",)
    ]
    return cands[0] if cands else None


# ---------------------------------------------------------------------------
# Hardware-accelerated encoding (dynamic GPU detection)
# ---------------------------------------------------------------------------
#
# When the host exposes a GPU through /dev/dri/renderD* (Intel Arc via QSV →
# VA-API, or AMD via VA-API — e.g. after PCI passthrough into a VM), the
# encode steps below switch to a VA-API encoder automatically. Detection is
# gated by an actual 0.5s test encode: a broken driver/encoder falls back to
# CPU instead of producing corrupt output. CPU is always the fallback, so a
# machine without /dev/dri (like this one) behaves exactly as before.

_VAAPI_CANDIDATES = ("av1_vaapi", "vp9_vaapi", "h264_vaapi", "hevc_vaapi")


@functools.lru_cache(maxsize=4)
def _vaapi_best(devices: tuple[str, ...] | None = None) -> tuple[str, str] | None:
    """(device, encoder) for the first VA-API encoder that passes a tiny test
    encode, else None. `devices` is for tests; default reads /dev/dri/renderD*."""
    if devices is None:
        dri = Path("/dev/dri")
        devices = tuple(str(p) for p in dri.glob("renderD*")) if dri.exists() else ()
    for device in devices:
        for enc in _VAAPI_CANDIDATES:
            try:
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-vaapi_device", device,
                     "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5",
                     "-vf", "format=nv12,hwupload",
                     "-c:v", enc, "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except Exception:
                continue
            if proc.returncode == 0:
                return (device, enc)
    return None


def _reencode(
    src: Path, start_ms: int, end_ms: int, dst: Path, progress_cb
) -> None:
    """Precise-mode re-encode: frame-accurate cut with x264 (or HW H.264)."""
    duration = max(1, (end_ms - start_ms) / 1000)
    start = f"{start_ms / 1000:.3f}"
    length = f"{duration:.3f}"
    # HW via VA-API when a GPU is present; else CPU x264 (untested
    # encoders are rejected by the test-encode gate in _vaapi_best).
    vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18"]
    hw = _vaapi_best()
    if hw and hw[1] == "h264_vaapi":
        vcodec = [
            "-vaapi_device", hw[0],
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-qp", "20",
        ]
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
        # -progress pipe:1: key=value progress on stdout, still emitted even
        # with loglevel warning (stderr `time=` stats are suppressed by it).
        "-nostats", "-progress", "pipe:1",
        "-ss", start, "-t", length, "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        *vcodec,
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        _run_ffmpeg_progress(
            cmd, duration, lambda pct: progress_cb("encode", pct)
        )
    except YtError as e:
        raise YtError(f"Precise-mode re-encode failed: {e}") from e


def convert_format(src: Path, fmt: str, out_dir: Path, progress_cb, duration_s: float) -> Path:
    """Post-process a cut result into the requested container; return the new file.

    mp4: no-op (the cut pipeline already produces mp4). mov: container remux
    (-c copy) when the video codec is MOV-safe (H.264/HEVC), otherwise the
    video is re-encoded to H.264 first — AV1/VP9 can't live in a MOV container
    ("av1 only supported in MP4 and AVIF"). webm: re-encode to VP9/Opus
    (H.264/AAC can't live in a webm container), so it's slower than mov.
    The source file is removed on success.
    """
    if fmt == "mp4":
        return src
    dst = out_dir / f"final.{fmt}"
    if fmt == "mov":
        vcodec = (probe(str(src)).get("video_codec") or "").lower()
        if vcodec in ("h264", "hevc"):
            cmd = [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
                "-i", str(src),
                "-c", "copy",
                "-movflags", "+faststart",
                str(dst),
            ]
        else:
            # AV1/VP9/unknown → re-encode video to H.264 (audio AAC copies as-is).
            vargs = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18"]
            hw = _vaapi_best()
            if hw and hw[1] == "h264_vaapi":
                vargs = [
                    "-vaapi_device", hw[0],
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12,hwupload",
                    "-c:v", "h264_vaapi", "-qp", "20",
                ]
            cmd = [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
                "-i", str(src),
                "-map", "0:v:0", "-map", "0:a:0?",
                *vargs,
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(dst),
            ]
    else:  # webm — VP9/AV1 video + Opus audio (H.264/AAC can't go in webm)
        hw = _vaapi_best()
        if hw and hw[1] in ("av1_vaapi", "vp9_vaapi"):
            cmd = [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
                "-vaapi_device", hw[0],
                "-i", str(src),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12,hwupload",
                "-c:v", hw[1], "-b:v", "0", "-qp", "30",
                "-c:a", "libopus", "-b:a", "128k",
                str(dst),
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
                "-i", str(src),
                "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "5",
                "-crf", "32", "-b:v", "0",
                "-c:a", "libopus", "-b:a", "128k",
                str(dst),
            ]
    try:
        _run_ffmpeg_progress(cmd, max(1.0, duration_s), progress_cb)
    except YtError as e:
        raise YtError(f"Format conversion to {fmt} failed: {e}") from e
    src.unlink(missing_ok=True)
    return dst


def cut_section(
    url: str,
    start_ms: int,
    end_ms: int,
    height: str | int,
    mode: str,
    out_dir: Path,
    progress_cb,
) -> tuple[Path, dict]:
    """Cut a YouTube video, return (result file path, video info).

    mode "fast"      : closed-Range proxy + stream copy mux (fast; frame-
                       accurate start via accurate_seek).
    mode "accurate"  : download a wider section, then frame-accurate re-encode.
    progress_cb(stage, pct) is called with stage "extract"/"download"/"encode".
    """
    vid = extract_video_id(url)
    if not vid:
        raise YtError("Invalid YouTube URL.")
    watch_url = f"https://www.youtube.com/watch?v={vid}"

    progress_cb("extract", None)
    info = get_video_info(watch_url)
    duration_ms = info["duration_ms"]
    if start_ms < 0 or end_ms < 0:
        raise YtError("Times must not be negative.")
    if duration_ms <= 0:
        raise YtError("Video has no duration (live/streaming?).")
    if end_ms > duration_ms:
        raise YtError(
            f"End exceeds video duration ({_fmt_section(duration_ms)})."
        )
    if start_ms >= end_ms:
        raise YtError("Start must be before End.")

    f_sel = _format_selector(height, info["has_audio"])
    logger.debug(
        "cut_section: mode=%s start=%d end=%d height=%s has_audio=%s selector=%s",
        mode, start_ms, end_ms, height, info["has_audio"], f_sel,
    )

    out_dir.mkdir(parents=True, exist_ok=True)

    if mode == "accurate":
        # ±3s margin so re-encode can decode from a keyframe before start.
        section = f"*{_fmt_section(max(0, start_ms - 3000))}-{_fmt_section(end_ms + 3000)}"
        out_tmpl = str(out_dir / "raw.%(ext)s")
        progress_cb("download", 0)
        try:
            _run_streaming(
                [
                    *BASE_ARGS, "--no-playlist", "--no-warnings", "--newline",
                    "--progress-template", "download:%(progress._percent_str)s",
                    "-f", f_sel,
                    "--download-sections", section,
                    "--merge-output-format", "mp4",
                    "-o", out_tmpl,
                    watch_url,
                ],
                lambda line: _maybe_progress(line, progress_cb, "download"),
            )
        except YtError as e:
            # The ffmpeg downloader is incompatible with some streams (googlevideo
            # requires a Range header on the first request). Layered fallback.
            if not _needs_full_fallback(str(e)):
                raise
            try:
                final = _prefix_fallback(watch_url, f_sel, start_ms, end_ms, mode, out_dir, progress_cb)
            except YtError:
                final = _full_fallback(watch_url, f_sel, start_ms, end_ms, mode, out_dir, progress_cb)
            return final, info
        raw = find_output(out_dir, prefix="raw")
        if raw is None:
            raise YtError("Downloaded file (precise mode) not found.")
        final = out_dir / "out.mp4"
        progress_cb("encode", 0)
        _reencode(raw, start_ms, end_ms, final, progress_cb)
        return final, info

    # Fast: cut via local closed-Range proxy (also fast for deep cuts — M5
    # fix). Layered fallback kept for HTTP access failures; other errors (e.g.
    # video unavailable) are reported directly.
    try:
        final = _proxy_cut(watch_url, f_sel, start_ms, end_ms, out_dir, progress_cb)
    except YtError as e:
        if not _needs_full_fallback(str(e)):
            raise
        try:
            final = _prefix_fallback(watch_url, f_sel, start_ms, end_ms, mode, out_dir, progress_cb)
        except YtError:
            final = _full_fallback(watch_url, f_sel, start_ms, end_ms, mode, out_dir, progress_cb)
    return final, info


def _proxy_cut(
    watch_url: str, f_sel: str, start_ms: int, end_ms: int,
    out_dir: Path, progress_cb,
) -> Path:
    """Fast-mode cut via closed-Range proxy (with fresh-URL retry).

    YouTube streams are sometimes throttled so ffmpeg produces an empty part
    (moov without tracks, exit 0) — `_verify_part` catches it and we retry
    with a fresh signed URL (one-time URL; re-fetched via `yt-dlp -g`). The
    second attempt switches to the combined progressive format (`b`), served
    from a different path than separate DASH streams — often available when
    DASH streams are empty. A short pause between attempts lets throttling
    subside.
    Cut details live in `_proxy_cut_once`.
    """
    last_err: YtError | None = None
    for attempt in range(3):
        # Attempt 1 & 3: original selector (separate DASH, best quality).
        # Attempt 2: combined progressive format — different serve path, often
        # available even when DASH streams are empty.
        sel = _progressive_selector(f_sel) if attempt == 1 else f_sel
        try:
            return _proxy_cut_once(watch_url, sel, start_ms, end_ms, out_dir, progress_cb)
        except YtError as e:
            last_err = e
            msg = str(e)
            retryable = ("result has no " in msg and " stream" in msg) or ("empty result" in msg)
            if not retryable or attempt == 2:
                raise
            logger.debug(
                "proxy cut retry %d/2 (selector=%s) setelah: %s",
                attempt + 1, sel, msg,
            )
            time.sleep(_RETRY_BACKOFF_S)
            progress_cb("download", 0)
    raise last_err  # pragma: no cover — loop selalu raise/return


def _progressive_selector(f_sel: str) -> str:
    """Combined progressive version of a selector: `b` (video+audio one file).

    YouTube serves progressive formats from a different server path than
    separate DASH streams; when DASH is empty/throttled, `b` is often still
    available. Keeps the height cap if the original selector had one.
    """
    m = re.search(r"height<=(\d+)", f_sel)
    if m:
        return f"b[height<={m.group(1)}]"
    return "b"


def _proxy_cut_once(
    watch_url: str, f_sel: str, start_ms: int, end_ms: int,
    out_dir: Path, progress_cb,
) -> Path:
    """Fast-mode cut via closed-Range proxy.

    1. Get stream URLs (video + audio) via `yt-dlp -g`.
    2. Run a local proxy per stream that closes ffmpeg's open Range
       (googlevideo throttles open Range deep in the file → slow).
    3. Video: `-ss` input + `-c copy` without `-avoid_negative_ts make_zero` —
       default accurate_seek makes the start frame-accurate (±1 frame).
       Audio: `-ss` input (precise via m4a index).
    4. Mux the two streams.
    """
    proc = _run(
        [*BASE_ARGS, "-g", "--no-playlist", "--no-warnings", "-f", f_sel, watch_url],
        timeout=120,
    )
    if proc.returncode != 0:
        raise YtError(_friendly_error(proc.stderr))
    urls = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    if not urls:
        raise YtError("No matching stream to cut.")
    logger.debug("proxy cut: %d stream, start=%.3fs dur=%.3fs", len(urls), start_ms / 1000, (end_ms - start_ms) / 1000)

    start = start_ms / 1000
    duration = max(1.0, (end_ms - start_ms) / 1000)
    progress_flags = ["-nostats", "-progress", "pipe:1"]

    parts: list[Path] = [None] * len(urls)  # type: ignore[list-item]
    errors: list[YtError] = []
    progress_cb("download", 0)

    # Parallel progress, weighted by each stream's expected read size. Video is
    # the big/slow stream; without weighting, the small fast audio part would
    # pin the bar high while video still crawls ("stuck at 78%").
    stream_pct: list[float] = [0.0] * len(urls)
    stream_weight: list[float] = [0.0] * len(urls)

    def _report_progress() -> None:
        total_w = sum(stream_weight) or 1.0
        pct = sum(p * w for p, w in zip(stream_pct, stream_weight)) / total_w
        progress_cb("download", min(100.0, pct))

    def _cut_stream(i: int, u: str) -> Path:
        proxy = _RangeProxy(u)
        is_video = i == 0
        # Fallback total size from the `clen` param when Content-Range has
        # no total (for byte-based progress).
        clen = _url_param(u, "clen")
        dur = float(_url_param(u, "dur") or 0)
        if clen and clen.isdigit():
            c = int(clen)
            proxy.expected_total = c
            # Estimated fraction of the file ffmpeg actually reads (input-seek
            # jumps to the cut position — not from byte 0): clip + GOP margin
            # over the stream duration.
            if dur > 0:
                clip_s = (end_ms - start_ms) / 1000 + 10.0
                proxy.expected_frac = min(1.0, clip_s / dur)
                stream_weight[i] = c * proxy.expected_frac
            else:
                stream_weight[i] = c
        elif dur > 0:
            clip_s = (end_ms - start_ms) / 1000 + 10.0
            proxy.expected_frac = min(1.0, clip_s / dur)
        if not stream_weight[i]:
            # No size info → heuristic: video dominates a 10:1 share.
            stream_weight[i] = 10.0 if is_video else 1.0
        proxy.__enter__()
        # Extension follows the stream container: AAC can't go into .webm.
        mime = _url_param(u, "mime") or ""
        if is_video:
            ext = "mp4"
        elif "webm" in mime:
            ext = "webm"
        else:
            ext = "m4a"
        out = out_dir / f"sec{i}.{ext}"
        try:
            if is_video:
                cmd = [
                    "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
                    *progress_flags,
                    "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                    "-i", proxy.url,
                    "-map", "0:v:0", "-map", "0:a:0?",
                    # DON'T use -avoid_negative_ts make_zero / -copyts here:
                    # both disable accurate_seek → input-seek snaps to a
                    # keyframe (cut shifts up to 1 GOP). Without them, ffmpeg
                    # decodes-and-discards the keyframe→start segment, so the
                    # start stays frame-accurate even with -c copy.
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(out),
                ]
            else:
                # Audio m4a uses INPUT-seek: indexed moov (sidx) → precise &
                # fast seek (1-2 requests). Output-seek would read the WHOLE
                # file (~84MB for a 1.5h video) — that was the "stuck at 5%"
                # cause.
                cmd = [
                    "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
                    *progress_flags,
                    "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                    "-i", proxy.url,
                    "-map", "0:a:0", "-c", "copy",
                    str(out),
                ]

            def _stream_cb(pct: float) -> None:
                # byte_pct is the honest signal (on -c copy, out_time jumps to
                # the cut position from the first packet); fall back to out_time
                # only when the stream size is unknown.
                bp = proxy.byte_pct()
                if bp is not None:
                    pct = bp
                stream_pct[i] = min(100.0, pct)
                _report_progress()

            _run_with_byte_progress(cmd, duration, _stream_cb, proxy)
            # Stream work finished (ffmpeg exited 0) → mark it complete.
            stream_pct[i] = 100.0
            _report_progress()
            if not out.exists() or out.stat().st_size == 0:
                raise YtError(
                    f"Stream cut failed ({'video' if is_video else 'audio'}): empty result."
                )
            _verify_part(out, "video" if is_video else "audio")
            return out
        finally:
            proxy.__exit__(None, None, None)

    # Video + audio download in parallel: overlaps both streams' googlevideo
    # throttling/transfer time instead of idling one stream while the other
    # runs — halves the wall-time of the download phase.
    def _worker(i: int, u: str) -> None:
        try:
            parts[i] = _cut_stream(i, u)
        except YtError as e:
            errors.append(e)
        except Exception as e:  # thread crash → propagate as a clear error
            errors.append(YtError(f"Stream cut failed: {e}"))

    threads = [
        threading.Thread(target=_worker, args=(i, u), daemon=True)
        for i, u in enumerate(urls)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    parts = [p for p in parts if p is not None]

    if len(parts) == 2:
        merged = out_dir / "merged.mp4"
        cmd = [
            "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
            *progress_flags,
            "-i", str(parts[0]), "-i", str(parts[1]),
            "-map", "0:v:0", "-map", "1:a:0",
            # DON'T -avoid_negative_ts make_zero here: on -c copy mux it snaps
            # the video back to the first keyframe (±1 GOP) even when the input
            # is already frame-accurate (proven: 60→75 frames, I-frame).
            "-c", "copy",
            "-movflags", "+faststart",
            str(merged),
        ]
        # Merge reports progress too (98→100) so it doesn't look "stuck" at
        # the end of download — previously subprocess.run without a callback.
        try:
            _run_ffmpeg_progress(
                cmd, duration,
                lambda pct: progress_cb("download", 98 + pct * 0.02),
            )
        except YtError as e:
            raise YtError(f"Stream merge failed: {str(e).split(': ', 1)[-1]}") from e
        final = merged
    else:
        final = parts[0]

    progress_cb("download", 100)
    return final


def _verify_part(path: Path, kind: str) -> None:
    """Ensure the cut part has the expected stream (video/audio).

    ffmpeg can exit 0 even when the result is empty (moov without tracks) —
    e.g. when the YouTube stream is throttled and sends no media data. The
    st_size guard alone is not enough (a ~200-byte header passes); ffprobe
    verification turns a confusing merge failure into a clear message.
    """
    try:
        info = probe(str(path))
    except YtError as e:
        raise YtError(f"Stream cut failed ({kind}): {e}")
    ok = info["has_video"] if kind == "video" else info["has_audio"]
    if not ok:
        raise YtError(
            f"Stream cut failed ({kind}): result has no {kind} stream — "
            "the YouTube stream may be throttled/empty, try again later."
        )


def _run_ffmpeg_progress(cmd: list[str], duration: float, progress_cb) -> None:
    """Run ffmpeg while mapping progress from `-progress pipe:1`.

    ffmpeg writes key=value blocks to stdout; we read `out_time_ms` for
    progress. (No longer parses stderr `time=` — loglevel warning suppresses
    it, which is why progress used to freeze at 5%.)
    """
    logger.debug("ffmpeg %s", " ".join(str(a) for a in cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    assert proc.stdout is not None
    tail: list[str] = []
    for raw in proc.stdout:
        raw = raw.strip()
        if "=" in raw:
            key, val = raw.split("=", 1)
            if key == "out_time_ms":
                try:
                    out_ms = int(float(val))
                except ValueError:
                    out_ms = 0
                progress_cb(min(100.0, out_ms / 1000 / duration * 100))
            elif key == "progress" and val == "end":
                break
    err = proc.stderr.read() if proc.stderr else ""
    proc.wait(timeout=1800)
    if proc.returncode != 0:
        raise YtError(f"Stream cut failed: {err.strip()[:200]}")


def _run_with_byte_progress(cmd, duration, progress_cb, proxy) -> None:
    """Run ffmpeg + sample proxy bytes in a separate thread.

    ffmpeg `-progress` emits almost nothing during the input-seek decode phase
    (out_time stays still until output is written) — bytes served by the proxy
    reflect the real work at that moment. The sampler thread calls
    progress_cb with byte-based pct; `_run_ffmpeg_progress` calls it with
    out_time — combined at the caller's callback (max).
    """
    stop = threading.Event()

    def sampler():
        while not stop.wait(_BYTE_POLL_INTERVAL):
            bp = proxy.byte_pct()
            if bp is not None:
                progress_cb(bp)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    try:
        _run_ffmpeg_progress(cmd, duration, progress_cb)
    finally:
        stop.set()
        th.join(timeout=2)


def _format_selector(height: str | int, has_audio: bool) -> str:
    """yt-dlp format selector: video (+ audio if present).

    m4a audio (AAC, MP4 container) is preferred over webm/opus: YouTube webm
    DASH often lacks cues, so ffmpeg must read nearly the whole file for deep
    seeks (very slow). m4a has an indexed moov → precise, fast seek. Without
    audio, don't request an audio stream so videos with no sound don't fail.
    """
    if height == "best":
        return "bv*+ba[ext=m4a]/bv*+ba/b" if has_audio else "bv*/b"
    h = int(height)
    if has_audio:
        return (
            f"bv*[height<={h}]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/b[height<={h}]"
        )
    return f"bv*[height<={h}]/b[height<={h}]"


def _url_param(url: str, key: str) -> str | None:
    vals = parse_qs(urlparse(url).query).get(key)
    return vals[0] if vals else None


def _prefix_fallback(
    watch_url: str, f_sel: str, start_ms: int, end_ms: int,
    mode: str, out_dir: Path, progress_cb,
) -> Path:
    """Light fallback: download [0, end_byte] per stream via requests, then
    mux + cut locally. Saves bandwidth vs. a full download."""
    proc = _run(
        [*BASE_ARGS, "-g", "--no-playlist", "--no-warnings", "-f", f_sel, watch_url],
        timeout=120,
    )
    if proc.returncode != 0:
        raise YtError(_friendly_error(proc.stderr))
    streams = []
    for line in proc.stdout.splitlines():
        u = line.strip()
        if not u:
            continue
        clen = _url_param(u, "clen")
        dur = _url_param(u, "dur")
        streams.append({
            "url": u,
            "filesize": int(clen) if clen and clen.isdigit() else None,
            "duration": float(dur) if dur else None,
        })
    if not streams:
        raise YtError("No matching stream for fallback.")

    end_s = end_ms / 1000
    margin_s = 15.0
    parts: list[Path] = []
    for i, st in enumerate(streams):
        if not (st["filesize"] and st["duration"]):
            raise YtError("Stream size info unavailable — using full fallback.")
        end_byte = min(
            st["filesize"] - 1,
            int((end_s + margin_s) / st["duration"] * st["filesize"]),
        )
        p = out_dir / f"prefix{i}.bin"
        progress_cb("download", 0)
        _download_prefix(st["url"], end_byte, p, lambda pct: progress_cb("download", pct))
        parts.append(p)

    if len(parts) == 2:
        merged = out_dir / "merged.mp4"
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", str(parts[0]), "-i", str(parts[1]),
            "-map", "0:v:0", "-map", "1:a:0",
            # Without make_zero: -c copy mux keeps the frame-accurate start
            # (see the merge comment in _proxy_cut).
            "-c", "copy",
            str(merged),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise YtError(f"Stream merge (fallback) failed: {proc.stderr.strip()[:200]}")
        src = merged
    else:
        src = parts[0]

    try:
        final = _trim_local(src, start_ms, end_ms, mode, out_dir, progress_cb)
    finally:
        for p in parts:
            p.unlink(missing_ok=True)
        if len(parts) == 2:
            src.unlink(missing_ok=True)
    return final


def _download_prefix(url: str, end_byte: int, out_path: Path, progress_cb) -> None:
    """Download [0, end_byte] with a Range header (always includes the header file)."""
    headers = {"Range": f"bytes=0-{end_byte}"}
    try:
        resp = crequests.get(url, headers=headers, impersonate="chrome", stream=True, timeout=30)
    except Exception as e:
        raise YtError(f"Failed to download stream: {e}")
    if resp.status_code not in (200, 206):
        raise YtError(f"Failed to download stream (HTTP {resp.status_code}).")
    total = int(resp.headers.get("Content-Length") or 0)
    done = 0
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                progress_cb(min(100.0, done / total * 100))


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


def _full_fallback(
    watch_url: str, f_sel: str, start_ms: int, end_ms: int,
    mode: str, out_dir: Path, progress_cb,
) -> Path:
    """Last-resort fallback: full download via the native downloader + local cut."""
    progress_cb("download", 0)
    full_tmpl = str(out_dir / "full.%(ext)s")
    _run_streaming(
        [
            *BASE_ARGS, "--no-playlist", "--no-warnings", "--newline",
            "--progress-template", "download:%(progress._percent_str)s",
            "-f", f_sel,
            "--merge-output-format", "mp4",
            "-o", full_tmpl,
            watch_url,
        ],
        lambda line: _maybe_progress(line, progress_cb, "download"),
    )
    full = find_output(out_dir, prefix="full")
    if full is None:
        raise YtError("Full download (fallback) failed.")
    try:
        return _trim_local(full, start_ms, end_ms, mode, out_dir, progress_cb)
    finally:
        full.unlink(missing_ok=True)


def _trim_local(
    src: Path, start_ms: int, end_ms: int, mode: str, out_dir: Path, progress_cb
) -> Path:
    """Cut a local file (result of a full-download fallback)."""
    final = out_dir / "out.mp4"
    start = f"{start_ms / 1000:.3f}"
    duration = max(1, (end_ms - start_ms) / 1000)
    if mode == "accurate":
        progress_cb("encode", 0)
        _reencode(src, start_ms, end_ms, final, progress_cb)
        return final

    # Without -avoid_negative_ts make_zero: accurate_seek makes the input-seek
    # frame-accurate even with -c copy (see _proxy_cut).
    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-ss", start, "-t", f"{duration:.3f}", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy",
        "-movflags", "+faststart",
        str(final),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise YtError(f"Local cut failed: {proc.stderr.strip()[:200]}")
    return final


_FALLBACK_MARKERS = ("403", "Server returned", "HTTP error", "ffmpeg exited", "Error opening input")


def _needs_full_fallback(msg: str) -> bool:
    """True if the section-download failure stems from HTTP stream access."""
    return any(m in msg for m in _FALLBACK_MARKERS)


def _maybe_progress(line: str, progress_cb, stage: str) -> None:
    pct = _parse_percent(line)
    if pct is not None:
        progress_cb(stage, pct)


# ---------------------------------------------------------------------------
# Verification (ffprobe)
# ---------------------------------------------------------------------------

def probe(path: str) -> dict:
    """Probe result file: video/audio streams, duration, per-stream start time."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,start_time:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise YtError(f"File verification failed: {proc.stderr.strip()[:200]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise YtError("Failed to read ffprobe output.")

    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "has_video": bool(video),
        "has_audio": bool(audio),
        "duration": float(data.get("format", {}).get("duration") or 0),
        "video_start": _first_start_time(video),
        "audio_start": _first_start_time(audio),
        "video_codec": _first_codec_name(video),
    }


def _first_codec_name(streams: list[dict]) -> str | None:
    for s in streams:
        c = s.get("codec_name")
        if c:
            return str(c)
    return None


def _first_start_time(streams: list[dict]) -> float | None:
    for s in streams:
        t = s.get("start_time")
        if t is not None:
            try:
                return float(t)
            except (TypeError, ValueError):
                continue
    return None
