from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler
from yt_shorts_bot.pathutils import (
    credential_path,
    relative_credential_value,
    safe_account_slug,
)
from yt_shorts_repost_bot.scheduler import ShortsRepostScheduler
from yt_shorts_repost_bot.uploader import UPLOAD_QUOTA_REACHED


def test_empty_named_account_never_uses_default_sources(monkeypatch, tmp_path):
    scheduler = ShortsRepostScheduler(
        accounts=[{"name": "Empty", "target_channels": [], "enabled": True}],
        state_db=StateDB(tmp_path / "state.db"),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("fetcher must not run for an empty source list")

    monkeypatch.setattr(
        "yt_shorts_repost_bot.scheduler.ShortsFetcher.fetch_channel_recent_shorts",
        forbidden,
    )
    assert scheduler.run_single_cycle() == 0


def test_clip_bot_empty_named_account_also_stays_empty(monkeypatch, tmp_path):
    scheduler = ShortsBotScheduler(
        accounts=[{"name": "Empty", "target_channels": [], "enabled": True}],
        state_db=StateDB(tmp_path / "clip-state.db"),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("clip fetcher must not run for an empty source list")

    monkeypatch.setattr(
        "yt_shorts_bot.scheduler.YouTubeFetcher.fetch_channel_recent_videos",
        forbidden,
    )
    assert scheduler.run_single_cycle() == 0


def test_accounts_reload_before_each_background_cycle(monkeypatch, tmp_path):
    scheduler = ShortsRepostScheduler(
        accounts=None, state_db=StateDB(tmp_path / "state.db")
    )
    fresh = [{"name": "Fresh", "target_channels": [], "enabled": True}]
    monkeypatch.setattr(scheduler, "_load_accounts_fresh", lambda: fresh)
    scheduler.run_single_cycle()
    assert scheduler.accounts == fresh


def test_stop_interrupts_initial_loop_and_wait(monkeypatch, tmp_path):
    scheduler = ShortsRepostScheduler(
        interval_hours=24,
        accounts=[{"name": "Empty", "target_channels": [], "enabled": True}],
        state_db=StateDB(tmp_path / "state.db"),
    )
    thread = threading.Thread(target=scheduler.start_24_7_loop)
    thread.start()
    deadline = time.monotonic() + 3
    while not scheduler._running and time.monotonic() < deadline:
        time.sleep(0.01)
    scheduler.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_foreign_windows_paths_become_portable_account_paths(tmp_path):
    resolved = credential_path(
        tmp_path,
        "New Channel 2",
        r"F:\youtube 2\accounts\new channel 2\token.json",
        "token.json",
    )
    assert resolved == tmp_path / "accounts" / "new channel 2" / "token.json"
    assert relative_credential_value("New Channel 2", "token.json") == (
        "accounts/new channel 2/token.json"
    )


def test_account_slug_blocks_traversal():
    slug = safe_account_slug("../../Danger\\Account")
    assert "/" not in slug and "\\" not in slug and ".." not in slug


class FakeFetcher:
    def __init__(self, raw: Path):
        self.raw = raw

    def download_short(self, _url):
        self.raw.write_bytes(b"raw")
        return self.raw

    def get_short_info(self, _url):
        return {"title": "Source title", "channel": "Owner"}


class FakeReprocessor:
    def __init__(self, final: Path, should_fail: bool = False):
        self.final = final
        self.should_fail = should_fail

    def process_short(self, _input, output_path=None, **_kwargs):
        target = Path(output_path) if output_path else self.final
        target.write_bytes(b"final")
        self.final = target
        if self.should_fail:
            raise RuntimeError("render broke")
        return target


class FakeStorage:
    client = None
    bucket_name = "bucket"

    def upload_file(self, _path, r2_key=None):
        return None

    @staticmethod
    def cleanup_local_files(*paths):
        for path in paths:
            if path:
                Path(path).unlink(missing_ok=True)


class QuotaUploader:
    def __init__(self):
        self.last_metadata = {
            "title": "EXACT TITLE #mine",
            "description": "EXACT DESCRIPTION",
            "tags": ["mine"],
        }

    def upload_short(self, **_kwargs):
        return UPLOAD_QUOTA_REACHED


def test_quota_result_stays_retryable_and_temp_files_are_cleaned(
    monkeypatch, tmp_path
):
    raw = tmp_path / "raw.mp4"
    final = tmp_path / "final.mp4"
    finished = tmp_path / "finished"
    finished.mkdir()
    monkeypatch.setattr("yt_shorts_repost_bot.scheduler.KEEP_LOCAL_SHORTS", True)
    monkeypatch.setattr("yt_shorts_repost_bot.scheduler.KEEP_SHORTS_DIR", finished)

    scheduler = ShortsRepostScheduler(
        accounts=[],
        state_db=StateDB(tmp_path / "state.db"),
        storage=FakeStorage(),
    )
    ok = scheduler._process_one(
        "VID12345678",
        "https://www.youtube.com/shorts/VID12345678",
        "Source title",
        "https://www.youtube.com/@Owner/shorts",
        account="My Account",
        fetcher=FakeFetcher(raw),
        reprocessor=FakeReprocessor(final),
        uploader=QuotaUploader(),
        expected_channel="My Channel",
    )
    assert ok is False
    state = scheduler.state_db.get_video_state("VID12345678", "My Account")
    assert state["status"] == "QUOTA_WAIT"
    assert not scheduler.state_db.is_video_processed("VID12345678", "My Account")
    assert not raw.exists() and not final.exists()
    assert not list(tmp_path.glob("final_*.mp4"))
    sidecar = finished / "my account_repost_VID12345678.txt"
    assert "EXACT TITLE #mine" in sidecar.read_text(encoding="utf-8")


def test_processing_exception_also_cleans_temp_files(tmp_path):
    raw = tmp_path / "raw.mp4"
    final = tmp_path / "final.mp4"
    scheduler = ShortsRepostScheduler(
        accounts=[],
        state_db=StateDB(tmp_path / "state.db"),
        storage=FakeStorage(),
    )
    with pytest.raises(RuntimeError, match="render broke"):
        scheduler._process_one(
            "VID12345678",
            "https://www.youtube.com/shorts/VID12345678",
            "Source title",
            "Owner",
            account="A",
            fetcher=FakeFetcher(raw),
            reprocessor=FakeReprocessor(final, should_fail=True),
            uploader=QuotaUploader(),
        )
    assert not raw.exists() and not final.exists()
    assert not list(tmp_path.glob("final_*.mp4"))
