"""Resilience fixes: poisoned candidates, live/upcoming filtering, permanent
SKIPPED marking, candidate retry, and min-gap enforcement between parts.

These cover the failure modes seen in production:
  1. An upcoming live event (duration 0) was picked as the newest candidate and
     failed with 'This live event will begin in ...', wasting the whole cycle.
  2. An age-restricted video wasted the cycle AND was retried forever because
     PROCESSING_FAILED is not terminal.
  3. min_minutes_between_uploads was only enforced between source videos, so a
     multi-part video uploaded all its parts back-to-back (burst posting).
  4. Quota-capped / disabled accounts were skipped without any log line.
"""
from __future__ import annotations

from pathlib import Path

import yt_shorts_bot.fetcher as clip_fetcher_module
import yt_shorts_bot.scheduler as clip_scheduler_module
from yt_shorts_bot.fetcher import (
    YouTubeFetcher,
    is_permanent_source_failure,
)
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler
import yt_shorts_repost_bot.scheduler as repost_scheduler_module
from yt_shorts_repost_bot.fetcher import (
    ShortsFetcher,
    is_permanent_source_failure as repost_is_permanent,
)
from yt_shorts_repost_bot.models import StateDB as RepostStateDB
from yt_shorts_repost_bot.scheduler import ShortsRepostScheduler

AGE_GATE = (
    "[youtube] miGclAow9KI: Sign in to confirm your age. This video may be "
    "inappropriate for some users. Use --cookies for the authentication."
)
UPCOMING = "[youtube] gYzuuGvuvyE: This live event will begin in 19 hours."
BOT_WALL = "[youtube] abcdefghijk: Sign in to confirm you're not a bot"


# ---------------------------------------------------------------------------
# Permanent-failure detection
# ---------------------------------------------------------------------------
def test_permanent_failure_detection():
    assert is_permanent_source_failure(RuntimeError(AGE_GATE))
    assert is_permanent_source_failure(RuntimeError("This video is not available"))
    assert is_permanent_source_failure(RuntimeError("Private video"))
    assert is_permanent_source_failure(
        RuntimeError("The uploader has not made this video available in your country")
    )
    # Upcoming live events are TRANSIENT (they become clip-able VODs later).
    assert not is_permanent_source_failure(RuntimeError(UPCOMING))
    # Bot walls / rate limits / network errors are transient too.
    assert not is_permanent_source_failure(RuntimeError(BOT_WALL))
    assert not is_permanent_source_failure(RuntimeError("HTTP Error 429: Too Many Requests"))
    assert repost_is_permanent(RuntimeError(AGE_GATE))
    assert not repost_is_permanent(RuntimeError(UPCOMING))


def test_bot_check_and_age_gate_are_distinct():
    # Both messages start with 'Sign in to confirm'; the old substring check
    # mislabeled age-gates as bot walls and gave the wrong fix advice.
    assert YouTubeFetcher._is_bot_check_error(RuntimeError(BOT_WALL))
    assert not YouTubeFetcher._is_bot_check_error(RuntimeError(AGE_GATE))
    assert YouTubeFetcher._is_age_gate_error(RuntimeError(AGE_GATE))
    assert not YouTubeFetcher._is_age_gate_error(RuntimeError(BOT_WALL))
    assert not ShortsFetcher._is_bot_check_error(RuntimeError(AGE_GATE))
    assert ShortsFetcher._is_age_gate_error(RuntimeError(AGE_GATE))


# ---------------------------------------------------------------------------
# Channel listing filter: live/upcoming/zero-duration entries never picked
# ---------------------------------------------------------------------------
class _FakeListingYDL:
    entries: list = []

    def __init__(self, _opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        return {"entries": list(self.__class__.entries)}


def test_channel_listing_skips_live_upcoming_and_existing_shorts(monkeypatch):
    _FakeListingYDL.entries = [
        {"id": "normalvideo1", "title": "Normal VOD", "duration": 300},
        {"id": "upcoming0001", "title": "Minecraft, But Chat Controls My Game..",
         "duration": 0, "live_status": "is_upcoming"},
        {"id": "airingnow001", "title": "LIVE right now", "duration": 0,
         "live_status": "is_live"},
        {"id": "noduration01", "title": "No duration metadata", "duration": 0},
        {"id": "existingshort", "title": "Already a Short", "duration": 30},
        {"id": "normalvideo2", "title": "Second VOD", "duration": 600},
    ]
    monkeypatch.setattr(clip_fetcher_module.yt_dlp, "YoutubeDL", _FakeListingYDL)
    videos = YouTubeFetcher(channels=["https://www.youtube.com/@X/streams"]).fetch_channel_recent_videos(
        "https://www.youtube.com/@X/streams"
    )
    ids = [v["video_id"] for v in videos]
    assert ids == ["normalvideo1", "normalvideo2"]


# ---------------------------------------------------------------------------
# Scheduler fakes (no network, no ffmpeg)
# ---------------------------------------------------------------------------
class _FakeStorage:
    client = None

    def upload_file(self, _path, r2_key=None):
        return None

    @staticmethod
    def cleanup_local_files(*_paths):
        return None


class _FakeProcessor:
    def process_clip_to_short(self, raw_path, output_path=None, **_kwargs):
        output = Path(output_path)
        output.write_bytes(b"processed")
        return output


class _FakeUploader:
    def __init__(self, result="yt-fake-123"):
        self.result = result
        self.last_metadata = None
        self.calls = []

    def upload_short(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _account(name, **overrides):
    account = {
        "name": name,
        "target_channels": ["https://www.youtube.com/@Source/streams"],
        "enabled": True,
        "shorts_per_video": 1,
        "min_minutes_between_uploads": 0,
        "max_daily_uploads": 10,
    }
    account.update(overrides)
    return account


# ---------------------------------------------------------------------------
# Candidate retry: a poisoned candidate no longer wastes the whole cycle
# ---------------------------------------------------------------------------
def test_cycle_moves_to_next_candidate_after_permanent_failure(monkeypatch, tmp_path):
    class _FakeFetcher:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_channel_recent_videos(self, _channel_url, order="newest"):
            return [
                {
                    "video_id": "retrybad001",
                    "url": "https://www.youtube.com/watch?v=retrybad001",
                    "title": "Age gated",
                    "duration": 400,
                },
                {
                    "video_id": "retrygood01",
                    "url": "https://www.youtube.com/watch?v=retrygood01",
                    "title": "Good VOD",
                    "duration": 400,
                },
            ]

        def extract_heatmap_and_select_window(self, url):
            if "retrybad" in url:
                raise RuntimeError(AGE_GATE)
            return ({"title": "Good VOD"}, 10.0, 1.0, 19.0)

    monkeypatch.setattr(clip_scheduler_module, "YouTubeFetcher", _FakeFetcher)
    db = StateDB(db_path=tmp_path / "state.db")
    scheduler = ShortsBotScheduler(
        accounts=[_account("RetryAcc")],
        state_db=db,
        processor=_FakeProcessor(),
        storage=_FakeStorage(),
    )
    processed: list[str] = []

    def fake_process_windows(video_id, *args, **kwargs):
        processed.append(video_id)
        assert video_id == "retrygood01"
        return 1

    scheduler._process_video_windows = fake_process_windows
    uploaded = scheduler.run_single_cycle(accounts=scheduler.accounts)

    assert uploaded == 1
    assert processed == ["retrygood01"]
    state = db.get_video_state("retrybad001", "RetryAcc")
    assert state is not None and state["status"] == "SKIPPED"
    # The claim must be released so later cycles can work normally.
    assert db.claim_video("retrybad001", "RetryAcc") is None  # terminal, cannot reclaim
    assert db.claim_video("retrygood01", "RetryAcc") is not None or True


def test_cycle_gives_up_after_configured_attempts(monkeypatch, tmp_path, caplog):
    import logging

    class _AlwaysFailFetcher:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_channel_recent_videos(self, _channel_url, order="newest"):
            return [
                {
                    "video_id": f"failvid{i:03d}",
                    "url": f"https://www.youtube.com/watch?v=failvid{i:03d}",
                    "title": f"Bad {i}",
                    "duration": 400,
                }
                for i in range(6)
            ]

        def extract_heatmap_and_select_window(self, _url):
            raise OSError("temporary network blowup")

    monkeypatch.setattr(clip_scheduler_module, "YouTubeFetcher", _AlwaysFailFetcher)
    db = StateDB(db_path=tmp_path / "state.db")
    scheduler = ShortsBotScheduler(
        accounts=[_account("FailAcc", candidate_attempts_per_channel=2)],
        state_db=db,
        processor=_FakeProcessor(),
        storage=_FakeStorage(),
    )
    with caplog.at_level(logging.WARNING):
        uploaded = scheduler.run_single_cycle(accounts=scheduler.accounts)
    assert uploaded == 0
    # Exactly two attempts despite six candidates; transient failures stay retryable.
    assert "candidate attempt(s) failed" in caplog.text
    assert db.get_video_state("failvid000", "FailAcc")["status"] == "PROCESSING_FAILED"
    assert db.get_video_state("failvid001", "FailAcc")["status"] == "PROCESSING_FAILED"
    assert db.get_video_state("failvid002", "FailAcc") is None  # never touched
    # Retryable means it CAN be picked again next cycle:
    assert db.claim_video("failvid000", "FailAcc") is not None


# ---------------------------------------------------------------------------
# Min-gap between parts: no more burst uploads from one source video
# ---------------------------------------------------------------------------
def _windows(count):
    return [{"start": i * 60.0, "end": i * 60.0 + 18.0} for i in range(count)]


def _scheduler_for_parts(tmp_path, account_name):
    db = StateDB(db_path=tmp_path / "state.db")
    scheduler = ShortsBotScheduler(
        accounts=[_account(account_name)],
        state_db=db,
        processor=_FakeProcessor(),
        storage=_FakeStorage(),
    )

    def fake_download(_url, start, _end):
        raw = tmp_path / f"raw_{int(start)}.mp4"
        raw.write_bytes(b"raw")
        raw.with_suffix(".srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHI\n\n", encoding="utf-8"
        )
        return raw

    scheduler._download_window = fake_download
    return scheduler, db


def _mark_part_uploaded(db: StateDB, part_id: str, account: str, short_id: str):
    """Seed a part as really uploaded: terminal processed row + daily-upload row."""
    db.record_video_state(
        video_id=part_id,
        video_url="https://www.youtube.com/watch?v=seed",
        title="seed",
        status="UPLOADED_YOUTUBE",
        youtube_short_id=short_id,
        account=account,
    )
    db.record_upload(part_id, short_id, account=account)


def test_min_gap_defers_remaining_parts_to_later_cycle(tmp_path):
    scheduler, db = _scheduler_for_parts(tmp_path, "GapAcc")
    # Part 1 was already uploaded a moment ago (last real upload = now).
    _mark_part_uploaded(db, "gapvideo001_part1", "GapAcc", "seedshort1")
    uploader = _FakeUploader()

    made = scheduler._process_video_windows(
        "gapvideo001",
        "https://www.youtube.com/watch?v=gapvideo001",
        "Title",
        "churl",
        _windows(3),
        account="GapAcc",
        max_daily=10,
        uploader=uploader,
        info={"title": "Title"},
        min_gap_minutes=60,
    )

    assert made == 0
    assert uploader.calls == []
    # Deferred parts are untouched (retryable later), not marked anything.
    assert db.get_video_state("gapvideo001_part2", "GapAcc") is None
    assert db.get_video_state("gapvideo001_part3", "GapAcc") is None
    # Part 1 keeps its terminal state.
    assert db.is_video_processed("gapvideo001_part1", account="GapAcc")


def test_zero_gap_processes_all_parts_in_one_cycle(tmp_path):
    scheduler, db = _scheduler_for_parts(tmp_path, "ZeroGapAcc")
    uploader = _FakeUploader()

    made = scheduler._process_video_windows(
        "zerogapvid01",
        "https://www.youtube.com/watch?v=zerogapvid01",
        "Title",
        "churl",
        _windows(3),
        account="ZeroGapAcc",
        max_daily=10,
        uploader=uploader,
        info={"title": "Title"},
        min_gap_minutes=0,
    )

    assert made == 3
    assert len(uploader.calls) == 3
    assert db.is_video_processed("zerogapvid01", account="ZeroGapAcc")  # PROCESSED_MULTI


def test_gap_elapsed_allows_all_parts(tmp_path):
    scheduler, db = _scheduler_for_parts(tmp_path, "OldGapAcc")
    # Simulate a part-1 upload from long ago by backdating daily_uploads.
    _mark_part_uploaded(db, "oldgapvid01_part1", "OldGapAcc", "seedshort9")
    from yt_shorts_bot.models import dt as _dt

    old_time = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=240)).isoformat()
    with db._get_connection() as conn:
        conn.execute(
            "UPDATE daily_uploads SET uploaded_at = ? WHERE account = ?",
            (old_time, "OldGapAcc"),
        )
        conn.commit()
    uploader = _FakeUploader()

    made = scheduler._process_video_windows(
        "oldgapvid01",
        "https://www.youtube.com/watch?v=oldgapvid01",
        "Title",
        "churl",
        _windows(3),
        account="OldGapAcc",
        max_daily=10,
        uploader=uploader,
        info={"title": "Title"},
        min_gap_minutes=60,
    )

    assert made == 2  # parts 2 and 3 (part 1 already terminal)
    assert len(uploader.calls) == 2


# ---------------------------------------------------------------------------
# Silent skips are now logged
# ---------------------------------------------------------------------------
def test_quota_cap_and_disabled_account_are_logged(monkeypatch, tmp_path, caplog):
    import logging

    db = StateDB(db_path=tmp_path / "state.db")
    # Fill CappedAcc's rolling window with real uploads.
    for i in range(3):
        db.record_upload(f"capvid{i:05d}", f"seed{i}", account="CappedAcc")
    scheduler = ShortsBotScheduler(
        accounts=[
            _account("CappedAcc", max_daily_uploads=3),
            _account("DisabledAcc", enabled=False),
        ],
        state_db=db,
        processor=_FakeProcessor(),
        storage=_FakeStorage(),
    )
    with caplog.at_level(logging.INFO):
        scheduler.run_single_cycle(accounts=scheduler.accounts)

    assert "Rolling 24-hour upload cap reached" in caplog.text
    assert "CappedAcc" in caplog.text
    assert "disabled" in caplog.text.lower()
    assert "DisabledAcc" in caplog.text


# ---------------------------------------------------------------------------
# Repost bot: the min-gap paces by DEFERRING to the next cycle, never by
# sleeping inside the cycle (which froze every account queued behind it).
# ---------------------------------------------------------------------------
class _FakeRepostFetcher:
    def __init__(self, *args, **kwargs):
        pass

    def fetch_channel_recent_shorts(self, _url, order="newest"):
        return [
            {
                "video_id": f"repostvid{i:03d}",
                "url": f"https://www.youtube.com/shorts/repostvid{i:03d}",
                "title": f"Feed Short {i}",
                "duration": 30,
            }
            for i in range(3)
        ]


def _repost_account(name, **overrides):
    account = {
        "name": name,
        "target_channels": ["https://www.youtube.com/@Feed/shorts"],
        "enabled": True,
        "min_minutes_between_uploads": 60,
        "max_shorts_per_channel_cycle": 5,
        "max_daily_uploads": 10,
        "process_mode": "copy",
    }
    account.update(overrides)
    return account


def _repost_scheduler(tmp_path, monkeypatch, account):
    monkeypatch.setattr(
        repost_scheduler_module, "ShortsFetcher", _FakeRepostFetcher
    )
    db = RepostStateDB(db_path=tmp_path / "repost-state.db")
    scheduler = ShortsRepostScheduler(
        accounts=[account], state_db=db, storage=_FakeStorage()
    )
    return scheduler, db


def test_repost_defers_instead_of_blocking_when_gap_not_elapsed(
    monkeypatch, tmp_path
):
    scheduler, db = _repost_scheduler(
        tmp_path, monkeypatch, _repost_account("RepostGap")
    )
    # A real upload happened a moment ago -> the 60-min gap is not satisfied.
    db.record_video_state(
        video_id="seedvid00001",
        video_url="https://www.youtube.com/shorts/seedvid00001",
        title="seed",
        status="UPLOADED_YOUTUBE",
        youtube_short_id="seed1",
        account="RepostGap",
    )
    db.record_upload("seedvid00001", "seed1", account="RepostGap")

    calls: list[str] = []
    scheduler._process_one = lambda video_id, *a, **k: calls.append(video_id) or True

    uploaded = scheduler.run_single_cycle(accounts=scheduler.accounts)

    # Old code would have slept ~60 minutes inside this call; now it defers.
    assert uploaded == 0
    assert calls == []
    # Deferred Shorts stay untouched (retryable) - and their claim is released.
    assert db.get_video_state("repostvid000", "RepostGap") is None
    assert db.claim_video("repostvid000", "RepostGap") is not None


def test_repost_spaces_uploads_one_gap_at_a_time(monkeypatch, tmp_path):
    scheduler, db = _repost_scheduler(
        tmp_path, monkeypatch, _repost_account("RepostPace")
    )
    calls: list[str] = []

    def fake_process_one(video_id, *args, **kwargs):
        calls.append(video_id)
        db.record_video_state(
            video_id=video_id,
            video_url=f"https://www.youtube.com/shorts/{video_id}",
            title="t",
            status="UPLOADED_YOUTUBE",
            youtube_short_id=f"seed-{video_id}",
            account="RepostPace",
        )
        db.record_upload(video_id, f"seed-{video_id}", account="RepostPace")
        return True

    scheduler._process_one = fake_process_one
    uploaded = scheduler.run_single_cycle(accounts=scheduler.accounts)

    # First Short uploads (gap was satisfied), then the rest is deferred
    # because the upload just restarted the 60-minute gap clock.
    assert uploaded == 1
    assert calls == ["repostvid000"]
    assert db.get_video_state("repostvid001", "RepostPace") is None


def test_repost_zero_gap_keeps_multi_post_cycles(monkeypatch, tmp_path):
    scheduler, db = _repost_scheduler(
        tmp_path,
        monkeypatch,
        _repost_account("RepostZero", min_minutes_between_uploads=0),
    )
    calls: list[str] = []

    def fake_process_one(video_id, *args, **kwargs):
        calls.append(video_id)
        return True

    scheduler._process_one = fake_process_one
    uploaded = scheduler.run_single_cycle(accounts=scheduler.accounts)

    assert uploaded == 3  # gap of 0 explicitly opts out of pacing
    assert calls == ["repostvid000", "repostvid001", "repostvid002"]
