# Tests for the ytdlp_service.py pipeline (subprocess/requests mocked, no network).
import json
from pathlib import Path

import pytest

from tubesnip import ytdlp_service as ys


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeStream:
    """Fake stderr: iterable (line by line) and fully readable (.read())."""

    def __init__(self, lines):
        self._lines = list(lines)
        self._i = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._i >= len(self._lines):
            raise StopIteration
        v = self._lines[self._i]
        self._i += 1
        return v

    def read(self):
        return "\n".join(self._lines)


def fake_popen(stderr_lines, returncode=0, stdout_lines=None):
    """Factory for a fake Popen class used by _run_streaming / _reencode.

    _run_ffmpeg_progress reads progress from STDOUT (`-progress pipe:1`), so
    stdout_lines hold key=value lines; stderr_lines hold error messages
    (iterated by _run_streaming, read whole by _run_ffmpeg_progress).
    """
    lines = list(stderr_lines)
    out = list(stdout_lines or [])

    class P:
        def __init__(self, cmd=None, **kw):
            self.cmd = cmd or []
            self.returncode = returncode
            self.stdout = iter(out)
            self.stderr = _FakeStream(lines)

        def wait(self, timeout=None):
            return self.returncode

    return P


def routing_subprocess(ytdlp_stdout="", ytdlp_rc=0, ffmpeg_rc=0, stderr="", popen_cls=None):
    """Fake subprocess: routes yt-dlp/ffprobe vs ffmpeg commands for `run`."""
    class S:
        PIPE = -1
        DEVNULL = -3

        def run(self, args, **kw):
            if args and args[0] in ("yt-dlp", "ffprobe"):
                return _Proc(ytdlp_rc, ytdlp_stdout, stderr)
            return _Proc(ffmpeg_rc, "", stderr)

        def Popen(self, cmd, **kw):
            if popen_cls is None:
                raise AssertionError("Popen not expected in this test")
            return popen_cls(cmd, **kw)

    return S()


def _info(**over):
    d = {
        "video_id": "vid123",
        "title": "Test Video",
        "duration_ms": 19000,
        "is_live": False,
        "has_audio": True,
        "resolutions": [{"height": 720, "fps": 30, "codec": "VP9", "ext": "webm"}],
    }
    d.update(over)
    return d


class _FakeResp:
    def __init__(self, status, content=b"", content_length=None):
        self.status_code = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self._chunks = [content[i : i + 1024] for i in range(0, len(content), 1024)] or [b""]

    def iter_content(self, size):
        yield from self._chunks


class _FakeRequests:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def get(self, url, **kw):
        if self._exc:
            raise self._exc
        return self._resp


class TestGetVideoInfo:
    def test_success(self, monkeypatch):
        info = {
            "id": "vid123", "title": "T", "duration": 12.5, "is_live": False,
            "formats": [
                {"vcodec": "vp9", "height": 720, "fps": 30, "ext": "webm", "tbr": 100},
                {"vcodec": "none", "acodec": "opus", "height": None},
            ],
        }
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout=json.dumps(info)))
        res = ys.get_video_info("https://youtu.be/vid123")
        assert res["video_id"] == "vid123"
        assert res["duration_ms"] == 12500
        assert res["has_audio"] is True
        assert res["resolutions"] == [
            {"height": 720, "fps": 30, "codec": "VP9", "ext": "webm", "bitrate": 100}
        ]

    def test_invalid_url(self):
        with pytest.raises(ys.YtError, match="Invalid YouTube URL"):
            ys.get_video_info("https://example.com/x")

    def test_ytdlp_error(self, monkeypatch):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_rc=1, stderr="ERROR: Private video"))
        with pytest.raises(ys.YtError, match="Private video"):
            ys.get_video_info("https://youtu.be/vid123")

    def test_broken_json(self, monkeypatch):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout="not json"))
        with pytest.raises(ys.YtError, match="Failed to parse"):
            ys.get_video_info("https://youtu.be/vid123")

    def test_live(self, monkeypatch):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout=json.dumps({"is_live": True, "formats": []})))
        with pytest.raises(ys.YtError, match="Live streams"):
            ys.get_video_info("https://youtu.be/vid123")

    def test_without_formats(self, monkeypatch):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout=json.dumps({"id": "v", "duration": 10, "is_live": False})))
        res = ys.get_video_info("https://youtu.be/vid123")
        assert res["has_audio"] is False
        assert res["resolutions"] == []


class TestRunStreaming:
    def test_success(self, monkeypatch):
        lines = ["[download] Destination: /tmp/x.mp4", "42.3%", "100%"]
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(popen_cls=fake_popen(lines)))
        seen = []
        stderr = ys._run_streaming(["yt-dlp", "-x"], seen.append)
        assert stderr == "\n".join(lines)
        assert seen == lines

    def test_nonzero_rc(self, monkeypatch):
        monkeypatch.setattr(
            ys, "subprocess",
            routing_subprocess(popen_cls=fake_popen(["ERROR: Server returned 403 Forbidden"], returncode=1)),
        )
        with pytest.raises(ys.YtError, match="Server returned"):
            ys._run_streaming(["yt-dlp"], lambda l: None)

    def test_empty_lines_skipped(self, monkeypatch):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(popen_cls=fake_popen(["", "   ", "10%"])))
        seen = []
        ys._run_streaming(["yt-dlp"], seen.append)
        assert seen == ["10%"]


class TestReencode:
    def test_success_and_progress(self, monkeypatch, tmp_path):
        # _reencode uses -progress pipe:1 → progress on STDOUT (key=value).
        out = [
            "out_time_ms=2500",
            "progress=continue",
            "out_time_ms=6250",
            "progress=end",
        ]
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(popen_cls=fake_popen([], stdout_lines=out)))
        cbs = []
        ys._reencode(tmp_path / "in.mp4", 2000, 6000, tmp_path / "out.mp4", lambda st, p: cbs.append((st, p)))
        assert cbs[0] == ("encode", 62.5)
        assert cbs[-1] == ("encode", 100.0)

    def test_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(popen_cls=fake_popen(["Error"], returncode=1)))
        with pytest.raises(ys.YtError, match="re-encode failed"):
            ys._reencode(tmp_path / "in.mp4", 0, 1000, tmp_path / "out.mp4", lambda st, p: None)


class TestProbe:
    def test_success(self, monkeypatch):
        data = {
            "streams": [
                {"codec_type": "video", "start_time": "0.5"},
                {"codec_type": "audio", "start_time": "0.5"},
            ],
            "format": {"duration": "4.0"},
        }
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout=json.dumps(data)))
        p = ys.probe("/tmp/x.mp4")
        assert p == {"has_video": True, "has_audio": True, "duration": 4.0, "video_start": 0.5, "audio_start": 0.5}

    def test_ffprobe_error(self, monkeypatch):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_rc=1, stderr="No such file or directory"))
        with pytest.raises(ys.YtError, match="File verification failed"):
            ys.probe("/tmp/x.mp4")

    def test_broken_json(self, monkeypatch):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout="x"))
        with pytest.raises(ys.YtError, match="Failed to read"):
            ys.probe("/tmp/x.mp4")


class TestFirstStartTime:
    def test_various(self):
        assert ys._first_start_time([{"start_time": "1.5"}]) == 1.5
        assert ys._first_start_time([{"start_time": "abc"}]) is None
        assert ys._first_start_time([{"start_time": None}, {"start_time": "2.0"}]) == 2.0
        assert ys._first_start_time([]) is None


class TestDownloadPrefix:
    def test_success_206_with_progress(self, monkeypatch, tmp_path):
        content = b"abcd" * 1000  # 4000 bytes
        monkeypatch.setattr(ys, "crequests", _FakeRequests(_FakeResp(206, content, 4000)))
        out = tmp_path / "p.bin"
        cbs = []
        ys._download_prefix("http://x", 3999, out, cbs.append)
        assert out.read_bytes() == content
        assert cbs[-1] == 100.0

    def test_status_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "crequests", _FakeRequests(_FakeResp(403)))
        with pytest.raises(ys.YtError, match="HTTP 403"):
            ys._download_prefix("http://x", 100, tmp_path / "p.bin", lambda p: None)

    def test_request_exception(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "crequests", _FakeRequests(exc=ConnectionError("connection dropped")))
        with pytest.raises(ys.YtError, match="connection dropped"):
            ys._download_prefix("http://x", 100, tmp_path / "p.bin", lambda p: None)

    def test_without_content_length(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "crequests", _FakeRequests(_FakeResp(206, b"abc")))
        out = tmp_path / "p.bin"
        ys._download_prefix("http://x", 100, out, lambda p: None)
        assert out.read_bytes() == b"abc"


class TestPrefixFallback:
    def test_success_two_streams_merge(self, monkeypatch, tmp_path):
        u1, u2 = "http://x/v.mp4?clen=4000&dur=10.0", "http://x/a.m4a?clen=2000&dur=10.0"
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout=f"{u1}\n{u2}\n"))
        monkeypatch.setattr(
            ys, "_download_prefix",
            lambda url, end_byte, out_path, cb: out_path.write_bytes(b"x" * 100),
        )
        final = tmp_path / "out.mp4"
        final.write_text("final")
        monkeypatch.setattr(ys, "_trim_local", lambda *a, **k: final)
        result = ys._prefix_fallback("https://youtu.be/v", "bv*+ba/b", 2000, 6000, "fast", tmp_path, lambda st, p: None)
        assert result == final
        assert not list(tmp_path.glob("prefix*.bin"))  # cleaned up
        assert not (tmp_path / "merged.mp4").exists()

    def test_success_one_stream(self, monkeypatch, tmp_path):
        u1 = "http://x/v.mp4?clen=4000&dur=10.0"
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout=u1 + "\n"))
        monkeypatch.setattr(ys, "_download_prefix", lambda url, end_byte, out_path, cb: out_path.write_bytes(b"x"))
        final = tmp_path / "out.mp4"
        monkeypatch.setattr(ys, "_trim_local", lambda *a, **k: final)
        result = ys._prefix_fallback("https://youtu.be/v", "bv*/b", 0, 1000, "fast", tmp_path, lambda st, p: None)
        assert result == final

    def test_ytdlp_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_rc=1))
        with pytest.raises(ys.YtError):
            ys._prefix_fallback("https://youtu.be/v", "bv*+ba/b", 0, 1000, "fast", tmp_path, lambda st, p: None)

    def test_no_streams(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout="\n\n"))
        with pytest.raises(ys.YtError, match="No matching stream"):
            ys._prefix_fallback("https://youtu.be/v", "bv*+ba/b", 0, 1000, "fast", tmp_path, lambda st, p: None)

    def test_missing_size_info(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout="http://x/v.mp4\n"))
        with pytest.raises(ys.YtError, match="Stream size info"):
            ys._prefix_fallback("https://youtu.be/v", "bv*/b", 0, 1000, "fast", tmp_path, lambda st, p: None)

    def test_merge_fails(self, monkeypatch, tmp_path):
        u1, u2 = "http://x/v.mp4?clen=4000&dur=10.0", "http://x/a.m4a?clen=2000&dur=10.0"
        monkeypatch.setattr(ys, "subprocess", routing_subprocess(ytdlp_stdout=f"{u1}\n{u2}\n", ffmpeg_rc=1))
        monkeypatch.setattr(ys, "_download_prefix", lambda url, end_byte, out_path, cb: out_path.write_bytes(b"x"))
        with pytest.raises(ys.YtError, match="Stream merge"):
            ys._prefix_fallback("https://youtu.be/v", "bv*+ba/b", 0, 1000, "fast", tmp_path, lambda st, p: None)


class TestFullFallback:
    def test_success_and_cleanup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "_run_streaming", lambda args, on_line, timeout=3600: "")
        full = tmp_path / "full.mp4"
        full.write_text("full")
        final = tmp_path / "out.mp4"
        monkeypatch.setattr(ys, "_trim_local", lambda *a, **k: final)
        result = ys._full_fallback("https://youtu.be/v", "bv*+ba/b", 0, 1000, "fast", tmp_path, lambda st, p: None)
        assert result == final
        assert not full.exists()

    def test_missing_output(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "_run_streaming", lambda args, on_line, timeout=3600: "")
        with pytest.raises(ys.YtError, match="Full download"):
            ys._full_fallback("https://youtu.be/v", "bv*+ba/b", 0, 1000, "fast", tmp_path, lambda st, p: None)

    def test_run_streaming_error(self, monkeypatch, tmp_path):
        def boom(args, on_line, timeout=3600):
            raise ys.YtError("Server returned 403")

        monkeypatch.setattr(ys, "_run_streaming", boom)
        with pytest.raises(ys.YtError, match="403"):
            ys._full_fallback("https://youtu.be/v", "bv*+ba/b", 0, 1000, "fast", tmp_path, lambda st, p: None)


class TestTrimLocal:
    def test_fast_success(self, monkeypatch, tmp_path):
        sub = _MakeFilesSubprocess()
        monkeypatch.setattr(ys, "subprocess", sub)
        result = ys._trim_local(tmp_path / "in.mp4", 2000, 6000, "fast", tmp_path, lambda st, p: None)
        assert result == tmp_path / "out.mp4"
        # One input-seek pass; NO -avoid_negative_ts make_zero / -copyts
        # (both disable accurate_seek → snap to keyframe).
        assert len(sub.runs) == 1
        run = sub.runs[0]
        assert "-ss" in run and run.index("-ss") < run.index("-i")
        assert "-avoid_negative_ts" not in run and "-copyts" not in run

    def test_fast_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "subprocess", _MakeFilesSubprocess(rc=1))
        with pytest.raises(ys.YtError, match="Local cut failed"):
            ys._trim_local(tmp_path / "in.mp4", 0, 1000, "fast", tmp_path, lambda st, p: None)

    def test_accurate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "_reencode", lambda src, s, e, dst, cb: dst.write_text("x"))
        result = ys._trim_local(tmp_path / "in.mp4", 0, 1000, "accurate", tmp_path, lambda st, p: None)
        assert result.exists()


class TestCutSection:
    def test_invalid_url(self, tmp_path):
        with pytest.raises(ys.YtError, match="Invalid YouTube URL"):
            ys.cut_section("https://example.com/x", 0, 1000, "best", "fast", tmp_path, lambda st, p: None)

    def test_success_fast(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info())
        final = tmp_path / "out.mp4"
        final.write_text("x")
        recorded = {}

        def fake_proxy(watch_url, f_sel, start_ms, end_ms, out_dir, progress_cb):
            recorded.update(watch_url=watch_url, f_sel=f_sel, start_ms=start_ms, end_ms=end_ms)
            return final

        monkeypatch.setattr(ys, "_proxy_cut", fake_proxy)
        stages = []
        result = ys.cut_section("https://youtu.be/vid123", 2000, 6000, "best", "fast", tmp_path, lambda st, p: stages.append((st, p)))
        assert result[0] == final
        assert result[1]["duration_ms"] == 19000
        assert stages[0] == ("extract", None)
        assert recorded["start_ms"] == 2000 and recorded["end_ms"] == 6000
        assert recorded["f_sel"] == "bv*+ba[ext=m4a]/bv*+ba/b"

    def test_success_accurate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info())
        monkeypatch.setattr(ys, "_run_streaming", lambda args, on_line, timeout=3600: "")
        (tmp_path / "raw.mp4").write_text("raw")
        reenc = {}

        def fake_reencode(src, s, e, dst, cb):
            reenc.update(src=src, s=s, e=e)
            dst.write_text("x")

        monkeypatch.setattr(ys, "_reencode", fake_reencode)
        stages = []
        result = ys.cut_section("https://youtu.be/vid123", 2000, 6000, "1080", "accurate", tmp_path, lambda st, p: stages.append((st, p)))
        assert result[0] == tmp_path / "out.mp4"
        assert reenc["s"] == 2000 and reenc["e"] == 6000
        assert any(st == "encode" for st, _ in stages)
        assert ("download", 0) in stages

    def test_negative_time(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info())
        with pytest.raises(ys.YtError, match="Times must not be negative"):
            ys.cut_section("https://youtu.be/vid123", -1, 1000, "best", "fast", tmp_path, lambda st, p: None)

    def test_no_duration(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info(duration_ms=0))
        with pytest.raises(ys.YtError, match="no duration"):
            ys.cut_section("https://youtu.be/vid123", 0, 1000, "best", "fast", tmp_path, lambda st, p: None)

    def test_end_exceeds_duration(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info(duration_ms=19000))
        with pytest.raises(ys.YtError, match=r"End exceeds video duration \(0:00:19\)"):
            ys.cut_section("https://youtu.be/vid123", 0, 20000, "best", "fast", tmp_path, lambda st, p: None)

    def test_start_ge_end(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info())
        with pytest.raises(ys.YtError, match="Start must be before"):
            ys.cut_section("https://youtu.be/vid123", 5000, 5000, "best", "fast", tmp_path, lambda st, p: None)

    def test_fast_proxy_nonhttp_error(self, monkeypatch, tmp_path):
        # A non-HTTP proxy error (e.g. video unavailable) is reported directly,
        # without triggering the full-download fallback.
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info())

        def boom(*a, **k):
            raise ys.YtError("Video unavailable (private / removed / blocked).")

        monkeypatch.setattr(ys, "_proxy_cut", boom)
        with pytest.raises(ys.YtError, match="unavailable"):
            ys.cut_section("https://youtu.be/vid123", 0, 1000, "best", "fast", tmp_path, lambda st, p: None)

    def test_accurate_raw_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info())
        monkeypatch.setattr(ys, "_run_streaming", lambda args, on_line, timeout=3600: "")
        with pytest.raises(ys.YtError, match="precise mode"):
            ys.cut_section("https://youtu.be/vid123", 0, 1000, "best", "accurate", tmp_path, lambda st, p: None)

    def test_fallback_triggered(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info())

        def boom(*a, **k):
            raise ys.YtError("Stream cut failed: HTTP error 403")

        monkeypatch.setattr(ys, "_proxy_cut", boom)
        final = tmp_path / "out.mp4"
        final.write_text("x")
        monkeypatch.setattr(ys, "_prefix_fallback", lambda *a, **k: final)
        result = ys.cut_section("https://youtu.be/vid123", 0, 1000, "best", "fast", tmp_path, lambda st, p: None)
        assert result[0] == final

    def test_both_fallbacks_fail(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ys, "get_video_info", lambda url: _info())

        def boom(*a, **k):
            raise ys.YtError("Server returned 403 Forbidden")

        def prefix_boom(*a, **k):
            raise ys.YtError("prefix failed")

        monkeypatch.setattr(ys, "_proxy_cut", boom)
        monkeypatch.setattr(ys, "_prefix_fallback", prefix_boom)
        final = tmp_path / "out.mp4"
        final.write_text("x")
        monkeypatch.setattr(ys, "_full_fallback", lambda *a, **k: final)
        result = ys.cut_section("https://youtu.be/vid123", 0, 1000, "best", "fast", tmp_path, lambda st, p: None)
        assert result[0] == final


class _MakeFilesSubprocess:
    """Fake subprocess for _proxy_cut: running ffmpeg creates the output file."""
    PIPE = -1
    DEVNULL = -3

    def __init__(self, rc=0, ytdlp_stdout=""):
        self.rc = rc
        self.ytdlp_stdout = ytdlp_stdout
        self.runs: list[list] = []

    def run(self, args, **kw):
        self.runs.append(args)
        if args and args[0] == "yt-dlp":
            return _Proc(self.rc, self.ytdlp_stdout, "" if self.rc == 0 else "ERROR: Server returned 403")
        if args and args[0] == "ffprobe":
            # _proxy_cut verifies each part via probe() → valid ffprobe JSON.
            return _Proc(
                0,
                '{"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], '
                '"format": {"duration": "10"}}',
                "",
            )
        if args and args[0] == "ffmpeg":
            out = args[-1]
            from pathlib import Path
            Path(out).write_text("x")
            return _Proc(self.rc)
        return _Proc(self.rc)

    def Popen(self, cmd, **kw):
        raise AssertionError("Popen not expected")


class _FakeRangeProxy:
    instances: list = []

    def __init__(self, target_url):
        self.target = target_url
        self.url = "http://127.0.0.1:1/s"
        self.exited = False
        self.expected_total = None
        self.expected_frac = None
        _FakeRangeProxy.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.exited = True

    def byte_pct(self):
        return None


class TestProxyCut:
    def test_success_two_streams(self, monkeypatch, tmp_path):
        sub = _MakeFilesSubprocess(
            ytdlp_stdout="http://v/url?mime=video%2Fmp4\nhttp://a/url?mime=audio%2Fmp4\n"
        )
        monkeypatch.setattr(ys, "subprocess", sub)

        recorded_cmds: dict = {}

        def fake_progress(cmd, dur, cb):
            out = str(cmd[-1])
            if "sec0" in out:
                recorded_cmds["video"] = cmd
            elif "sec1" in out:
                recorded_cmds["audio"] = cmd
            Path(cmd[-1]).write_text("x")

        monkeypatch.setattr(ys, "_run_ffmpeg_progress", fake_progress)
        _FakeRangeProxy.instances = []
        monkeypatch.setattr(ys, "_RangeProxy", _FakeRangeProxy)
        stages = []
        final = ys._proxy_cut(
            "https://youtu.be/vid123", "bv*+ba/b", 2000, 6000, tmp_path,
            lambda st, p: stages.append((st, p)),
        )
        assert final == tmp_path / "merged.mp4"
        assert final.exists()
        assert len(_FakeRangeProxy.instances) == 2
        assert all(p.exited for p in _FakeRangeProxy.instances)
        # Video & m4a audio both use input-seek (-ss before -i): m4a has an
        # indexed moov → precise & fast seek (not output-seek, which reads the
        # whole file).
        vrun = recorded_cmds["video"]
        arun = recorded_cmds["audio"]
        assert vrun.index("-ss") < vrun.index("-i")
        assert arun.index("-ss") < arun.index("-i")
        # Both use -progress pipe:1 (progress flows even with loglevel warning).
        assert "-progress" in vrun and "pipe:1" in vrun
        assert "-progress" in arun and "pipe:1" in arun
        # NO -avoid_negative_ts make_zero / -copyts on the video pass:
        # both disable accurate_seek → start snaps to keyframe.
        assert "-avoid_negative_ts" not in vrun and "-copyts" not in vrun
        # No extra trim pass (accurate_seek is already frame-accurate).
        assert not [r for r in sub.runs if "video_trim.mp4" in str(r[-1])]

    def test_success_one_stream(self, monkeypatch, tmp_path):
        sub = _MakeFilesSubprocess(ytdlp_stdout="http://v/url\n")
        monkeypatch.setattr(ys, "subprocess", sub)

        def fake_progress(cmd, dur, cb):
            Path(cmd[-1]).write_text("x")

        monkeypatch.setattr(ys, "_run_ffmpeg_progress", fake_progress)
        final = ys._proxy_cut(
            "https://youtu.be/vid123", "bv*/b", 0, 1000, tmp_path, lambda st, p: None,
        )
        assert final == tmp_path / "sec0.mp4"
        assert final.exists()

    def test_g_fails(self, monkeypatch, tmp_path):
        sub = _MakeFilesSubprocess(rc=1)
        monkeypatch.setattr(ys, "subprocess", sub)
        with pytest.raises(ys.YtError, match="403"):
            ys._proxy_cut("https://youtu.be/vid123", "bv*+ba/b", 0, 1000, tmp_path, lambda st, p: None)

    def test_no_streams(self, monkeypatch, tmp_path):
        sub = _MakeFilesSubprocess(ytdlp_stdout="")
        monkeypatch.setattr(ys, "subprocess", sub)
        with pytest.raises(ys.YtError, match="No matching stream"):
            ys._proxy_cut("https://youtu.be/vid123", "bv*+ba/b", 0, 1000, tmp_path, lambda st, p: None)

    def test_audio_fails(self, monkeypatch, tmp_path):
        sub = _MakeFilesSubprocess(
            ytdlp_stdout="http://v/url?mime=video%2Fmp4\nhttp://a/url?mime=audio%2Fmp4\n"
        )
        monkeypatch.setattr(ys, "subprocess", sub)

        def fake_progress(cmd, dur, cb):
            if any("sec1.m4a" in str(x) for x in cmd):
                raise ys.YtError("audio boom")
            Path(cmd[-1]).write_text("x")

        monkeypatch.setattr(ys, "_run_ffmpeg_progress", fake_progress)
        with pytest.raises(ys.YtError, match="audio boom"):
            ys._proxy_cut("https://youtu.be/vid123", "bv*+ba/b", 0, 1000, tmp_path, lambda st, p: None)

    def test_merge_fails(self, monkeypatch, tmp_path):
        class Sub(_MakeFilesSubprocess):
            def run(self, args, **kw):
                self.runs.append(args)
                if args and args[0] == "yt-dlp":
                    return _Proc(0, "http://v/url\nhttp://a/url\n")
                if args and args[0] == "ffprobe":
                    return _Proc(
                        0,
                        '{"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], '
                        '"format": {"duration": "10"}}',
                        "",
                    )
                Path(args[-1]).write_text("x")
                return _Proc(0)

        monkeypatch.setattr(ys, "subprocess", Sub())

        def fake_progress(cmd, dur, cb):
            # Merge fails: _run_ffmpeg_progress throws YtError → wrapped with
            # the "Stream merge failed" message.
            if any("merged.mp4" in str(x) for x in cmd):
                raise ys.YtError("Stream cut failed: merge boom")
            Path(cmd[-1]).write_text("x")

        monkeypatch.setattr(ys, "_run_ffmpeg_progress", fake_progress)
        with pytest.raises(ys.YtError, match="Stream merge failed: merge boom"):
            ys._proxy_cut("https://youtu.be/vid123", "bv*+ba/b", 0, 1000, tmp_path, lambda st, p: None)

    def test_empty_video_part_verified(self, monkeypatch, tmp_path):
        """ffmpeg exits 0 but the result has no video stream → clear error, not merge."""
        class Sub(_MakeFilesSubprocess):
            def run(self, args, **kw):
                self.runs.append(args)
                if args and args[0] == "yt-dlp":
                    return _Proc(0, "http://v/url\nhttp://a/url\n")
                if args and args[0] == "ffprobe":
                    return _Proc(0, '{"streams": [{"codec_type": "audio"}], "format": {"duration": "10"}}', "")
                if args and args[0] == "ffmpeg":
                    Path(args[-1]).write_text("x")
                    return _Proc(0)
                return _Proc(0)

        monkeypatch.setattr(ys, "subprocess", Sub())
        monkeypatch.setattr(
            ys, "_run_ffmpeg_progress", lambda cmd, dur, cb: Path(cmd[-1]).write_text("x")
        )
        monkeypatch.setattr(ys.time, "sleep", lambda _: None)  # retry backoff
        with pytest.raises(ys.YtError, match="result has no video stream"):
            ys._proxy_cut("https://youtu.be/vid123", "bv*+ba/b", 0, 1000, tmp_path, lambda st, p: None)

    def test_empty_audio_falls_back_to_progressive(self, monkeypatch, tmp_path):
        """Empty DASH audio → attempt 2 switches to progressive format `b` → success."""
        calls = {"probe": 0}

        def _ytdlp_out(args):
            if "-f" in args and args[args.index("-f") + 1] == "b":
                return "http://v/url\n"
            return "http://v/url\nhttp://a/url\n"

        class Sub(_MakeFilesSubprocess):
            def run(self, args, **kw):
                self.runs.append(args)
                if args and args[0] == "yt-dlp":
                    return _Proc(0, _ytdlp_out(args))
                if args and args[0] == "ffprobe":
                    calls["probe"] += 1
                    # Attempt 1: video ok (probe 1), audio empty (probe 2).
                    # Attempt 2 (`b`): video ok (probe 3) — audio inside the file.
                    return _Proc(
                        0, '{"streams": [{"codec_type": "video"}], "format": {"duration": "10"}}', ""
                    )
                if args and args[0] == "ffmpeg":
                    Path(args[-1]).write_text("x")
                    return _Proc(0)
                return _Proc(0)

        sub = Sub()
        monkeypatch.setattr(ys, "subprocess", sub)
        monkeypatch.setattr(
            ys, "_run_ffmpeg_progress", lambda cmd, dur, cb: Path(cmd[-1]).write_text("x")
        )
        monkeypatch.setattr(ys.time, "sleep", lambda _: None)  # retry backoff
        _FakeRangeProxy.instances = []
        monkeypatch.setattr(ys, "_RangeProxy", _FakeRangeProxy)
        final = ys._proxy_cut(
            "https://youtu.be/vid123", "bv*+ba/b", 0, 1000, tmp_path, lambda st, p: None
        )
        assert final == tmp_path / "sec0.mp4"
        assert final.exists()
        assert calls["probe"] == 3
        b_runs = [r for r in sub.runs if "-f" in r and r[r.index("-f") + 1] == "b"]
        assert b_runs, "progressive fallback `b` never tried when audio was empty"

    def test_retry_after_empty_stream(self, monkeypatch, tmp_path):
        """Empty part (throttle) → retry with a fresh URL; second attempt succeeds."""
        calls = {"probe": 0}

        def _ytdlp_out(args):
            # Original selector (separate DASH) → 2 streams; progressive `b` → 1.
            if "-f" in args and args[args.index("-f") + 1] == "b":
                return "http://v/url\n"
            return "http://v/url\nhttp://a/url\n"

        class Sub(_MakeFilesSubprocess):
            def run(self, args, **kw):
                self.runs.append(args)
                if args and args[0] == "yt-dlp":
                    return _Proc(0, _ytdlp_out(args))
                if args and args[0] == "ffprobe":
                    calls["probe"] += 1
                    if calls["probe"] == 1:  # video part, first attempt: empty
                        return _Proc(
                            0, '{"streams": [{"codec_type": "audio"}], "format": {"duration": "10"}}', ""
                        )
                    return _Proc(
                        0,
                        '{"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], '
                        '"format": {"duration": "10"}}',
                        "",
                    )
                if args and args[0] == "ffmpeg":
                    Path(args[-1]).write_text("x")
                    return _Proc(0)
                return _Proc(0)

        sub = Sub()
        monkeypatch.setattr(ys, "subprocess", sub)
        monkeypatch.setattr(
            ys, "_run_ffmpeg_progress", lambda cmd, dur, cb: Path(cmd[-1]).write_text("x")
        )
        monkeypatch.setattr(ys.time, "sleep", lambda _: None)  # retry backoff
        _FakeRangeProxy.instances = []
        monkeypatch.setattr(ys, "_RangeProxy", _FakeRangeProxy)
        final = ys._proxy_cut(
            "https://youtu.be/vid123", "bv*+ba/b", 0, 1000, tmp_path, lambda st, p: None
        )
        # Attempt 2 uses the combined progressive format `b` → one stream, no merge.
        assert final == tmp_path / "sec0.mp4"
        assert final.exists()
        # Attempt 1 failed (video probe), attempt 2: video probe only (1 stream).
        assert calls["probe"] == 2
        # Proxy: attempt 1 video (failed), attempt 2 video → 2 total, cleaned up.
        assert len(_FakeRangeProxy.instances) == 2
        assert all(p.exited for p in _FakeRangeProxy.instances)
        # The second attempt really used the progressive `b` selector.
        b_runs = [r for r in sub.runs if "-f" in r and r[r.index("-f") + 1] == "b"]
        assert b_runs, "progressive fallback `b` never tried"

    def test_proxy_cleaned_up_on_error(self, monkeypatch, tmp_path):
        class Sub(_MakeFilesSubprocess):
            def run(self, args, **kw):
                if args and args[0] == "yt-dlp":
                    return _Proc(0, "http://v/url\n")
                return _Proc(1, "", "video boom")

        monkeypatch.setattr(ys, "subprocess", Sub())
        monkeypatch.setattr(ys, "_run_ffmpeg_progress", lambda cmd, dur, cb: (_ for _ in ()).throw(ys.YtError("video boom")))
        _FakeRangeProxy.instances = []
        monkeypatch.setattr(ys, "_RangeProxy", _FakeRangeProxy)
        with pytest.raises(ys.YtError):
            ys._proxy_cut("https://youtu.be/vid123", "bv*+ba/b", 0, 1000, tmp_path, lambda st, p: None)
        assert len(_FakeRangeProxy.instances) == 1
        assert _FakeRangeProxy.instances[0].exited

    def test_run_ffmpeg_progress(self, monkeypatch):
        # -progress pipe:1 → out_time_ms on stdout (milliseconds).
        out = [
            "out_time_ms=2000",
            "progress=continue",
            "out_time_ms=4000",
            "progress=end",
        ]
        monkeypatch.setattr(
            ys, "subprocess",
            routing_subprocess(popen_cls=fake_popen([], stdout_lines=out)),
        )
        seen = []
        ys._run_ffmpeg_progress(["ffmpeg", "-i", "x"], 10.0, lambda pct: seen.append(pct))
        assert seen and seen[-1] == 40.0

    def test_run_ffmpeg_progress_fails(self, monkeypatch):
        monkeypatch.setattr(
            ys, "subprocess",
            routing_subprocess(popen_cls=fake_popen(["ffmpeg: error"], returncode=1)),
        )
        with pytest.raises(ys.YtError, match="Stream cut failed"):
            ys._run_ffmpeg_progress(["ffmpeg", "-i", "x"], 10.0, lambda pct: None)

    def test_byte_progress_sampler_flows(self, monkeypatch):
        """When ffmpeg emits no blocks (idle decode), the proxy byte sampler
        still reports progress in realtime."""
        import time as _time

        calls: list[float] = []
        real_sleep = _time.sleep

        def fake_ffmpeg(cmd, dur, cb):
            # Fake ffmpeg: idle decode (never calls cb), 0.3s.
            real_sleep(0.3)

        class P:
            def __init__(self):
                self.n = 0

            def byte_pct(self):
                self.n += 1
                return min(90.0, self.n * 10)  # grows each sample

        monkeypatch.setattr(ys, "_run_ffmpeg_progress", fake_ffmpeg)
        monkeypatch.setattr(ys, "_BYTE_POLL_INTERVAL", 0.05)
        ys._run_with_byte_progress(["ffmpeg"], 5.0, lambda pct: calls.append(pct), P())
        # The sampler polls several times while ffmpeg runs → progress rises in
        # realtime even though ffmpeg reports nothing itself.
        assert len(calls) >= 2
        assert calls[0] < calls[-1]
        assert max(calls) <= 90.0

    def test_byte_progress_stops_when_ffmpeg_fails(self, monkeypatch):
        """ffmpeg fails → the sampler stops too (thread joined in finally)."""
        def fake_ffmpeg(cmd, dur, cb):
            raise ys.YtError("video boom")

        monkeypatch.setattr(ys, "_run_ffmpeg_progress", fake_ffmpeg)
        with pytest.raises(ys.YtError, match="video boom"):
            ys._run_with_byte_progress(["ffmpeg"], 5.0, lambda pct: None, object())

    def test_range_proxy_closes_open_range(self, monkeypatch):
        captured = {}

        class Resp:
            status_code = 206
            headers = {"Content-Range": "bytes 500-1048575/73732097"}
            content = b"DATA" * 10

        def fake_get(url, **kw):
            captured["headers"] = kw["headers"]
            return Resp()

        monkeypatch.setattr(ys.crequests, "get", fake_get)
        with ys._RangeProxy("http://target/v") as p:
            import urllib.request

            req = urllib.request.Request(p.url, headers={"Range": "bytes=500-"})
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.read() == b"DATA" * 10
        assert captured["headers"]["Range"] == "bytes=500-1049075"  # 500 + window(1MiB) - 1

    def test_range_proxy_without_range(self, monkeypatch):
        captured = {}

        class Resp:
            status_code = 206
            headers = {"Content-Range": "bytes 0-1048575/73732097"}
            content = b"X" * 8

        def fake_get(url, **kw):
            captured["headers"] = kw["headers"]
            return Resp()

        monkeypatch.setattr(ys.crequests, "get", fake_get)
        with ys._RangeProxy("http://target/v") as p:
            import urllib.request

            req = urllib.request.Request(p.url)
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.read() == b"X" * 8
        # rqh=1: the first request must carry a Range
        assert captured["headers"]["Range"] == "bytes=0-1048575"


class TestHelpers:
    def test_url_param(self):
        assert ys._url_param("http://x?clen=123&dur=5", "clen") == "123"
        assert ys._url_param("http://x?clen=123", "dur") is None
        assert ys._url_param("http://x", "clen") is None

    def test_codec_label(self):
        assert ys._codec_label("av01.0.05M.08") == "AV1"
        assert ys._codec_label("av1") == "AV1"
        assert ys._codec_label("vp9") == "VP9"
        assert ys._codec_label("avc1.64001f") == "H.264"
        assert ys._codec_label("h264") == "H.264"
        assert ys._codec_label("hevc") == "hevc"
        assert ys._codec_label("") == "?"
        assert ys._codec_label(None) == "?"

    def test_maybe_progress(self):
        cbs = []
        ys._maybe_progress("42.3%", lambda st, p: cbs.append((st, p)), "download")
        assert cbs == [("download", 42.3)]
        ys._maybe_progress("[download] Destination: /tmp/x.mp4", lambda st, p: cbs.append((st, p)), "download")
        assert len(cbs) == 1

    def test_find_output(self, tmp_path):
        (tmp_path / "out.mp4").write_text("x")
        (tmp_path / "out.part").write_text("x")
        assert ys.find_output(tmp_path, "out") == tmp_path / "out.mp4"
        assert ys.find_output(tmp_path / "empty", "out") is None

    def test_collect_defaults(self):
        formats = [{"height": 480, "vcodec": "vp9"}]  # no fps/ext/tbr
        assert ys._collect_resolutions(formats) == [
            {"height": 480, "fps": 30, "codec": "VP9", "ext": "mp4", "bitrate": 0}
        ]
