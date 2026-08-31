"""YouTube-side scheduled publishing (`publishAt`) tests.

When an account enables ``spread_uploads_across_window``, uploads go up as
PRIVATE with a YouTube publish time spaced evenly from *now* until the
posting window ends (not from the configured window start), and local gap
pacing is skipped because YouTube does the publishing even when the bot is
offline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import yt_shorts_bot.scheduler as clip_scheduler_module
import yt_shorts_repost_bot.scheduler as repost_scheduler_module
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler
from yt_shorts_bot.uploader import YouTubeUploader as ClipUploader
from yt_shorts_repost_bot.models import StateDB as RepostStateDB
from yt_shorts_repost_bot.scheduler import ShortsRepostScheduler
from yt_shorts_repost_bot.uploader import YouTubeUploader as RepostUploader

CT = "America/Chicago"
# 2026-08-29 13:20 UTC == 08:20 Central (CDT, UTC-5)
NOW = datetime(2026, 8, 29, 13, 20, tzinfo=timezone.utc)
WINDOW_END_UTC = datetime(2026, 8, 29, 22, 0, tzinfo=timezone.utc)  # 17:00 CT


class _StubState:
    """Minimal stand-in for the two queries the planner makes."""

    def __init__(self, used=0, first=None):
        self.used = used
        self.first = first

    def get_uploads_in_last_24_hours(self, account=None):
        return self.used

    def get_first_upload_time(self, account=""):
        return self.first


def _window_account(**overrides):
    account = {
        "name": "SchedAcc",
        "spread_uploads_across_window": True,
        "posting_timezone": CT,
        "posting_start_time": "06:00",
        "posting_end_time": "17:00",
    }
    account.update(overrides)
    return account


def _clip_scheduler(state):
    scheduler = ShortsBotScheduler(accounts=[])
    scheduler.state_db = state
    return scheduler


def _repost_scheduler(state):
    scheduler = ShortsRepostScheduler(accounts=[])
    scheduler.state_db = state
    return scheduler


# ---------------------------------------------------------------------------
# Planner behavior
# ---------------------------------------------------------------------------
def test_spread_toggle_requires_valid_non_24h_window():
    sched = _clip_scheduler(_StubState())
    assert sched._spread_scheduling_enabled(_window_account())
    assert not sched._spread_scheduling_enabled(_window_account(spread_uploads_across_window=False))
    assert not sched._spread_scheduling_enabled(_window_account(posting_timezone=""))
    assert not sched._spread_scheduling_enabled(
        _window_account(posting_start_time="06:00", posting_end_time="06:00")
    )


def test_plan_starts_from_now_not_window_start():
    # Bot starts at 08:20 with a 06:00-17:00 window: the first publish time is
    # anchored to NOW (+15 min processing buffer), nothing is planned in the
    # past, and the whole day still fits before the window ends.
    sched = _clip_scheduler(_StubState(used=0))
    publish = sched._plan_publish_at(_window_account(), max_daily=6, now_utc=NOW)
    assert publish is not None
    assert publish == NOW + timedelta(minutes=15)


def test_plan_slots_stay_increasing_and_inside_window():
    first = NOW - timedelta(minutes=10)  # first upload today at ~08:10 CT
    for used in range(6):
        sched = _clip_scheduler(_StubState(used=used, first=first))
        publish = sched._plan_publish_at(_window_account(), max_daily=6, now_utc=NOW)
        assert publish is not None, f"slot {used} fell back to immediate"
        assert publish <= WINDOW_END_UTC, f"slot {used} scheduled after window end"
        assert publish > first, f"slot {used} is before the first upload"


def test_plan_aborts_when_window_is_nearly_over():
    late = datetime(2026, 8, 29, 21, 58, tzinfo=timezone.utc)  # 16:58 CT
    sched = _clip_scheduler(_StubState())
    assert sched._plan_publish_at(_window_account(), max_daily=6, now_utc=late) is None


def test_plan_handles_overnight_window_pre_and_post_midnight():
    overnight = _window_account(posting_start_time="20:00", posting_end_time="06:00")
    sched = _clip_scheduler(_StubState())
    # 23:00 CT pre-midnight -> end is TOMORROW 06:00.
    pre = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    publish = sched._plan_publish_at(overnight, max_daily=6, now_utc=pre)
    assert publish is not None and publish <= datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc)
    # 03:00 CT post-midnight -> end is TODAY 06:00.
    post = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    publish = sched._plan_publish_at(overnight, max_daily=6, now_utc=post)
    assert publish is not None and publish <= datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc)


def test_plan_disabled_returns_none():
    sched = _clip_scheduler(_StubState())
    account = _window_account(spread_uploads_across_window=False)
    assert sched._plan_publish_at(account, max_daily=6, now_utc=NOW) is None


def test_repost_planner_matches_clip_planner():
    first = NOW - timedelta(minutes=10)
    clip = _clip_scheduler(_StubState(used=2, first=first))._plan_publish_at(
        _window_account(), 6, NOW
    )
    repost = _repost_scheduler(_StubState(used=2, first=first))._plan_publish_at(
        _window_account(), 6, NOW
    )
    assert repost == clip and repost is not None


# ---------------------------------------------------------------------------
# Uploader request body
# ---------------------------------------------------------------------------
class _FakeRequest:
    def next_chunk(self, num_retries=0):
        return None, {"id": "abc123short"}


class _FakeService:
    def __init__(self):
        self.body = None

    def channels(self):
        return self

    def list(self, part=None, mine=None):
        class _Exec:
            def execute(self):
                return {"items": [{"id": "UCFAKE", "snippet": {"title": "Fake"}}]}

        return _Exec()

    def videos(self):
        return self

    def insert(self, part=None, body=None, media_body=None):
        self.body = body
        return _FakeRequest()


def _real_uploader_cls(uploader_cls, tmp_path):
    db = StateDB(db_path=tmp_path / "u.db") if uploader_cls is ClipUploader else RepostStateDB(db_path=tmp_path / "u.db")
    service = _FakeService()
    uploader = uploader_cls(
        client_secret_file=tmp_path / "missing-secret.json",
        token_file=tmp_path / "missing-token.json",
        state_db=db,
    )
    uploader.youtube_service = service
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 2048)
    return uploader, service, video


def test_upload_body_is_private_with_publish_at_when_scheduled(tmp_path):
    for uploader_cls in (ClipUploader, RepostUploader):
        uploader, service, video = _real_uploader_cls(uploader_cls, tmp_path)
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        result = uploader.upload_short(
            video_path=video,
            original_video_id=f"vid-{uploader_cls.__module__}",
            original_title="T",
            account="SchedAcc",
            account_max_daily=6,
            info={"title": "T"},
            expected_channel_id="UCFAKE",
            publish_at=future,
        )
        assert result == "abc123short"
        status = service.body["status"]
        assert status["privacyStatus"] == "private"
        assert status["publishAt"] == future.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_upload_body_stays_public_without_publish_at(tmp_path):
    for uploader_cls in (ClipUploader, RepostUploader):
        uploader, service, video = _real_uploader_cls(uploader_cls, tmp_path)
        result = uploader.upload_short(
            video_path=video,
            original_video_id=f"vid2-{uploader_cls.__module__}",
            original_title="T",
            account="SchedAcc2",
            account_max_daily=6,
            info={"title": "T"},
            expected_channel_id="UCFAKE",
        )
        assert result == "abc123short"
        assert service.body["status"]["privacyStatus"] == "public"
        assert "publishAt" not in service.body["status"]


# ---------------------------------------------------------------------------
# Scheduler integration: spread ON means no local gap blocking, parts get
# publish times; OFF keeps old pacing.
# ---------------------------------------------------------------------------
class _FakeProcessor:
    def process_clip_to_short(self, raw_path, output_path=None, **_kwargs):
        from pathlib import Path

        output = Path(output_path)
        output.write_bytes(b"processed")
        return output


class _FakeStorage:
    client = None

    def upload_file(self, _path, r2_key=None):
        return None

    @staticmethod
    def cleanup_local_files(*_paths):
        return None


def test_clip_cycle_with_spread_uploads_all_parts_immediately(monkeypatch, tmp_path):
    class _FakeFetcher:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_channel_recent_videos(self, _channel_url, order="newest"):
            return [
                {
                    "video_id": "spreadvid01",
                    "url": "https://www.youtube.com/watch?v=spreadvid01",
                    "title": "Good VOD",
                    "duration": 400,
                }
            ]

        def select_top_windows(self, _url, count=1):
            return [
                {"start": 0.0, "end": 18.0, "score": 0.9},
                {"start": 60.0, "end": 78.0, "score": 0.8},
                {"start": 120.0, "end": 138.0, "score": 0.7},
            ]

        def extract_heatmap_and_select_window(self, _url):
            return ({"title": "Good VOD"}, 10.0, 1.0, 19.0)

    monkeypatch.setattr(clip_scheduler_module, "YouTubeFetcher", _FakeFetcher)
    account = _window_account(
        target_channels=["https://www.youtube.com/@S/streams"],
        shorts_per_video=3,
        min_minutes_between_uploads=60,  # would block/wait if spread were OFF
        max_daily_uploads=6,
        # window that is always open so the test works at any wall-clock time
        posting_start_time="00:00",
        posting_end_time="23:59",
    )
    db = StateDB(db_path=tmp_path / "state.db")
    # an upload just happened -> min_gap=60 is NOT satisfied right now
    db.record_video_state(
        video_id="spreadseed01",
        video_url="https://www.youtube.com/watch?v=spreadseed01",
        title="seed",
        status="UPLOADED_YOUTUBE",
        youtube_short_id="seedx",
        account="SchedAcc",
    )
    db.record_upload("spreadseed01", "seedx", account="SchedAcc")

    captured = []

    def fake_uploader_factory(**_kwargs):
        class _U:
            last_metadata = None

            def upload_short(self, **kwargs):
                captured.append(kwargs)
                vid = kwargs["original_video_id"]
                db.record_video_state(
                    video_id=vid,
                    video_url=kwargs["original_url"],
                    title="t",
                    status="UPLOADED_YOUTUBE",
                    youtube_short_id=f"seed-{vid}",
                    account=kwargs["account"],
                )
                db.record_upload(vid, f"seed-{vid}", account=kwargs["account"])
                return f"seed-{vid}"

        return _U()

    monkeypatch.setattr(clip_scheduler_module, "YouTubeUploader", fake_uploader_factory)
    scheduler = ShortsBotScheduler(
        accounts=[account],
        state_db=db,
        processor=_FakeProcessor(),
        storage=_FakeStorage(),
    )

    def fake_download(_url, start, _end):
        raw = tmp_path / f"raw_{int(start)}.mp4"
        raw.write_bytes(b"raw")
        return raw

    scheduler._download_window = fake_download
    uploaded = scheduler.run_single_cycle(accounts=scheduler.accounts)

    # All 3 parts uploaded in ONE cycle: with spread ON the bot does not wait
    # for the local min gap - YouTube spaces the public times out instead.
    assert uploaded == 3
    publish_times = [call.get("publish_at") for call in captured]
    assert all(t is not None for t in publish_times)


def test_repost_spread_skips_deferral_and_attaches_publish_at(monkeypatch, tmp_path):
    class _FakeRepostFetcher:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_channel_recent_shorts(self, _url, order="newest"):
            return [
                {
                    "video_id": "repostspread1",
                    "url": "https://www.youtube.com/shorts/repostspread1",
                    "title": "Feed Short",
                    "duration": 30,
                }
            ]

    monkeypatch.setattr(
        repost_scheduler_module, "ShortsFetcher", _FakeRepostFetcher
    )
    account = _window_account(
        name="RepostSpread",
        target_channels=["https://www.youtube.com/@F/shorts"],
        min_minutes_between_uploads=60,
        posting_start_time="00:00",
        posting_end_time="23:59",
    )
    db = RepostStateDB(db_path=tmp_path / "repost-state.db")
    db.record_video_state(
        video_id="repostseed001",
        video_url="https://www.youtube.com/shorts/repostseed001",
        title="seed",
        status="UPLOADED_YOUTUBE",
        youtube_short_id="seedy",
        account="RepostSpread",
    )
    db.record_upload("repostseed001", "seedy", account="RepostSpread")

    captured = []
    scheduler = ShortsRepostScheduler(
        accounts=[account], state_db=db, storage=_FakeStorage()
    )

    def fake_process_one(video_id, *args, **kwargs):
        captured.append(kwargs.get("publish_at"))
        return True

    scheduler._process_one = fake_process_one
    uploaded = scheduler.run_single_cycle(accounts=scheduler.accounts)

    # Gap is active (last upload = now) but spread defers to YouTube instead.
    assert uploaded == 1
    assert captured and captured[0] is not None
