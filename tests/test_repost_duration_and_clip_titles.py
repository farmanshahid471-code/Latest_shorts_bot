"""Regression tests for unrestricted repost duration and clip smart titles."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import yt_shorts_bot.scheduler as clip_scheduler_module
import yt_shorts_repost_bot.fetcher as repost_fetcher_module
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler
from yt_shorts_bot.uploader import YouTubeUploader
from yt_shorts_repost_bot.fetcher import ShortsFetcher


class LongListingYDL:
    entries: ClassVar[list[dict]] = []

    def __init__(self, _options):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        return {"entries": [dict(entry) for entry in self.entries]}


def test_repost_listing_keeps_sources_longer_than_sixty_seconds(monkeypatch):
    LongListingYDL.entries = [
        {
            "id": "longshort01",
            "title": "A longer Short",
            "duration": 181,
            "webpage_url": "https://www.youtube.com/shorts/longshort01",
        },
        {
            "id": "verylong001",
            "title": "An intentionally long source",
            "duration": 901,
            "webpage_url": "https://www.youtube.com/watch?v=verylong001",
        },
    ]
    monkeypatch.setattr(repost_fetcher_module.yt_dlp, "YoutubeDL", LongListingYDL)
    monkeypatch.setattr(ShortsFetcher, "_cookies_opts", staticmethod(dict))

    videos = ShortsFetcher(fetch_limit=2).fetch_channel_recent_shorts(
        "https://www.youtube.com/@source/shorts"
    )

    assert [video["video_id"] for video in videos] == [
        "longshort01",
        "verylong001",
    ]
    assert [video["duration"] for video in videos] == [181, 901]


class LongDownloadYDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def download(self, _urls):
        Path(self.options["outtmpl"]).write_bytes(b"video" * 3_000)


def test_repost_download_does_not_delete_a_long_source(monkeypatch, tmp_path):
    monkeypatch.setattr(repost_fetcher_module.yt_dlp, "YoutubeDL", LongDownloadYDL)
    monkeypatch.setattr(ShortsFetcher, "_cookies_opts", staticmethod(dict))
    monkeypatch.setattr(
        ShortsFetcher, "_probe_duration", staticmethod(lambda _path: 901.0)
    )
    output = tmp_path / "long-source.mp4"

    result = ShortsFetcher().download_short(
        "https://www.youtube.com/shorts/longshort01", output_path=output
    )

    assert result == output
    assert output.is_file()
    assert output.stat().st_size > 0


class SmartTitleProcessor:
    detected_language = "en"
    detected_language_probability = 0.99

    def __init__(self):
        self.transcribed = 0
        self.render_subtitles = None

    @staticmethod
    def _probe_has_audio(_path):
        return True

    def transcribe_and_generate_srt(self, _video_path, srt_path=None):
        self.transcribed += 1
        target = Path(srt_path)
        target.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n"
            "Nobody expected the final challenge to end this way\n\n",
            encoding="utf-8",
        )
        return target

    def process_clip_to_short(self, _raw_path, output_path=None, **kwargs):
        self.render_subtitles = kwargs.get("subtitles")
        target = Path(output_path)
        target.write_bytes(b"processed")
        return target


class NoopStorage:
    client = None
    bucket_name = "unused"

    @staticmethod
    def upload_file(_path, r2_key=None):
        return None

    @staticmethod
    def cleanup_local_files(*paths):
        for path in paths:
            if path:
                Path(path).unlink(missing_ok=True)


def test_clip_smart_title_transcribes_even_when_burned_subtitles_are_off(
    monkeypatch, tmp_path
):
    processor = SmartTitleProcessor()
    state = StateDB(tmp_path / "state.db")
    scheduler = ShortsBotScheduler(
        accounts=[],
        state_db=state,
        processor=processor,
        storage=NoopStorage(),
    )
    raw = tmp_path / "raw.mp4"

    def download_window(_url, _start, _end):
        raw.write_bytes(b"raw")
        return raw

    monkeypatch.setattr(scheduler, "_download_window", download_window)
    monkeypatch.setattr(clip_scheduler_module, "KEEP_LOCAL_SHORTS", False)
    uploader = YouTubeUploader(state_db=state, dry_run=True)

    uploaded = scheduler._process_video_windows(
        "source12345",
        "https://www.youtube.com/watch?v=source12345",
        "Original Long Video Title",
        "https://www.youtube.com/@source/videos",
        [{"start": 10.0, "end": 28.0}],
        account="Smart Account",
        uploader=uploader,
        info={"title": "Original Long Video Title"},
        smart_titles=True,
        subtitles_enabled=False,
    )

    assert uploaded == 0
    assert processor.transcribed == 1
    assert processor.render_subtitles is False
    assert uploader.last_metadata is not None
    assert "Nobody Expected The Final Challenge" in uploader.last_metadata["title"]
    assert "Original Long Video Title" not in uploader.last_metadata["title"]
