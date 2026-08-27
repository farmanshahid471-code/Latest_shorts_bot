from __future__ import annotations

import json
from datetime import datetime, timezone

from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler
from yt_shorts_repost_bot.scheduler import ShortsRepostScheduler
from yt_shorts_repost_bot.timewindows import (
    US_TIMEZONES,
    is_within_posting_window,
    seconds_until_posting_window,
    validate_posting_window,
)
from yt_shorts_repost_bot import webui


def pacific_account(start="05:00", end="17:00"):
    return {
        "name": "Pacific",
        "posting_timezone": "America/Los_Angeles",
        "posting_start_time": start,
        "posting_end_time": end,
    }


def test_pacific_window_follows_standard_and_daylight_time():
    account = pacific_account()
    # January: Pacific is UTC-8.
    assert is_within_posting_window(
        account, datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
    )
    assert not is_within_posting_window(
        account, datetime(2026, 1, 16, 1, 0, tzinfo=timezone.utc)
    )
    # July: Pacific is UTC-7. The same 5 AM local opening is one UTC hour earlier.
    assert is_within_posting_window(
        account, datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    )
    assert not is_within_posting_window(
        account, datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    )


def test_overnight_equal_and_disabled_windows():
    overnight = pacific_account("17:00", "05:00")
    assert is_within_posting_window(
        overnight, datetime(2026, 1, 16, 4, 0, tzinfo=timezone.utc)  # 8 PM
    )
    assert is_within_posting_window(
        overnight, datetime(2026, 1, 16, 10, 0, tzinfo=timezone.utc)  # 2 AM
    )
    assert not is_within_posting_window(
        overnight, datetime(2026, 1, 16, 20, 0, tzinfo=timezone.utc)  # noon
    )
    assert is_within_posting_window(pacific_account("05:00", "05:00"))
    assert is_within_posting_window({"name": "Always"})


def test_seconds_until_next_pacific_opening():
    account = pacific_account()
    # 4 AM Pacific in January -> one hour until the 5 AM opening.
    delay = seconds_until_posting_window(
        account, datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    )
    assert delay == 3600


def test_invalid_or_partial_window_fails_closed():
    partial = {
        "posting_timezone": "America/Los_Angeles",
        "posting_start_time": "05:00",
        "posting_end_time": "",
    }
    assert validate_posting_window(partial)
    assert not is_within_posting_window(partial)
    invalid = pacific_account()
    invalid["posting_timezone"] = "Europe/London"
    assert validate_posting_window(invalid)
    assert not is_within_posting_window(invalid)


def test_all_us_time_zone_regions_are_available():
    keys = {key for _label, key in US_TIMEZONES}
    assert {
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Phoenix",
        "America/Los_Angeles",
        "America/Anchorage",
        "America/Adak",
        "Pacific/Honolulu",
        "America/Puerto_Rico",
        "Pacific/Pago_Pago",
        "Pacific/Guam",
    } == keys


def test_scheduler_skips_account_outside_window(monkeypatch, tmp_path):
    account = {
        **pacific_account(),
        "target_channels": ["https://www.youtube.com/@Owned/shorts"],
        "enabled": True,
    }
    scheduler = ShortsRepostScheduler(
        accounts=[account], state_db=StateDB(tmp_path / "state.db")
    )
    monkeypatch.setattr(
        "yt_shorts_repost_bot.scheduler.is_within_posting_window", lambda _account: False
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("source scan must not run outside the account window")

    monkeypatch.setattr(
        "yt_shorts_repost_bot.scheduler.ShortsFetcher.fetch_channel_recent_shorts",
        forbidden,
    )
    assert scheduler.run_single_cycle() == 0


def test_clip_scheduler_also_skips_outside_window(monkeypatch, tmp_path):
    account = {
        **pacific_account(),
        "target_channels": ["https://www.youtube.com/@Owned/videos"],
        "enabled": True,
    }
    scheduler = ShortsBotScheduler(
        accounts=[account], state_db=StateDB(tmp_path / "clip-state.db")
    )
    monkeypatch.setattr(
        "yt_shorts_bot.scheduler.is_within_posting_window", lambda _account: False
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("clip scan must not run outside the account window")

    monkeypatch.setattr(
        "yt_shorts_bot.scheduler.YouTubeFetcher.fetch_channel_recent_videos",
        forbidden,
    )
    assert scheduler.run_single_cycle() == 0


def test_scheduler_wakes_for_window_before_global_interval(monkeypatch, tmp_path):
    account = {**pacific_account(), "enabled": True}
    scheduler = ShortsRepostScheduler(
        accounts=[account], state_db=StateDB(tmp_path / "state.db")
    )
    monkeypatch.setattr(
        "yt_shorts_repost_bot.scheduler.is_within_posting_window", lambda _account: False
    )
    monkeypatch.setattr(
        "yt_shorts_repost_bot.scheduler.seconds_until_posting_window",
        lambda _account: 300.0,
    )
    assert scheduler._next_wait_seconds(interval_hours=3) == 300.0


def test_webui_saves_and_renders_posting_window(monkeypatch, tmp_path):
    account = {"name": "A", "target_channels": [], "enabled": True}
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps({"accounts": [account]}), encoding="utf-8")
    monkeypatch.setattr(webui, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(webui, "ACCOUNTS", [account])
    database = StateDB(tmp_path / "state.db")
    monkeypatch.setattr(webui, "StateDB", lambda: database)
    client = webui.create_app(testing=True).test_client()

    response = client.post(
        "/api/account-settings/save",
        data={
            "account": "A",
            "posting_timezone": "America/Los_Angeles",
            "posting_start_time": "05:00",
            "posting_end_time": "17:00",
        },
    )
    assert response.status_code == 302
    saved = json.loads(accounts_file.read_text())["accounts"][0]
    assert saved["posting_timezone"] == "America/Los_Angeles"
    assert saved["posting_start_time"] == "05:00"
    assert saved["posting_end_time"] == "17:00"

    html = client.get("/?account=A").get_data(as_text=True)
    assert "Automatic posting time zone" in html
    assert 'value="America/Los_Angeles" selected' in html
    for _label, key in US_TIMEZONES:
        assert f'value="{key}"' in html


def test_webui_rejects_partial_posting_window(monkeypatch, tmp_path):
    account = {"name": "A", "target_channels": [], "enabled": True}
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps({"accounts": [account]}), encoding="utf-8")
    monkeypatch.setattr(webui, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(webui, "ACCOUNTS", [account])
    monkeypatch.setattr(webui, "StateDB", lambda: StateDB(tmp_path / "state.db"))
    client = webui.create_app(testing=True).test_client()
    response = client.post(
        "/api/account-settings/save",
        data={
            "account": "A",
            "posting_timezone": "America/Los_Angeles",
            "posting_start_time": "05:00",
            "posting_end_time": "",
        },
    )
    assert "Both+posting+start+and+end+times" in response.headers["Location"]
    saved = json.loads(accounts_file.read_text())["accounts"][0]
    assert "posting_timezone" not in saved
