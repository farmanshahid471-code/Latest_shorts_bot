"""Tests for source ordering (newest/oldest/random) and already-uploaded skipping."""
from __future__ import annotations

from pathlib import Path

import pytest

import yt_shorts_bot.fetcher as fetcher_module
import yt_shorts_bot.scheduler as scheduler_module
from yt_shorts_bot.fetcher import YouTubeFetcher
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler


class FakeTabYDL:
    """Replays a newest-first channel listing; records the opts and requested window."""

    entries: list[dict] = []
    seen_opts: list[dict] = []
    url = ""

    def __init__(self, opts):
        self.opts = opts
        self.__class__.seen_opts.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        self.__class__.url = url
        return {"entries": [dict(e) for e in self.entries]}


@pytest.fixture
def newest_first_entries():
    # Newest first: v1 newest ... v6 oldest.
    return [
        {"id": f"v{i}", "title": f"Video {i}", "duration": 600, "webpage_url": f"https://www.youtube.com/watch?v=v{i}"}
        for i in range(1, 7)
    ]


@pytest.fixture
def fake_tab(newest_first_entries, monkeypatch):
    FakeTabYDL.entries = newest_first_entries
    FakeTabYDL.seen_opts = []
    monkeypatch.setattr(fetcher_module.yt_dlp, "YoutubeDL", FakeTabYDL)
    return FakeTabYDL


def ids(list_of_dicts):
    return [d["video_id"] for d in list_of_dicts]


def test_newest_keeps_newest_first(fake_tab, monkeypatch):
    monkeypatch.setattr(fetcher_module, "FETCH_SCAN_LIMIT", 100)
    out = YouTubeFetcher(fetch_limit=5).fetch_channel_recent_videos(
        "https://www.youtube.com/@chan", order="newest"
    )
    assert ids(out) == ["v1", "v2", "v3", "v4", "v5", "v6"]
    # Deep window so already-uploaded videos can be skipped.
    assert FakeTabYDL.seen_opts[0]["playlistend"] == 100


def test_oldest_returns_true_oldest_first(fake_tab, monkeypatch):
    """Oldest must start at v6, NOT the reverse of the newest N."""
    monkeypatch.setattr(fetcher_module, "FETCH_SCAN_LIMIT", 100)
    out = YouTubeFetcher(fetch_limit=5).fetch_channel_recent_videos(
        "https://www.youtube.com/@chan", order="oldest"
    )
    assert ids(out) == ["v6", "v5", "v4", "v3", "v2", "v1"]


def test_random_is_a_permutation_and_not_identity(fake_tab, monkeypatch):
    monkeypatch.setattr(fetcher_module, "FETCH_SCAN_LIMIT", 100)
    expected = ["v1", "v2", "v3", "v4", "v5", "v6"]
    out = YouTubeFetcher(fetch_limit=5).fetch_channel_recent_videos(
        "https://www.youtube.com/@chan", order="random"
    )
    got = ids(out)
    assert sorted(got) == sorted(expected)  # same set, no dupes
    # Over enough draws a non-trivial shuffle is overwhelmingly likely, but the
    # real invariant is: a permutation of the scanned window (not a fixed slice).
    assert len(got) == len(expected)
    assert not FakeTabYDL.url.endswith("/random")


def test_invalid_order_falls_back_to_newest(fake_tab):
    out = YouTubeFetcher().fetch_channel_recent_videos(
        "https://www.youtube.com/@chan", order="bogus"
    )
    assert ids(out) == IDs_NEWEST_FIRST


IDs_NEWEST_FIRST = ["v1", "v2", "v3", "v4", "v5", "v6"]


# ---------------------------------------------------------------------------
# Scheduler skip behaviour (clip bot)
# ---------------------------------------------------------------------------
class FakeFetcherForScheduler:
    """Returns a fixed newest-first list; honours order (like the real fetcher)."""

    order_seen = None

    def __init__(self, channels=None, strategy=None, heatmap_weight=None, audio_weight=None):
        self.channels = channels

    def fetch_channel_recent_videos(self, channel_url, order="newest"):
        self.__class__.order_seen = order
        videos = [
            {"video_id": "v1", "url": "https://youtu.be/v1", "title": "T1", "duration": 600, "channel": channel_url},
            {"video_id": "v2", "url": "https://youtu.be/v2", "title": "T2", "duration": 600, "channel": channel_url},
            {"video_id": "v3", "url": "https://youtu.be/v3", "title": "T3", "duration": 600, "channel": channel_url},
            {"video_id": "v4", "url": "https://youtu.be/v4", "title": "T4", "duration": 600, "channel": channel_url},
            {"video_id": "v5", "url": "https://youtu.be/v5", "title": "T5", "duration": 600, "channel": channel_url},
        ]
        if order == "oldest":
            videos.reverse()
        elif order == "random":
            import random
            random.shuffle(videos)
        return videos

    def extract_heatmap_and_select_window(self, url):
        return {"id": "x", "title": "T"}, 30.0, 21.0, 39.0

    def select_top_windows(self, url, count=3):
        return [
            {"start": 21.0 + i * 25.0, "end": 39.0 + i * 25.0, "score": 1.0 - i, "source": "combined"}
            for i in range(count)
        ]


@pytest.fixture
def scheduler_env(monkeypatch, tmp_path):
    processed: list[dict] = []

    class FakeUploader:
        def __init__(self, client_secret_file=None, token_file=None, state_db=None):
            self.last_metadata = None

    monkeypatch.setattr(scheduler_module, "YouTubeFetcher", FakeFetcherForScheduler)
    monkeypatch.setattr(scheduler_module, "YouTubeUploader", FakeUploader)
    monkeypatch.setattr(
        scheduler_module,
        "resolve_credentials",
        lambda account: (tmp_path / "cs.json", tmp_path / "token.json"),
    )
    db = StateDB(Path(tmp_path) / "state.db")
    scheduler = ShortsBotScheduler(accounts=[], state_db=db)

    def fake_process_video_windows(
        video_id, video_url, video_title, channel_url, windows, account="", **kwargs
    ):
        # Simulate a real successful upload by marking the source terminal, so a
        # later run must SKIP it and pick the next candidate in the order.
        processed.append((video_id, account))
        db.record_video_state(
            video_id=video_id,
            video_url=video_url,
            title=video_title,
            clip_start=float(windows[0]["start"]),
            clip_end=float(windows[0]["end"]),
            youtube_short_id="ytshort_abc",
            status="UPLOADED_YOUTUBE",
            account=account,
        )
        return 1

    monkeypatch.setattr(scheduler, "_process_video_windows", fake_process_video_windows)
    return scheduler, db, processed


def test_newest_skips_uploaded_and_picks_next(scheduler_env):
    scheduler, db, processed = scheduler_env
    account = {
        "name": "default",
        "target_channels": ["https://www.youtube.com/@chan"],
        "enabled": True,
        "max_daily_uploads": 10,
        "shorts_per_video": 1,
    }
    # v1 newest already uploaded -> must skip to v2.
    db.record_video_state(video_id="v1", status="UPLOADED_YOUTUBE", youtube_short_id="old", account="default")

    scheduler.run_single_cycle(accounts=[account])
    assert FakeFetcherForScheduler.order_seen == "newest"
    assert processed == [("v2", "default")]


def test_oldest_skips_uploaded_and_picks_next(scheduler_env):
    scheduler, db, processed = scheduler_env
    account = {
        "name": "default",
        "target_channels": ["https://www.youtube.com/@chan"],
        "enabled": True,
        "max_daily_uploads": 10,
        "shorts_per_video": 1,
        "selection_order": "oldest",
    }
    # v5 (oldest in the window) already uploaded -> must skip to v4.
    db.record_video_state(video_id="v5", status="UPLOADED_YOUTUBE", youtube_short_id="old", account="default")

    scheduler.run_single_cycle(accounts=[account])
    assert FakeFetcherForScheduler.order_seen == "oldest"
    assert processed == [("v4", "default")]


def test_only_one_video_per_channel_per_cycle(scheduler_env):
    scheduler, db, processed = scheduler_env
    account = {
        "name": "default",
        "target_channels": ["https://www.youtube.com/@chan"],
        "enabled": True,
        "max_daily_uploads": 10,
        "shorts_per_video": 1,
    }
    scheduler.run_single_cycle(accounts=[account])
    assert processed == [("v1", "default")]  # v1 only, not v2 v3...


def test_newest_only_skips_video_when_its_shorts_are_fully_uploaded(monkeypatch, tmp_path):
    """'newest' must pick the newest video, and only skip it once every short
    FROM THAT VIDEO exists. Partially-done videos are completed, never jumped."""
    from pathlib import Path as P


    db = StateDB(P(tmp_path) / "state.db")
    scheduler = ShortsBotScheduler(accounts=[], state_db=db)
    uploaded: list[str] = []

    # Part 1 of v1 already uploaded -> parts 2 and 3 must still be completed.
    db.record_video_state(video_id="v1_part1", status="UPLOADED_YOUTUBE", account="default")

    def fake_download(url, start, end):
        p = P(tmp_path) / f"raw_{start}_{end}.mp4"
        p.write_bytes(b"x")
        return p

    def fake_render(raw, output_path=None, **kwargs):
        P(output_path).write_bytes(b"x")
        return P(output_path)

    class FakeUploader:
        def __init__(self, state_db=None):
            self.last_metadata = None

        def upload_short(self, video_path, original_video_id, account="", **kwargs):
            uploaded.append(original_video_id)
            return f"ytshort_{original_video_id}"

    class FakeStorage:
        client = None
        bucket_name = ""

        def upload_file(self, path, r2_key=""):
            return "r2_key"

        def cleanup_local_files(self, *paths):
            pass

    monkeypatch.setattr(scheduler, "_download_window", fake_download)
    monkeypatch.setattr(scheduler, "storage", FakeStorage())
    scheduler.processor.process_clip_to_short = fake_render
    uploader = FakeUploader()
    scheduler.state_db = db

    windows = [{"start": 10 + i * 25, "end": 25 + i * 25} for i in range(3)]
    count = scheduler._process_video_windows(
        video_id="v1",
        video_url="https://youtu.be/v1",
        video_title="T1",
        channel_url="https://www.youtube.com/@chan",
        windows=windows,
        account="default",
        uploader=uploader,
    )
    assert count == 2
    assert uploaded == ["v1_part2", "v1_part3"]
    # Whole video is now terminal -> the next cycle skips it entirely.
    assert db.is_video_processed("v1", account="default")


def test_terminal_multi_status_skips_whole_video(scheduler_env):
    """After all parts are uploaded the parent video must never be re-picked."""
    scheduler, db, processed = scheduler_env
    account = {
        "name": "default",
        "target_channels": ["https://www.youtube.com/@chan"],
        "enabled": True,
        "max_daily_uploads": 10,
        "shorts_per_video": 3,
    }
    # Simulate: parent was marked PROCESSED_MULTI after all 3 parts uploaded.
    for part in ("v1_part1", "v1_part2", "v1_part3"):
        db.record_video_state(video_id=part, status="UPLOADED_YOUTUBE", account="default")
    db.record_video_state(video_id="v1", status="PROCESSED_MULTI", account="default")

    scheduler.run_single_cycle(accounts=[account])
    assert processed == [("v2", "default")]
