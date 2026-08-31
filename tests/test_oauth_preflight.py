"""OAuth refresh-token preflight tests for both bot variants."""
from __future__ import annotations

import pytest

import yt_shorts_bot.scheduler as clip_scheduler_module
import yt_shorts_bot.uploader as clip_uploader_module
import yt_shorts_bot.webui as clip_webui
import yt_shorts_repost_bot.scheduler as repost_scheduler_module
import yt_shorts_repost_bot.uploader as repost_uploader_module
import yt_shorts_repost_bot.webui as repost_webui
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler
from yt_shorts_repost_bot.scheduler import ShortsRepostScheduler


class _ChannelService:
    def __init__(self, channel_id="UC-right", title="Right Channel"):
        self.channel_id = channel_id
        self.title = title
        self.list_kwargs = None

    def channels(self):
        return self

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return self

    def execute(self):
        return {
            "items": [
                {
                    "id": self.channel_id,
                    "snippet": {"title": self.title},
                }
            ]
        }


@pytest.mark.parametrize(
    "uploader_module",
    [clip_uploader_module, repost_uploader_module],
)
def test_preflight_forces_refresh_even_when_access_token_is_still_valid(
    monkeypatch, tmp_path, uploader_module
):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    service = _ChannelService()

    class FakeCredentials:
        valid = True
        expired = False
        refresh_token = "refresh-token"
        refresh_calls = 0

        def refresh(self, _request):
            self.refresh_calls += 1

        def to_json(self):
            return '{"refreshed": true}'

    creds = FakeCredentials()
    monkeypatch.setattr(
        uploader_module.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: creds,
    )
    monkeypatch.setattr(uploader_module, "Request", lambda: object())
    monkeypatch.setattr(uploader_module, "build", lambda *_args, **_kwargs: service)

    uploader = uploader_module.YouTubeUploader(
        client_secret_file=tmp_path / "client.json",
        token_file=token,
        state_db=object(),
    )
    ok, detail = uploader.check_connection(expected_channel_id="UC-right")

    assert ok is True
    assert "Right Channel" in detail
    assert creds.refresh_calls == 1
    assert token.read_text(encoding="utf-8") == '{"refreshed": true}'
    assert service.list_kwargs == {"part": "id,snippet", "mine": True}


@pytest.mark.parametrize(
    "uploader_module",
    [clip_uploader_module, repost_uploader_module],
)
def test_preflight_reports_dead_refresh_token_without_opening_login(
    monkeypatch, tmp_path, uploader_module
):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")

    class DeadCredentials:
        valid = True
        expired = False
        refresh_token = "dead-refresh-token"

        def refresh(self, _request):
            raise RuntimeError("invalid_grant: token expired or revoked")

    monkeypatch.setattr(
        uploader_module.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: DeadCredentials(),
    )
    monkeypatch.setattr(uploader_module, "Request", lambda: object())
    monkeypatch.setattr(
        uploader_module,
        "build",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("API client must not be built after refresh failure")
        ),
    )

    uploader = uploader_module.YouTubeUploader(
        client_secret_file=tmp_path / "client.json",
        token_file=token,
        state_db=object(),
    )
    ok, detail = uploader.check_connection()

    assert ok is False
    assert "refresh failed" in detail.lower()
    assert "Connect / Test YouTube" in detail
    assert "invalid_grant" in uploader.last_auth_error


@pytest.mark.parametrize(
    "uploader_module",
    [clip_uploader_module, repost_uploader_module],
)
def test_preflight_rejects_wrong_destination_channel(monkeypatch, uploader_module):
    uploader = uploader_module.YouTubeUploader(state_db=object())
    monkeypatch.setattr(
        uploader,
        "_get_authenticated_service",
        lambda **_kwargs: _ChannelService(channel_id="UC-other", title="Other Channel"),
    )

    ok, detail = uploader.check_connection(expected_channel_id="UC-right")

    assert ok is False
    assert "Other Channel" in detail
    assert "correct Google account" in detail


@pytest.mark.parametrize(
    "webui,uploader_module",
    [
        (clip_webui, clip_uploader_module),
        (repost_webui, repost_uploader_module),
    ],
)
def test_panel_preflight_names_the_account_that_needs_reconnection(
    monkeypatch, tmp_path, webui, uploader_module
):
    class FakeUploader:
        def __init__(self, token_file, **_kwargs):
            self.token_file = token_file

        def check_connection(self, **_kwargs):
            if "dead" in str(self.token_file):
                return False, "refresh token expired"
            return True, "channel check passed"

    monkeypatch.setattr(webui, "DRY_RUN", False)
    monkeypatch.setattr(webui, "StateDB", lambda: object())
    monkeypatch.setattr(uploader_module, "YouTubeUploader", FakeUploader)
    monkeypatch.setattr(
        uploader_module,
        "resolve_credentials",
        lambda account: (
            tmp_path / f"{account['name']}-client.json",
            tmp_path / f"{account['name']}-token.json",
        ),
    )
    accounts = [
        {"name": "good", "enabled": True, "target_channels": ["source"]},
        {"name": "dead", "enabled": True, "target_channels": ["source"]},
        {"name": "disabled", "enabled": False, "target_channels": ["source"]},
    ]

    ok, detail = webui._preflight_enabled_accounts(accounts)

    assert ok is False
    assert "dead: refresh token expired" in detail
    assert "disabled" not in detail


@pytest.mark.parametrize("webui", [clip_webui, repost_webui])
def test_scheduler_start_is_blocked_when_preflight_fails(monkeypatch, tmp_path, webui):
    accounts = [
        {
            "name": "Testing token account",
            "enabled": True,
            "target_channels": ["https://www.youtube.com/@source/shorts"],
        }
    ]
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text('{"accounts": []}', encoding="utf-8")
    monkeypatch.setattr(webui, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(webui, "ACCOUNTS", accounts)
    monkeypatch.setattr(webui, "_scheduler_thread", None)
    monkeypatch.setattr(
        webui,
        "_preflight_enabled_accounts",
        lambda: (False, "Testing token account must reconnect"),
    )

    response = webui.create_app(testing=True).test_client().post("/api/scheduler/start")

    assert response.status_code == 302
    assert "must+reconnect" in response.headers["Location"]
    assert webui._scheduler_thread is None


@pytest.mark.parametrize(
    "scheduler_module,scheduler_class,fetcher_name",
    [
        (clip_scheduler_module, ShortsBotScheduler, "YouTubeFetcher"),
        (repost_scheduler_module, ShortsRepostScheduler, "ShortsFetcher"),
    ],
)
def test_cycle_checks_oauth_before_scanning_sources(
    monkeypatch, tmp_path, scheduler_module, scheduler_class, fetcher_name
):
    scanned = []

    class RejectingUploader:
        dry_run = False

        def __init__(self, **_kwargs):
            pass

        def check_connection(self, **_kwargs):
            return False, "refresh token expired"

    class ForbiddenFetcher:
        def __init__(self, **_kwargs):
            pass

        def fetch_channel_recent_videos(self, *_args, **_kwargs):
            scanned.append(True)
            return []

        def fetch_channel_recent_shorts(self, *_args, **_kwargs):
            scanned.append(True)
            return []

    monkeypatch.setattr(scheduler_module, "YouTubeUploader", RejectingUploader)
    monkeypatch.setattr(scheduler_module, fetcher_name, ForbiddenFetcher)
    account = {
        "name": "A",
        "enabled": True,
        "target_channels": ["https://www.youtube.com/@source/shorts"],
        "connected_channel_id": "UC-right",
    }
    scheduler = scheduler_class(
        accounts=[account],
        state_db=StateDB(tmp_path / f"{fetcher_name}.db"),
    )

    assert scheduler.run_single_cycle(preflight_oauth=True) == 0
    assert scanned == []
