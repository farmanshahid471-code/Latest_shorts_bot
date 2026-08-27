"""A cycle must never freeze silently: gap waits, skips and network calls are logged/bounded."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


import yt_shorts_bot.fetcher as fetcher_module
import yt_shorts_bot.scheduler as scheduler_module
from yt_shorts_bot.fetcher import YouTubeFetcher
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler


class FakeEvent:
    def __init__(self, interrupted: bool = False):
        self._interrupted = interrupted

    def is_set(self) -> bool:
        return self._interrupted

    def wait(self, timeout=None) -> bool:
        return self._interrupted

    def set(self) -> None:
        self._interrupted = True


def test_upload_gap_wait_logs_remaining_time_and_respects_stop(monkeypatch, caplog):
    scheduler = ShortsBotScheduler(accounts=[])
    last_upload = datetime.now(timezone.utc) - timedelta(minutes=5)
    monkeypatch.setattr(
        scheduler.state_db, "get_last_upload_time", lambda account="": last_upload
    )
    monkeypatch.setattr(scheduler, "stop_event", FakeEvent(interrupted=True))

    with caplog.at_level("INFO", logger="yt_shorts_bot.scheduler"):
        ok = scheduler._wait_for_upload_gap("default", min_gap_minutes=30)

    assert ok is False  # a stop request interrupts the silent wait
    assert "min_minutes_between_uploads=30" in caplog.text
    assert "Waiting" in caplog.text
    assert "interrupted" in caplog.text.lower()


def test_gap_wait_returns_immediately_when_gap_elapsed(monkeypatch):
    scheduler = ShortsBotScheduler(accounts=[])
    last_upload = datetime.now(timezone.utc) - timedelta(minutes=90)
    monkeypatch.setattr(
        scheduler.state_db, "get_last_upload_time", lambda account="": last_upload
    )
    monkeypatch.setattr(scheduler, "stop_event", FakeEvent(interrupted=False))
    assert scheduler._wait_for_upload_gap("default", min_gap_minutes=60) is True


class FakeYDL:
    seen_opts: dict = {}

    def __init__(self, opts):
        self.__class__.seen_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        return {
            "entries": [
                {
                    "id": "v1",
                    "title": "T",
                    "duration": 600,
                    "webpage_url": "https://www.youtube.com/watch?v=v1",
                }
            ]
        }


def test_every_fetch_call_is_bounded_by_a_socket_timeout(monkeypatch):
    monkeypatch.setattr(fetcher_module.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(fetcher_module, "YTDL_SOCKET_TIMEOUT_SEC", 12.0)

    YouTubeFetcher().fetch_channel_recent_videos("https://www.youtube.com/@chan")

    opts = FakeYDL.seen_opts
    assert opts["socket_timeout"] == 12.0
    assert opts["retries"] == 2
    assert opts["extractor_retries"] == 2


class FakeFetcherForAllSkipped:
    def __init__(self, channels=None, strategy=None, heatmap_weight=None, audio_weight=None):
        self.channels = channels

    def fetch_channel_recent_videos(self, channel_url, order="newest"):
        return [
            {
                "video_id": f"v{i}",
                "url": f"https://youtu.be/v{i}",
                "title": f"T{i}",
                "duration": 600,
                "channel": channel_url,
            }
            for i in range(1, 6)
        ]

    def extract_heatmap_and_select_window(self, url):
        return {"id": "x", "title": "T"}, 30.0, 21.0, 39.0


def test_all_candidates_already_processed_is_logged_not_silent(monkeypatch, tmp_path, caplog):
    processed: list = []

    class FakeUploader:
        def __init__(self, client_secret_file=None, token_file=None, state_db=None):
            pass

    monkeypatch.setattr(scheduler_module, "YouTubeFetcher", FakeFetcherForAllSkipped)
    monkeypatch.setattr(scheduler_module, "YouTubeUploader", FakeUploader)
    monkeypatch.setattr(
        scheduler_module,
        "resolve_credentials",
        lambda account: (tmp_path / "cs.json", tmp_path / "token.json"),
    )
    db = StateDB(Path(tmp_path) / "state.db")
    for i in range(1, 6):
        db.record_video_state(
            video_id=f"v{i}",
            status="UPLOADED_YOUTUBE",
            youtube_short_id="old",
            account="default",
        )
    scheduler = ShortsBotScheduler(accounts=[], state_db=db)
    monkeypatch.setattr(scheduler, "_process_video_windows", lambda *a, **k: processed.append(1) or 1)
    account = {
        "name": "default",
        "target_channels": ["https://www.youtube.com/@chan"],
        "enabled": True,
        "max_daily_uploads": 10,
        "shorts_per_video": 1,
    }

    with caplog.at_level("INFO", logger="yt_shorts_bot.scheduler"):
        total = scheduler.run_single_cycle(accounts=[account])

    assert total == 0
    assert processed == []
    assert "Nothing left to do" in caplog.text
    assert "FETCH_SCAN_LIMIT" in caplog.text


def test_picked_candidate_is_logged_with_skip_counts(monkeypatch, tmp_path, caplog):
    processed: list = []

    class FakeUploader:
        def __init__(self, client_secret_file=None, token_file=None, state_db=None):
            pass

    monkeypatch.setattr(scheduler_module, "YouTubeFetcher", FakeFetcherForAllSkipped)
    monkeypatch.setattr(scheduler_module, "YouTubeUploader", FakeUploader)
    monkeypatch.setattr(
        scheduler_module,
        "resolve_credentials",
        lambda account: (tmp_path / "cs.json", tmp_path / "token.json"),
    )
    db = StateDB(Path(tmp_path) / "state.db")
    db.record_video_state(
        video_id="v1", status="UPLOADED_YOUTUBE", youtube_short_id="old", account="default"
    )
    scheduler = ShortsBotScheduler(accounts=[], state_db=db)

    def fake_process(*args, **kwargs):
        processed.append(args[0])
        return 1

    monkeypatch.setattr(scheduler, "_process_video_windows", fake_process)
    account = {
        "name": "default",
        "target_channels": ["https://www.youtube.com/@chan"],
        "enabled": True,
        "max_daily_uploads": 10,
        "shorts_per_video": 1,
    }

    with caplog.at_level("INFO", logger="yt_shorts_bot.scheduler"):
        total = scheduler.run_single_cycle(accounts=[account])

    assert total == 1
    assert processed == ["v2"]
    assert "Picked candidate v2" in caplog.text
    assert "1 already uploaded" in caplog.text
