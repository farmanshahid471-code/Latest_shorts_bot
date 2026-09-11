"""TikTok cross-posting: captions, uploader, idempotency, panel."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import yt_shorts_bot.social as clip_social
import yt_shorts_bot.social_tiktok as clip_tiktok
import yt_shorts_bot.webui as clip_webui
import yt_shorts_repost_bot.social as repost_social
import yt_shorts_repost_bot.webui as repost_webui
from yt_shorts_bot.models import StateDB as ClipStateDB
from yt_shorts_bot.uploader import UPLOAD_QUOTA_REACHED
from yt_shorts_repost_bot.models import StateDB as RepostStateDB

SOCIAL_MODULES = {"clip": clip_social, "repost": repost_social}
WEBUI_MODULES = {"clip": clip_webui, "repost": repost_webui}
STATE_DBS = {"clip": ClipStateDB, "repost": RepostStateDB}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeStorage:
    """Stand-in for CloudStorageManager.public_url_for_key."""

    def __init__(self, public_url=""):
        self.public_url = public_url

    def public_url_for_key(self, r2_key):
        if not r2_key or not self.public_url:
            return ""
        return f"{self.public_url}/{r2_key}"


def _ok_tiktok(payload):
    return FakeResponse({"error": {"code": "ok", "message": ""}, "data": payload})


# ---------------------------------------------------------------------------
# Caption builder + enable flags (both bots behave identically)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["clip", "repost"])
def test_build_social_caption(name):
    social = SOCIAL_MODULES[name]
    # Tags already present in the title ("cats", "funny") are not duplicated.
    metadata = {"title": "Funny Cat #cats", "tags": ["cats", "funny", "pets"]}
    assert social.build_social_caption(metadata, "tiktok") == "Funny Cat #cats #pets"


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_build_social_caption_truncates_and_handles_empty(name):
    social = SOCIAL_MODULES[name]
    long_title = "x" * 500
    assert len(social.build_social_caption({"title": long_title}, "tiktok")) == 150
    assert "New Short" in social.build_social_caption({}, "tiktok")
    assert social.build_social_caption(None, "tiktok")


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_build_social_caption_strips_tag_whitespace(name):
    social = SOCIAL_MODULES[name]
    caption = social.build_social_caption(
        {"title": "Hi", "tags": ["funny cats", "ok"]}, "tiktok"
    )
    assert "#funnycats" in caption and "funny cats" not in caption


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_short_wrapper_never_raises(name, tmp_path):
    social = SOCIAL_MODULES[name]
    db = STATE_DBS[name](tmp_path / f"{name}-wrap.db")
    assert (
        social.crosspost_short({"name": "A"}, "vid1", None, state_db=db, dry_run=True)
        == {}
    )
    assert social.crosspost_short(None, "vid1", None, state_db=db) == {}


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_is_enabled(name):
    social = SOCIAL_MODULES[name]
    assert not social.is_enabled(None, "tiktok")
    assert not social.is_enabled({}, "tiktok")
    assert not social.is_enabled({"tiktok_enabled": False}, "tiktok")
    assert social.is_enabled({"tiktok_enabled": 1}, "tiktok")


def test_social_modules_are_mirrored():
    root = Path(__file__).resolve().parents[1]
    for filename in ("social.py", "social_tiktok.py"):
        clip = (root / "yt_shorts_bot" / filename).read_text(encoding="utf-8")
        repost = (root / "yt_shorts_repost_bot" / filename).read_text(encoding="utf-8")
        assert clip == repost, f"{filename} diverged between bots"


# ---------------------------------------------------------------------------
# TikTok uploader (mocked HTTP)
# ---------------------------------------------------------------------------
def _tiktok_post_factory(calls, status="PUBLISH_COMPLETE"):
    state = {"init_calls": 0}

    def _post(url, **kwargs):
        calls.append(url)
        if url.endswith("/v2/post/publish/creator_info/query/"):
            return _ok_tiktok({"privacy_level_options": ["PUBLIC_TO_EVERYONE"]})
        if url.endswith("/v2/post/publish/video/init/"):
            state["init_calls"] += 1
            return _ok_tiktok(
                {"publish_id": "pub1", "upload_url": "https://up.example/x"}
            )
        if url.endswith("/v2/post/publish/status/fetch/"):
            return _ok_tiktok({"status": status})
        raise AssertionError(f"unexpected POST {url}")

    _post.state = state
    return _post


def _tiktok_user_info_get(url, **kwargs):
    # /v2/user/info/ is a GET endpoint with fields as query params.
    assert url.endswith("/v2/user/info/")
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["params"] == {"fields": "open_id,display_name"}
    return _ok_tiktok({"user": {"open_id": "open1", "display_name": "clipz"}})


def test_tiktok_check_connection_ok(monkeypatch):
    def _no_post(*_args, **_kwargs):
        raise AssertionError("user info must use GET, not POST")

    monkeypatch.setattr(clip_tiktok.requests, "get", _tiktok_user_info_get)
    monkeypatch.setattr(clip_tiktok.requests, "post", _no_post)
    uploader = clip_tiktok.TikTokUploader(
        open_id="open1", access_token="tok", dry_run=False
    )
    ok, detail = uploader.check_connection()
    assert ok and "clipz" in detail


def test_tiktok_check_connection_open_id_mismatch(monkeypatch):
    monkeypatch.setattr(clip_tiktok.requests, "get", _tiktok_user_info_get)
    uploader = clip_tiktok.TikTokUploader(
        open_id="someone-else", access_token="tok", dry_run=False
    )
    ok, detail = uploader.check_connection()
    assert not ok and "different TikTok user" in detail


def test_tiktok_file_upload_flow(monkeypatch, tmp_path):
    calls, puts = [], []

    def _put(url, **kwargs):
        puts.append(kwargs["headers"]["Content-Range"])
        return FakeResponse({}, 200)

    monkeypatch.setattr(clip_tiktok.requests, "post", _tiktok_post_factory(calls))
    monkeypatch.setattr(clip_tiktok.requests, "put", _put)
    video = tmp_path / "short.mp4"
    video.write_bytes(b"0" * 1024)
    uploader = clip_tiktok.TikTokUploader(
        open_id="open1", access_token="tok", dry_run=False
    )
    assert uploader.upload_video(video, title="hello") == "pub1"
    assert puts == ["bytes 0-1023/1024"]
    assert any("video/init" in url for url in calls)


def test_tiktok_pull_from_url_skips_file_upload(monkeypatch, tmp_path):
    calls = []
    seen = {}

    def _post(url, **kwargs):
        calls.append(url)
        if url.endswith("/v2/post/publish/video/init/"):
            seen.update(kwargs["json"]["source_info"])
            return _ok_tiktok({"publish_id": "pub9"})
        if url.endswith("/v2/post/publish/creator_info/query/"):
            return _ok_tiktok({})
        if url.endswith("/v2/post/publish/status/fetch/"):
            return _ok_tiktok({"status": "PUBLISH_COMPLETE"})
        raise AssertionError(url)

    def _put(*_args, **_kwargs):
        raise AssertionError("no chunk upload for URL pulls")

    monkeypatch.setattr(clip_tiktok.requests, "post", _post)
    monkeypatch.setattr(clip_tiktok.requests, "put", _put)
    uploader = clip_tiktok.TikTokUploader(
        open_id="open1", access_token="tok", dry_run=False
    )
    assert (
        uploader.upload_video(
            tmp_path / "missing.mp4",
            title="hello",
            video_url="https://cdn.example/a.mp4",
        )
        == "pub9"
    )
    assert seen == {"source": "PULL_FROM_URL", "video_url": "https://cdn.example/a.mp4"}


def test_tiktok_failed_status_raises(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        clip_tiktok.requests, "post", _tiktok_post_factory(calls, status="FAILED")
    )
    monkeypatch.setattr(clip_tiktok.requests, "put", lambda *a, **k: FakeResponse({}, 200))
    video = tmp_path / "short.mp4"
    video.write_bytes(b"0" * 8)
    uploader = clip_tiktok.TikTokUploader(
        open_id="open1", access_token="tok", dry_run=False
    )
    with pytest.raises(clip_tiktok.TikTokAPIError):
        uploader.upload_video(video, title="hello")


def test_tiktok_expired_token_refreshes_and_retries(monkeypatch, tmp_path):
    refreshed = {}

    def _post(url, **kwargs):
        if url.endswith("/v2/oauth/token/"):
            # The token endpoint requires form-encoded parameters and answers
            # with a FLAT object (no nested data.* wrapper).
            assert kwargs.get("json") is None
            assert (
                kwargs["headers"]["Content-Type"]
                == "application/x-www-form-urlencoded"
            )
            refreshed.update(kwargs["data"])
            return FakeResponse(
                {
                    "access_token": "newA",
                    "refresh_token": "newR",
                    "expires_in": 86400,
                    "open_id": "open1",
                }
            )
        if url.endswith("/v2/post/publish/creator_info/query/"):
            return _ok_tiktok({})
        if url.endswith("/v2/post/publish/video/init/"):
            if kwargs.get("headers", {}).get("Authorization") == "Bearer oldA":
                return FakeResponse(
                    {"error": {"code": "invalid_token", "message": "expired"},
                     "data": {}},
                    status_code=401,
                )
            return _ok_tiktok({"publish_id": "pub2"})
        if url.endswith("/v2/post/publish/status/fetch/"):
            return _ok_tiktok({"status": "PUBLISH_COMPLETE"})
        raise AssertionError(url)

    saved = {}
    monkeypatch.setattr(clip_tiktok.requests, "post", _post)
    uploader = clip_tiktok.TikTokUploader(
        open_id="open1",
        access_token="oldA",
        refresh_token="oldR",
        client_key="key",
        client_secret="secret",
        dry_run=False,
        on_tokens_refreshed=lambda a, r, _e: saved.update(
            {"access": a, "refresh": r}
        ),
    )
    assert (
        uploader.upload_video(
            tmp_path / "missing.mp4", title="hi", video_url="https://cdn.example/a.mp4"
        )
        == "pub2"
    )
    assert refreshed.get("grant_type") == "refresh_token"
    assert saved == {"access": "newA", "refresh": "newR"}


# ---------------------------------------------------------------------------
# Orchestrator: gating, idempotency, recording (both bots)
# ---------------------------------------------------------------------------
def _poster(name, tmp_path, monkeypatch, public_url="", dry_run=False):
    social = SOCIAL_MODULES[name]
    db = STATE_DBS[name](tmp_path / f"{name}-social.db")
    return social.SocialDestinations(
        state_db=db, storage=FakeStorage(public_url), dry_run=dry_run
    ), db


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_disabled_posts_nothing(name, tmp_path, monkeypatch):
    poster, db = _poster(name, tmp_path, monkeypatch)
    assert poster.crosspost({"name": "A"}, "vid1", None, None, {}) == {}
    assert db.get_social_posts_for_video("vid1", "A") == []


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_skips_already_posted(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch)
    db.record_social_post("vid1", "A", "tiktok", remote_id="m1", status="POSTED")

    def _boom(*_args, **_kwargs):
        raise AssertionError("already-posted must not touch the network")

    monkeypatch.setattr(social.TikTokUploader, "upload_video", _boom)
    account = {"name": "A", "tiktok_enabled": True,
               "tiktok_open_id": "1", "tiktok_access_token": "t"}
    assert poster.crosspost(account, "vid1", None, "k.mp4", {}) == {
        "tiktok": social.SOCIAL_ALREADY_POSTED
    }


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_dry_run_records_without_network(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch, dry_run=True)
    account = {"name": "A", "tiktok_enabled": True,
               "tiktok_open_id": "1", "tiktok_access_token": "t"}
    assert poster.crosspost(account, "vid1", None, "k.mp4", {}) == {
        "tiktok": social.SOCIAL_DRY_RUN
    }
    row = db.get_social_post("vid1", "A", "tiktok")
    assert row["status"] == social.SOCIAL_DRY_RUN


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_tiktok_uses_file(monkeypatch, tmp_path, name):
    social = SOCIAL_MODULES[name]
    poster, _db = _poster(name, tmp_path, monkeypatch)
    video = tmp_path / "short.mp4"
    video.write_bytes(b"0" * 16)
    account = {"name": "A", "tiktok_enabled": True,
               "tiktok_open_id": "o", "tiktok_access_token": "t"}
    seen = {}
    monkeypatch.setattr(
        social.TikTokUploader,
        "upload_video",
        lambda self, path, title="", video_url="": seen.update(
            {"path": str(path), "title": title, "url": video_url}
        ) or "pub1",
    )
    assert poster.crosspost(account, "vid1", video, None, {"title": "Hi"}) == {
        "tiktok": social.SOCIAL_POSTED
    }
    assert seen["path"].endswith("short.mp4") and seen["url"] == ""


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_never_raises(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, _db = _poster(name, tmp_path, monkeypatch,
                          public_url="https://cdn.example")

    def _crash(self, *_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(social.TikTokUploader, "upload_video", _crash)
    account = {"name": "A", "tiktok_enabled": True,
               "tiktok_open_id": "1", "tiktok_access_token": "t"}
    assert poster.crosspost(account, "vid1", None, "k.mp4", {}) == {
        "tiktok": social.SOCIAL_FAILED
    }


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_save_social_tokens_whitelists_keys(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps({"accounts": [{"name": "A", "tiktok_access_token": "old"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(social, "ACCOUNTS_FILE", accounts_file)
    assert social.save_social_tokens(
        "A", "tiktok", {"tiktok_access_token": "new", "tiktok_evil": "x"}
    )
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["tiktok_access_token"] == "new"
    assert "tiktok_evil" not in saved
    assert not social.save_social_tokens("Ghost", "tiktok", {"tiktok_access_token": "z"})


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_delete_account_data_removes_social_rows(name, tmp_path):
    db = STATE_DBS[name](tmp_path / f"{name}-del.db")
    db.record_social_post("vid1", "Gone", "tiktok", status="POSTED")
    db.delete_account_data("Gone")
    assert db.get_social_post("vid1", "Gone", "tiktok") is None


# ---------------------------------------------------------------------------
# Control panel: save endpoint, secrets handling, preservation (both bots)
# ---------------------------------------------------------------------------
def _panel(tmp_path, monkeypatch, webui, accounts):
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    monkeypatch.setattr(webui, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(webui, "ACCOUNTS", accounts)
    return accounts_file


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_social_save_endpoint(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    accounts_file = _panel(
        tmp_path, monkeypatch, webui,
        [{"name": "A", "target_channels": [], "enabled": True}],
    )
    client = webui.create_app(testing=True).test_client()
    response = client.post(
        "/api/social/save",
        data={
            "account": "A",
            "tiktok_enabled": "true",
            "tiktok_open_id": "open1",
            "tiktok_access_token": "tttok",
            "tiktok_privacy_level": "SELF_ONLY",
        },
    )
    assert response.status_code == 302
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["tiktok_enabled"] is True
    assert saved["tiktok_access_token"] == "tttok"
    assert saved["tiktok_privacy_level"] == "SELF_ONLY"

    # Blank secrets keep the stored values; other forms never clear toggles.
    response = client.post(
        "/api/social/save",
        data={"account": "A", "tiktok_open_id": "open1",
              "tiktok_access_token": ""},
    )
    assert response.status_code == 302
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["tiktok_access_token"] == "tttok"
    assert saved["tiktok_enabled"] is True  # untouched by a partial save

    # Invalid privacy levels are ignored.
    client.post(
        "/api/social/save",
        data={"account": "A", "tiktok_privacy_level": "EVERYONE_EVERYWHERE"},
    )
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["tiktok_privacy_level"] == "SELF_ONLY"


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_social_save_rejects_bad_account(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    _panel(tmp_path, monkeypatch, webui, [])
    client = webui.create_app(testing=True).test_client()
    response = client.post("/api/social/save", data={"account": "../evil"})
    assert response.status_code == 302
    assert "Invalid+account+name" in response.headers["Location"]


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_clean_account_preserves_social_fields(name):
    webui = WEBUI_MODULES[name]
    cleaned = webui._clean_account(
        {
            "name": "A",
            "target_channels": [],
            "tiktok_enabled": "on",
            "tiktok_open_id": "open1",
            "tiktok_privacy_level": "bogus",
        }
    )
    assert cleaned["tiktok_enabled"] is True
    assert cleaned["tiktok_privacy_level"] == "PUBLIC_TO_EVERYONE"


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_sources_save_keeps_social_credentials(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    accounts_file = _panel(
        tmp_path, monkeypatch, webui,
        [{"name": "A", "target_channels": [], "enabled": True,
          "tiktok_enabled": True, "tiktok_access_token": "tttok"}],
    )
    client = webui.create_app(testing=True).test_client()
    response = client.post(
        "/api/accounts/save",
        data={
            "acc_name_0": "A",
            "acc_channels_0": "https://www.youtube.com/@x",
            "acc_maxdaily_0": "5",
            "acc_enabled_0": "true",
            "acc_processmode_0": "copy",
            "acc_order_0": "newest",
        },
    )
    assert response.status_code == 302
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["tiktok_enabled"] is True
    assert saved["tiktok_access_token"] == "tttok"


# ---------------------------------------------------------------------------
# Scheduler integration: the hook fires independently of the YouTube result
# ---------------------------------------------------------------------------
class _HookStorage:
    client = None

    def upload_file(self, _path, r2_key=None):
        return None

    @staticmethod
    def cleanup_local_files(*_paths):
        return None


class _HookProcessor:
    def process_clip_to_short(self, raw_path, output_path=None, **_kwargs):
        output = Path(output_path)
        output.write_bytes(b"processed")
        return output


class _HookUploader:
    def __init__(self, result):
        self.result = result
        self.last_metadata = {"title": "Hooked Title", "tags": ["hooked"]}
        self.calls = 0

    def upload_short(self, **kwargs):
        self.calls += 1
        return self.result


class _HookShortsFetcher:
    def __init__(self, raw_path):
        self.raw_path = raw_path

    def download_short(self, _url):
        return self.raw_path

    def get_short_info(self, _url):
        return {"title": "Hooked Short"}


class _HookReprocessor:
    def process_short(self, _raw_path, output_path=None, **_kwargs):
        output = Path(output_path)
        output.write_bytes(b"processed")
        return output


@pytest.mark.parametrize(
    "result,expected", [(None, 0), (UPLOAD_QUOTA_REACHED, 0), ("yt-real-1", 1)]
)
def test_clip_hook_fires_despite_youtube_result(tmp_path, monkeypatch, result, expected):
    from yt_shorts_bot.scheduler import ShortsBotScheduler

    db = ClipStateDB(tmp_path / "hook.db")
    scheduler = ShortsBotScheduler(
        accounts=[{"name": "H", "enabled": True}],
        state_db=db,
        processor=_HookProcessor(),
        storage=_HookStorage(),
    )
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"raw")
    monkeypatch.setattr(scheduler, "_download_window", lambda _u, _s, _e: raw)
    seen = {}

    def fake_crosspost(self, account, video_id, video_path, r2_key=None, metadata=None):
        seen.update(
            {
                "account": account,
                "video_id": video_id,
                "video_path": Path(video_path),
                "metadata": metadata,
            }
        )
        return {"tiktok": "POSTED"}

    monkeypatch.setattr(clip_social.SocialDestinations, "crosspost", fake_crosspost)
    made = scheduler._process_video_windows(
        "hookvid01",
        "https://www.youtube.com/watch?v=hookvid01",
        "Title",
        "churl",
        [{"start": 0.0, "end": 18.0}],
        account="H",
        uploader=_HookUploader(result),
        info={"title": "Title"},
        account_config={"name": "H", "tiktok_enabled": True},
    )
    assert made == expected
    # The hook fired with the finished file + YouTube metadata either way.
    assert seen["video_id"] == "hookvid01"
    assert seen["account"]["tiktok_enabled"] is True
    assert seen["video_path"].name.startswith("processed_")
    assert seen["metadata"]["title"] == "Hooked Title"


def test_clip_hook_social_crash_does_not_break_youtube(tmp_path, monkeypatch):
    from yt_shorts_bot.scheduler import ShortsBotScheduler

    db = ClipStateDB(tmp_path / "hook-crash.db")
    scheduler = ShortsBotScheduler(
        accounts=[{"name": "H", "enabled": True}],
        state_db=db,
        processor=_HookProcessor(),
        storage=_HookStorage(),
    )
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"raw")
    monkeypatch.setattr(scheduler, "_download_window", lambda _u, _s, _e: raw)

    def boom(self, *_args, **_kwargs):
        raise RuntimeError("social exploded")

    monkeypatch.setattr(clip_social.SocialDestinations, "crosspost", boom)
    made = scheduler._process_video_windows(
        "hookvid02",
        "https://www.youtube.com/watch?v=hookvid02",
        "Title",
        "churl",
        [{"start": 0.0, "end": 18.0}],
        account="H",
        uploader=_HookUploader("yt-real-9"),
        info={"title": "Title"},
        account_config={"name": "H", "tiktok_enabled": True},
    )
    assert made == 1


def test_repost_hook_fires_despite_youtube_result(tmp_path, monkeypatch):
    from yt_shorts_repost_bot.scheduler import ShortsRepostScheduler

    db = RepostStateDB(tmp_path / "hook.db")
    scheduler = ShortsRepostScheduler(
        accounts=[{"name": "H", "enabled": True}],
        state_db=db,
        storage=_HookStorage(),
    )
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"raw")
    seen = {}

    def fake_crosspost(self, account, video_id, video_path, r2_key=None, metadata=None):
        seen.update({"video_id": video_id, "account": account})
        return {"tiktok": "POSTED"}

    monkeypatch.setattr(repost_social.SocialDestinations, "crosspost", fake_crosspost)
    ok = scheduler._process_one(
        "rhook01",
        "https://www.youtube.com/shorts/rhook01",
        "Title",
        "churl",
        account="H",
        fetcher=_HookShortsFetcher(raw),
        reprocessor=_HookReprocessor(),
        uploader=_HookUploader(None),
        account_config={"name": "H", "tiktok_enabled": True},
    )
    assert ok is False  # YouTube failed, but the hook still fired.
    assert seen == {"video_id": "rhook01", "account": {"name": "H", "tiktok_enabled": True}}


def test_end_to_end_cycle_posts_youtube_and_social(tmp_path, monkeypatch):
    """Full clip-bot cycle with social enabled: YouTube upload + TikTok post,
    all tracked in the DB. All platform HTTP is mocked."""
    import yt_shorts_bot.scheduler as clip_scheduler_module
    from yt_shorts_bot.scheduler import ShortsBotScheduler

    account = {
        "name": "E2E",
        "target_channels": ["https://www.youtube.com/@Source"],
        "enabled": True,
        "shorts_per_video": 1,
        "min_minutes_between_uploads": 0,
        "max_daily_uploads": 10,
        "tiktok_enabled": True,
        "tiktok_open_id": "open1",
        "tiktok_access_token": "tt-token",
    }

    class _E2EFetcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def fetch_channel_recent_videos(self, _url, order="newest"):
            return [
                {
                    "video_id": "e2evid00001",
                    "url": "https://www.youtube.com/watch?v=e2evid00001",
                    "title": "E2E VOD",
                    "duration": 400,
                }
            ]

        def extract_heatmap_and_select_window(self, _url):
            return ({"title": "E2E VOD"}, 10.0, 1.0, 19.0)

    class _E2EUploader:
        dry_run = False

        def __init__(self, *_args, **_kwargs):
            self.last_metadata = {"title": "E2E Short #e2e", "tags": ["e2e", "clips"]}

        def upload_short(self, **kwargs):
            return "yt-e2e-1"

    class _E2EStorage(_HookStorage):
        def upload_file(self, _path, r2_key=None):
            return r2_key

        def public_url_for_key(self, r2_key):
            return f"https://cdn.example/{r2_key}" if r2_key else ""

    monkeypatch.setattr(clip_scheduler_module, "YouTubeFetcher", _E2EFetcher)
    monkeypatch.setattr(clip_scheduler_module, "YouTubeUploader", _E2EUploader)

    def _tt_post(url, **kwargs):
        if url.endswith("/v2/post/publish/creator_info/query/"):
            return _ok_tiktok({"privacy_level_options": ["PUBLIC_TO_EVERYONE"]})
        if url.endswith("/v2/post/publish/video/init/"):
            posted = kwargs["json"]
            assert posted["source_info"]["source"] == "PULL_FROM_URL"
            assert posted["post_info"]["title"] == "E2E Short #e2e #clips"
            return _ok_tiktok({"publish_id": "tt-pub-e2e"})
        if url.endswith("/v2/post/publish/status/fetch/"):
            return _ok_tiktok({"status": "PUBLISH_COMPLETE"})
        raise AssertionError(f"unexpected TikTok POST {url}")

    def _no_put(*_args, **_kwargs):
        raise AssertionError("URL-pull flow must not PUT chunks")

    monkeypatch.setattr(clip_tiktok.requests, "post", _tt_post)
    monkeypatch.setattr(clip_tiktok.requests, "put", _no_put)

    db = ClipStateDB(tmp_path / "e2e.db")
    scheduler = ShortsBotScheduler(
        accounts=[account],
        state_db=db,
        processor=_HookProcessor(),
        storage=_E2EStorage(),
    )
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"raw")
    monkeypatch.setattr(scheduler, "_download_window", lambda _u, _s, _e: raw)

    assert scheduler.run_single_cycle(accounts=scheduler.accounts) == 1
    assert db.get_video_state("e2evid00001", "E2E")["status"] == "UPLOADED_YOUTUBE"
    tt = db.get_social_post("e2evid00001", "E2E", "tiktok")
    assert (tt["status"], tt["remote_id"]) == ("POSTED", "tt-pub-e2e")
