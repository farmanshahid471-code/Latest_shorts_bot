"""Regression coverage for the August 2026 authenticated yt-dlp client issue."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

import yt_shorts_bot.fetcher as clip_fetcher_module
import yt_shorts_repost_bot.fetcher as repost_fetcher_module
from yt_dlp_support import authenticated_youtube_options, run_youtube_dl
from yt_shorts_bot.fetcher import YouTubeFetcher
from yt_shorts_repost_bot.fetcher import ShortsFetcher

SAFE_CLIENTS = ["default", "web_embedded"]


def cookie_options(tmp_path: Path) -> dict:
    return {
        "cookiefile": str(tmp_path / "cookies.txt"),
        **authenticated_youtube_options(),
    }


class ReloadThenSuccessYDL:
    """Simulate the production failure only while viewer cookies are present."""

    seen: ClassVar[list[dict]] = []
    payload: ClassVar[dict] = {}

    def __init__(self, options):
        self.options = options
        self.__class__.seen.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        if self.options.get("cookiefile"):
            raise RuntimeError("The page needs to be reloaded")
        return self.__class__.payload


@pytest.fixture(autouse=True)
def reset_fake():
    ReloadThenSuccessYDL.seen = []
    ReloadThenSuccessYDL.payload = {}


def assert_safe_authenticated_then_anonymous(seen: list[dict]) -> None:
    assert len(seen) == 2
    assert seen[0]["cookiefile"].endswith("cookies.txt")
    assert seen[0]["extractor_args"]["youtube"]["player_client"] == SAFE_CLIENTS
    assert "cookiefile" not in seen[1]
    assert "extractor_args" not in seen[1]


def test_clip_metadata_keeps_safe_cookies_and_public_sources_fall_back(
    monkeypatch, tmp_path
):
    ReloadThenSuccessYDL.payload = {
        "id": "abcdefghijk",
        "title": "Public source",
        "duration": 120,
        "formats": [],
    }
    monkeypatch.setattr(clip_fetcher_module.yt_dlp, "YoutubeDL", ReloadThenSuccessYDL)
    monkeypatch.setattr(
        YouTubeFetcher,
        "_cookies_opts",
        staticmethod(lambda: cookie_options(tmp_path)),
    )

    info = YouTubeFetcher()._get_info("https://www.youtube.com/watch?v=abcdefghijk")

    assert info["id"] == "abcdefghijk"
    assert_safe_authenticated_then_anonymous(ReloadThenSuccessYDL.seen)


def test_clip_channel_scan_uses_the_same_cookie_client_policy(monkeypatch, tmp_path):
    ReloadThenSuccessYDL.payload = {
        "entries": [
            {
                "id": "abcdefghijk",
                "title": "Long source",
                "duration": 120,
                "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
            }
        ]
    }
    monkeypatch.setattr(clip_fetcher_module.yt_dlp, "YoutubeDL", ReloadThenSuccessYDL)
    monkeypatch.setattr(
        YouTubeFetcher,
        "_cookies_opts",
        staticmethod(lambda: cookie_options(tmp_path)),
    )

    videos = YouTubeFetcher(fetch_limit=1).fetch_channel_recent_videos(
        "https://www.youtube.com/@source"
    )

    assert [video["video_id"] for video in videos] == ["abcdefghijk"]
    assert_safe_authenticated_then_anonymous(ReloadThenSuccessYDL.seen)


def test_clip_download_stream_lookup_falls_back_only_for_public_video(
    monkeypatch, tmp_path
):
    ReloadThenSuccessYDL.payload = {
        "id": "abcdefghijk",
        "url": "https://media.example/video.mp4",
        "vcodec": "avc1.640028",
        "acodec": "mp4a.40.2",
        "ext": "mp4",
        "height": 1080,
    }
    monkeypatch.setattr(clip_fetcher_module.yt_dlp, "YoutubeDL", ReloadThenSuccessYDL)
    monkeypatch.setattr(
        YouTubeFetcher,
        "_cookies_opts",
        staticmethod(lambda: cookie_options(tmp_path)),
    )
    monkeypatch.setattr(
        YouTubeFetcher,
        "_ffmpeg_slice",
        staticmethod(lambda _args, path: path.write_bytes(b"video")),
    )
    monkeypatch.setattr(
        YouTubeFetcher,
        "_verify_slice",
        staticmethod(lambda _path, _start, _end: True),
    )
    output = tmp_path / "clip.mp4"

    result = YouTubeFetcher()._slice_progressive(
        "https://www.youtube.com/watch?v=abcdefghijk", 10.0, 30.0, output
    )

    assert result == output
    assert output.read_bytes() == b"video"
    assert_safe_authenticated_then_anonymous(ReloadThenSuccessYDL.seen)


def test_repost_feed_scan_uses_the_same_cookie_client_policy(monkeypatch, tmp_path):
    ReloadThenSuccessYDL.payload = {
        "entries": [
            {
                "id": "abcdefghijk",
                "title": "Short source",
                "duration": 30,
                "webpage_url": "https://www.youtube.com/shorts/abcdefghijk",
            }
        ]
    }
    monkeypatch.setattr(repost_fetcher_module.yt_dlp, "YoutubeDL", ReloadThenSuccessYDL)
    monkeypatch.setattr(
        ShortsFetcher,
        "_cookies_opts",
        staticmethod(lambda: cookie_options(tmp_path)),
    )

    videos = ShortsFetcher(fetch_limit=1).fetch_channel_recent_shorts(
        "https://www.youtube.com/@source"
    )

    assert [video["video_id"] for video in videos] == ["abcdefghijk"]
    assert_safe_authenticated_then_anonymous(ReloadThenSuccessYDL.seen)


def test_repost_information_lookup_no_longer_forces_tv_client(monkeypatch, tmp_path):
    ReloadThenSuccessYDL.payload = {
        "id": "abcdefghijk",
        "title": "Public Short",
        "duration": 30,
    }
    monkeypatch.setattr(repost_fetcher_module.yt_dlp, "YoutubeDL", ReloadThenSuccessYDL)
    monkeypatch.setattr(
        ShortsFetcher,
        "_cookies_opts",
        staticmethod(lambda: cookie_options(tmp_path)),
    )

    info = ShortsFetcher.get_short_info("https://www.youtube.com/shorts/abcdefghijk")

    assert info["duration"] == 30
    assert_safe_authenticated_then_anonymous(ReloadThenSuccessYDL.seen)
    assert all(
        "tv"
        not in options.get("extractor_args", {})
        .get("youtube", {})
        .get("player_client", [])
        for options in ReloadThenSuccessYDL.seen
    )


def test_age_gate_never_drops_viewer_cookies():
    class AgeGateYDL:
        seen: ClassVar[list[dict]] = []

        def __init__(self, options):
            self.options = options
            self.__class__.seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            raise RuntimeError(
                "Sign in to confirm your age; this video is age-restricted"
            )

    with pytest.raises(RuntimeError, match="confirm your age"):
        run_youtube_dl(
            AgeGateYDL,
            {"cookiefile": "cookies.txt", **authenticated_youtube_options()},
            lambda ydl: ydl.extract_info("https://youtu.be/abcdefghijk"),
            logger=clip_fetcher_module.logger,
            context="age-gate regression test",
        )

    assert len(AgeGateYDL.seen) == 1
    assert AgeGateYDL.seen[0]["cookiefile"] == "cookies.txt"
