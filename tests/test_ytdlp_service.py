# Unit tests for pure logic in ytdlp_service (no network).
# Run: uv run pytest
import pytest

from tubesnip import ytdlp_service as ys


class TestExtractVideoId:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
            ("https://youtu.be/jNQXAC9IVRw", "jNQXAC9IVRw"),
            ("https://youtu.be/jNQXAC9IVRw?t=30", "jNQXAC9IVRw"),
            ("https://www.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
            ("https://www.youtube.com/embed/jNQXAC9IVRw", "jNQXAC9IVRw"),
            ("https://www.youtube.com/live/jNQXAC9IVRw", "jNQXAC9IVRw"),
            ("https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLx", "jNQXAC9IVRw"),
            ("https://example.com/not-youtube", None),
            ("https://www.youtube.com/", None),
            ("", None),
        ],
    )
    def test_extract(self, url, expected):
        assert ys.extract_video_id(url) == expected


class TestFriendlyError:
    @pytest.mark.parametrize(
        "stderr,expected",
        [
            ("ERROR: [youtube] xxx: Private video. Sign in if you've been granted access",
             "Private video — cannot be accessed without permission."),
            ("ERROR: Sign in to confirm your age", "Age-restricted video — requires age verification (login). See README for cookie options."),
            ("ERROR: This video is available to this channel's members on level: 1",
             "Members-only video — requires login/subscription. See README for cookie options."),
            ("ERROR: This video is not available in your country",
             "Video is geo-restricted — try via proxy/VPN (see README)."),
            ("ERROR: [youtube] xxx: Video unavailable", "Video unavailable (private / removed / blocked)."),
            ("ERROR: [youtube] AAAAAAAAAAA: This video is unavailable", "Video unavailable (private / removed / blocked)."),
            ("ERROR: [youtube] xxx: Unable to extract video data: yt-dlp is outdated",
             "Failed to extract video data — yt-dlp may be outdated. Update: uv run yt-dlp -U"),
            ("ERROR: Sign in to confirm you're not a bot",
             "YouTube is blocking automated access (bot-check). Try again, or update yt-dlp: uv run yt-dlp -U"),
            ("ERROR: x is not a valid URL", "Invalid YouTube URL."),
            # empty / unknown stderr → last line (fallback)
            ("", "An unknown error occurred."),
            ("ERROR: something weird happened", "ERROR: something weird happened"),
        ],
    )
    def test_mapping(self, stderr, expected):
        assert ys._friendly_error(stderr) == expected

    def test_ansi_stripped(self):
        assert ys._friendly_error("\x1b[31mERROR: Private video\x1b[0m") == (
            "Private video — cannot be accessed without permission."
        )


class TestParsePercent:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("42.3%", 42.3),
            ("100%", 100.0),
            ("  5.9%", 5.9),
            ("[download]  42.3% of 10.5MiB at 1.2MiB/s", 42.3),
            ("[download] 100% of  145.05KiB in 00:00:03", 100.0),
            # URLs with %2C / %3D must not be caught
            ("[in#0] from 'https://...?met=1786855101%2C&x=1%3D':", None),
            ("[out#0/mp4] muxing overhead: 3.361146%", None),
            ("[download] Destination: /tmp/x.mp4", None),
            ("", None),
        ],
    )
    def test_parse(self, line, expected):
        assert ys._parse_percent(line) == expected


class TestFmtSection:
    def test_basic(self):
        assert ys._fmt_section(0) == "0:00:00"
        assert ys._fmt_section(2000) == "0:00:02"
        assert ys._fmt_section(60000) == "0:01:00"
        assert ys._fmt_section(3661000) == "1:01:01"
        assert ys._fmt_section(86400000) == "24:00:00"
        assert ys._fmt_section(-500) == "0:00:00"  # negative clamped


class TestFormatSelector:
    @pytest.mark.parametrize(
        "height,has_audio,expected",
        [
            # m4a audio (AAC) preferred: webm DASH without cues makes deep
            # seeks slow. Fall back to any audio when m4a is unavailable.
            ("best", True, "bv*+ba[ext=m4a]/bv*+ba/b"),
            ("best", False, "bv*/b"),
            (1080, True, "bv*[height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]"),
            (1080, False, "bv*[height<=1080]/b[height<=1080]"),
            ("144", True, "bv*[height<=144]+ba[ext=m4a]/bv*[height<=144]+ba/b[height<=144]"),
            ("144", False, "bv*[height<=144]/b[height<=144]"),
        ],
    )
    def test_selector(self, height, has_audio, expected):
        assert ys._format_selector(height, has_audio) == expected


class TestCookieArgs:
    def test_without_env(self, monkeypatch):
        monkeypatch.delenv("TUBESNIP_COOKIES", raising=False)
        monkeypatch.delenv("TUBESNIP_COOKIES_FROM_BROWSER", raising=False)
        assert ys._cookie_args() == []

    def test_file_cookies(self, monkeypatch):
        monkeypatch.setenv("TUBESNIP_COOKIES", "/tmp/cookies.txt")
        monkeypatch.delenv("TUBESNIP_COOKIES_FROM_BROWSER", raising=False)
        assert ys._cookie_args() == ["--cookies", "/tmp/cookies.txt"]

    def test_browser_cookies(self, monkeypatch):
        monkeypatch.delenv("TUBESNIP_COOKIES", raising=False)
        monkeypatch.setenv("TUBESNIP_COOKIES_FROM_BROWSER", "chrome")
        assert ys._cookie_args() == ["--cookies-from-browser", "chrome"]

    def test_both(self, monkeypatch):
        monkeypatch.setenv("TUBESNIP_COOKIES", "/tmp/c.txt")
        monkeypatch.setenv("TUBESNIP_COOKIES_FROM_BROWSER", "firefox")
        assert ys._cookie_args() == ["--cookies", "/tmp/c.txt", "--cookies-from-browser", "firefox"]


class TestNeedsFullFallback:
    def test_http_errors(self):
        assert ys._needs_full_fallback("ERROR: ffmpeg exited with code 8")
        assert ys._needs_full_fallback("Server returned 403 Forbidden")
        assert ys._needs_full_fallback("HTTP error 403 Forbidden")
        assert ys._needs_full_fallback("Error opening input files: Server returned 403")

    def test_not_http_error(self):
        assert not ys._needs_full_fallback("Video unavailable (private / removed).")
        assert not ys._needs_full_fallback("End exceeds video duration.")


class TestCollectResolutions:
    def test_grouped_by_height_picks_best(self):
        formats = [
            {"height": 720, "fps": 30, "vcodec": "vp9", "ext": "webm", "tbr": 1000},
            {"height": 720, "fps": 60, "vcodec": "vp9", "ext": "webm", "tbr": 2000},
            {"height": 1080, "fps": 60, "vcodec": "av01.0.05M.08", "ext": "mp4", "tbr": 3000},
            {"height": None, "fps": 30, "vcodec": "vp9", "ext": "webm", "tbr": 500},  # no height
            {"height": 1080, "fps": None, "vcodec": "none", "ext": "m4a", "tbr": 128},  # audio
        ]
        res = ys._collect_resolutions(formats)
        assert res == [
            {"height": 720, "fps": 60, "codec": "VP9", "ext": "webm", "bitrate": 2000},
            {"height": 1080, "fps": 60, "codec": "AV1", "ext": "mp4", "bitrate": 3000},
        ]

    def test_empty(self):
        assert ys._collect_resolutions([]) == []
