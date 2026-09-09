"""Instagram + TikTok cross-posting: captions, uploaders, idempotency, panel."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import yt_shorts_bot.social as clip_social
import yt_shorts_bot.social_instagram as clip_instagram
import yt_shorts_bot.social_tiktok as clip_tiktok
import yt_shorts_bot.webui as clip_webui
import yt_shorts_repost_bot.social as repost_social
import yt_shorts_repost_bot.webui as repost_webui
from yt_shorts_bot.models import StateDB as ClipStateDB
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
    assert (
        social.build_social_caption(metadata, "instagram") == "Funny Cat #cats\n\n#pets"
    )
    assert social.build_social_caption(metadata, "tiktok") == "Funny Cat #cats #pets"


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_build_social_caption_truncates_and_handles_empty(name):
    social = SOCIAL_MODULES[name]
    long_title = "x" * 500
    assert len(social.build_social_caption({"title": long_title}, "tiktok")) == 150
    assert "New Short" in social.build_social_caption({}, "instagram")
    assert social.build_social_caption(None, "tiktok")


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_is_enabled(name):
    social = SOCIAL_MODULES[name]
    assert not social.is_enabled(None, "instagram")
    assert not social.is_enabled({}, "tiktok")
    assert social.is_enabled({"instagram_enabled": True}, "instagram")
    assert not social.is_enabled({"instagram_enabled": True}, "tiktok")
    assert social.is_enabled({"tiktok_enabled": 1}, "tiktok")


def test_social_modules_are_mirrored():
    root = Path(__file__).resolve().parents[1]
    for filename in ("social.py", "social_instagram.py", "social_tiktok.py"):
        clip = (root / "yt_shorts_bot" / filename).read_text(encoding="utf-8")
        repost = (root / "yt_shorts_repost_bot" / filename).read_text(encoding="utf-8")
        assert clip == repost, f"{filename} diverged between bots"


# ---------------------------------------------------------------------------
# Instagram uploader (mocked HTTP)
# ---------------------------------------------------------------------------
def _instagram_responder(calls, container_status="FINISHED"):
    def _request(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/media") and method == "POST":
            return FakeResponse({"id": "container123"})
        if url.endswith("/media_publish"):
            return FakeResponse({"id": "media999"})
        if "container123" in url:
            return FakeResponse({"status_code": container_status})
        if url.rstrip("/").endswith("17841401234567890"):
            return FakeResponse({"id": "17841401234567890", "username": "clipz"})
        raise AssertionError(f"unexpected call {method} {url}")

    return _request


def test_instagram_check_connection_ok(monkeypatch):
    calls = []
    monkeypatch.setattr(
        clip_instagram.requests, "request", _instagram_responder(calls)
    )
    uploader = clip_instagram.InstagramReelsUploader(
        ig_user_id="17841401234567890",
        access_token="secret-token-abc123",
        dry_run=False,
    )
    ok, detail = uploader.check_connection()
    assert ok and "@clipz" in detail
    assert "secret-token-abc123" not in detail  # tokens never leak into messages
    assert detail.endswith("(token …c123).")


def test_instagram_check_connection_missing_config():
    uploader = clip_instagram.InstagramReelsUploader(dry_run=False)
    ok, detail = uploader.check_connection()
    assert not ok and detail


def test_instagram_publish_reel_flow(monkeypatch):
    calls = []
    monkeypatch.setattr(
        clip_instagram.requests, "request", _instagram_responder(calls)
    )
    uploader = clip_instagram.InstagramReelsUploader(
        ig_user_id="17841401234567890", access_token="tok", dry_run=False
    )
    assert (
        uploader.publish_reel("https://cdn.example/a.mp4", "hi #x") == "media999"
    )
    assert [method for method, _ in calls] == ["POST", "GET", "POST"]


def test_instagram_publish_reel_container_error(monkeypatch):
    calls = []
    monkeypatch.setattr(
        clip_instagram.requests, "request", _instagram_responder(calls, "ERROR")
    )
    uploader = clip_instagram.InstagramReelsUploader(
        ig_user_id="17841401234567890", access_token="tok", dry_run=False
    )
    with pytest.raises(clip_instagram.InstagramAPIError):
        uploader.publish_reel("https://cdn.example/a.mp4", "hi")


def test_instagram_dry_run_sends_nothing(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("no HTTP in dry-run")

    monkeypatch.setattr(clip_instagram.requests, "request", _boom)
    uploader = clip_instagram.InstagramReelsUploader(
        ig_user_id="1", access_token="tok", dry_run=True
    )
    assert uploader.publish_reel("https://cdn.example/a.mp4") == "DRY_RUN"


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
        if url.endswith("/v2/user/info/"):
            return _ok_tiktok({"user": {"open_id": "open1", "display_name": "clipz"}})
        raise AssertionError(f"unexpected POST {url}")

    _post.state = state
    return _post


def test_tiktok_check_connection_ok(monkeypatch):
    calls = []
    monkeypatch.setattr(clip_tiktok.requests, "post", _tiktok_post_factory(calls))
    uploader = clip_tiktok.TikTokUploader(
        open_id="open1", access_token="tok", dry_run=False
    )
    ok, detail = uploader.check_connection()
    assert ok and "clipz" in detail


def test_tiktok_check_connection_open_id_mismatch(monkeypatch):
    calls = []
    monkeypatch.setattr(clip_tiktok.requests, "post", _tiktok_post_factory(calls))
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
            refreshed.update(kwargs["json"])
            return _ok_tiktok(
                {"access_token": "newA", "refresh_token": "newR", "expires_in": 86400}
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
    db.record_social_post("vid1", "A", "instagram", remote_id="m1", status="POSTED")

    def _boom(*_args, **_kwargs):
        raise AssertionError("already-posted must not touch the network")

    monkeypatch.setattr(social.InstagramReelsUploader, "publish_reel", _boom)
    account = {"name": "A", "instagram_enabled": True,
               "instagram_ig_user_id": "1", "instagram_access_token": "t"}
    assert poster.crosspost(account, "vid1", None, "k.mp4", {}) == {
        "instagram": social.SOCIAL_ALREADY_POSTED
    }


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_dry_run_records_without_network(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch, dry_run=True)
    account = {"name": "A", "instagram_enabled": True,
               "instagram_ig_user_id": "1", "instagram_access_token": "t"}
    assert poster.crosspost(account, "vid1", None, "k.mp4", {}) == {
        "instagram": social.SOCIAL_DRY_RUN
    }
    row = db.get_social_post("vid1", "A", "instagram")
    assert row["status"] == social.SOCIAL_DRY_RUN


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_instagram_needs_public_url(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch, public_url="")
    account = {"name": "A", "instagram_enabled": True,
               "instagram_ig_user_id": "1", "instagram_access_token": "t"}
    assert poster.crosspost(account, "vid1", None, "k.mp4", {}) == {
        "instagram": social.SOCIAL_SKIPPED
    }
    row = db.get_social_post("vid1", "A", "instagram")
    assert row["status"] == social.SOCIAL_SKIPPED
    assert "R2_PUBLIC_BASE_URL" in row["error_msg"]


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_instagram_success_and_failure(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch,
                         public_url="https://cdn.example")
    account = {"name": "A", "instagram_enabled": True,
               "instagram_ig_user_id": "1", "instagram_access_token": "t"}
    metadata = {"title": "Hi", "tags": ["x"]}

    monkeypatch.setattr(
        social.InstagramReelsUploader, "publish_reel", lambda self, url, cap="": "media1"
    )
    assert poster.crosspost(account, "vid1", None, "k.mp4", metadata) == {
        "instagram": social.SOCIAL_POSTED
    }
    assert db.get_social_post("vid1", "A", "instagram")["remote_id"] == "media1"

    def _fail(self, url, cap=""):
        raise social.InstagramAPIError("Meta says no")

    monkeypatch.setattr(social.InstagramReelsUploader, "publish_reel", _fail)
    assert poster.crosspost(account, "vid2", None, "k.mp4", metadata) == {
        "instagram": social.SOCIAL_FAILED
    }
    row = db.get_social_post("vid2", "A", "instagram")
    assert row["status"] == social.SOCIAL_FAILED and "Meta says no" in row["error_msg"]


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

    def _crash(self, url, cap=""):
        raise RuntimeError("boom")

    monkeypatch.setattr(social.InstagramReelsUploader, "publish_reel", _crash)
    account = {"name": "A", "instagram_enabled": True,
               "instagram_ig_user_id": "1", "instagram_access_token": "t"}
    assert poster.crosspost(account, "vid1", None, "k.mp4", {}) == {
        "instagram": social.SOCIAL_FAILED
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
            "instagram_enabled": "true",
            "instagram_ig_user_id": "1784",
            "instagram_access_token": "igtok",
            "tiktok_enabled": "true",
            "tiktok_open_id": "open1",
            "tiktok_access_token": "tttok",
            "tiktok_privacy_level": "SELF_ONLY",
        },
    )
    assert response.status_code == 302
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["instagram_enabled"] is True
    assert saved["instagram_access_token"] == "igtok"
    assert saved["tiktok_privacy_level"] == "SELF_ONLY"

    # Blank secrets keep the stored values; other forms never clear toggles.
    response = client.post(
        "/api/social/save",
        data={"account": "A", "instagram_enabled": "true",
              "instagram_ig_user_id": "1784", "instagram_access_token": ""},
    )
    assert response.status_code == 302
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["instagram_access_token"] == "igtok"
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
            "instagram_enabled": "true",
            "instagram_ig_user_id": "1784",
            "instagram_access_token": "igtok",
            "tiktok_enabled": "on",
            "tiktok_open_id": "open1",
            "tiktok_privacy_level": "bogus",
        }
    )
    assert cleaned["instagram_enabled"] is True
    assert cleaned["instagram_access_token"] == "igtok"
    assert cleaned["tiktok_enabled"] is True
    assert cleaned["tiktok_privacy_level"] == "PUBLIC_TO_EVERYONE"


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_sources_save_keeps_social_credentials(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    accounts_file = _panel(
        tmp_path, monkeypatch, webui,
        [{"name": "A", "target_channels": [], "enabled": True,
          "instagram_enabled": True, "instagram_access_token": "igtok",
          "tiktok_access_token": "tttok"}],
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
    assert saved["instagram_enabled"] is True
    assert saved["instagram_access_token"] == "igtok"
    assert saved["tiktok_access_token"] == "tttok"
