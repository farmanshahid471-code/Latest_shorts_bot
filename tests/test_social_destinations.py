"""TikTok + Bilibili cross-posting: captions, uploaders, idempotency, panel."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import yt_shorts_bot.social as clip_social
import yt_shorts_bot.social_bilibili as clip_bilibili
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
    assert not social.is_enabled({"tiktok_enabled": 1}, "bilibili")
    assert social.is_enabled({"bilibili_enabled": True}, "bilibili")


def test_social_modules_are_mirrored():
    root = Path(__file__).resolve().parents[1]
    for filename in ("social.py", "social_tiktok.py", "social_bilibili.py",
                     "platform_settings.py", "manual_export.py",
                     "dubbing.py"):
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


# ---------------------------------------------------------------------------
# Bilibili uploader (mocked HTTP)
# ---------------------------------------------------------------------------
class _BiliResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {"code": 0, "message": "0"}
        self.status_code = status_code

    def json(self):
        return self._payload


def _bili_uploader(**kwargs):
    defaults = dict(
        client_id="cid",
        client_secret="secret",
        access_token="atok",
        refresh_token="rtok",
        dry_run=False,
    )
    defaults.update(kwargs)
    return clip_bilibili.BilibiliUploader(**defaults)


def test_bilibili_signature_is_deterministic_hmac():
    uploader = _bili_uploader()
    headers = uploader._signed_headers(b'{"a":1}', "application/json")
    # Signed payload = the sorted x-bili-* headers joined by newlines.
    signed = {k: v for k, v in headers.items() if k.startswith("x-bili-")}
    payload = "\n".join(f"{k}:{signed[k]}" for k in sorted(signed))
    import hashlib
    import hmac as _hmac

    expected = _hmac.new(b"secret", payload.encode(), hashlib.sha256).hexdigest()
    assert headers["Authorization"] == expected
    assert headers["x-bili-content-md5"] == hashlib.md5(b'{"a":1}').hexdigest()
    assert headers["x-bili-signature-method"] == "HMAC-SHA256"
    assert headers["access-token"] == "atok"


def test_bilibili_check_connection_missing_config():
    ok, detail = clip_bilibili.BilibiliUploader(dry_run=False).check_connection()
    assert ok is False and "client id" in detail


def test_bilibili_check_connection_ok(monkeypatch):
    def _request(method, url, **kwargs):
        assert url.endswith("/arcopen/fn/user/account/info")
        return _BiliResponse({"code": 0, "data": {"name": "upzhu"}})

    monkeypatch.setattr(clip_bilibili.requests, "request", _request)
    ok, detail = _bili_uploader().check_connection()
    assert ok is True and "upzhu" in detail


def test_bilibili_small_file_uses_single_shot_upload(monkeypatch, tmp_path):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"x" * 1024)
    seen = {"posts": [], "submit": None}

    def _request(method, url, **kwargs):
        seen["posts"].append(url)
        if url.endswith("/archive/video/init"):
            # Small files must ask for the single-shot upload type.
            assert b'"utype": "1"' in kwargs["data"]
            return _BiliResponse({"code": 0, "data": {"upload_token": "utok"}})
        if url.endswith("/video/v2/upload"):
            assert kwargs["params"]["upload_token"] == "utok"
            return _BiliResponse()
        if url.endswith("/archive/add-by-utoken"):
            seen["submit"] = json.loads(kwargs["data"].decode())
            return _BiliResponse({"code": 0, "data": {"resource_id": "BV1xx"}})
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(clip_bilibili.requests, "request", _request)
    monkeypatch.setattr(clip_bilibili.requests, "post", lambda url, **kw: _request("POST", url, **kw))
    resource_id = _bili_uploader(tid=17).upload_video(
        video, title="Hi", description="desc", tags=["a", "b"]
    )
    assert resource_id == "BV1xx"
    assert seen["submit"]["tid"] == 17
    assert seen["submit"]["tag"] == "a,b"
    assert seen["submit"]["copyright"] == 1
    # No merge call for the single-shot flow.
    assert not any("video/complete" in u for u in seen["posts"])


def test_bilibili_large_file_chunks_and_merges(monkeypatch, tmp_path):
    monkeypatch.setattr(clip_bilibili, "SMALL_FILE_MAX_BYTES", 10)
    monkeypatch.setattr(clip_bilibili, "CHUNK_SIZE", 4)
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x" * 11)
    parts, merged = [], []

    def _request(method, url, **kwargs):
        if url.endswith("/archive/video/init"):
            assert b'"utype": "0"' in kwargs["data"]
            return _BiliResponse({"code": 0, "data": {"upload_token": "utok"}})
        if url.endswith("/part/upload"):
            parts.append((kwargs["params"]["part_number"], len(kwargs["data"])))
            return _BiliResponse()
        if url.endswith("/archive/video/complete"):
            merged.append(kwargs["params"]["upload_token"])
            return _BiliResponse({"code": 0, "data": {}})
        if url.endswith("/archive/add-by-utoken"):
            return _BiliResponse({"code": 0, "data": {"resource_id": "BV2yy"}})
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(clip_bilibili.requests, "request", _request)
    monkeypatch.setattr(clip_bilibili.requests, "post", lambda url, **kw: _request("POST", url, **kw))
    assert _bili_uploader().upload_video(video, title="Big") == "BV2yy"
    assert parts == [(1, 4), (2, 4), (3, 3)]
    assert merged == ["utok"]


def test_bilibili_expired_token_refreshes_and_retries(monkeypatch, tmp_path):
    saved = {}
    calls = {"n": 0}

    def _request(method, url, **kwargs):
        if url.endswith("/arcopen/fn/user/account/info"):
            calls["n"] += 1
            if calls["n"] == 1:
                return _BiliResponse({"code": 127001, "message": "token invalid"})
            # The retry must carry the refreshed token.
            assert kwargs["headers"]["access-token"] == "fresh-token"
            return _BiliResponse({"code": 0, "data": {"name": "upzhu"}})
        raise AssertionError(f"unexpected call {url}")

    def _post(url, **kwargs):
        assert url == clip_bilibili.OAUTH_URL
        return _BiliResponse(
            {"code": 0, "data": {"access_token": "fresh-token",
                                 "refresh_token": "fresh-refresh"}}
        )

    monkeypatch.setattr(clip_bilibili.requests, "request", _request)
    monkeypatch.setattr(clip_bilibili.requests, "post", _post)
    uploader = _bili_uploader(
        on_tokens_refreshed=lambda a, r, e: saved.update({"a": a, "r": r})
    )
    ok, _detail = uploader.check_connection()
    assert ok is True
    assert saved == {"a": "fresh-token", "r": "fresh-refresh"}


def test_bilibili_api_error_raises(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(
        clip_bilibili.requests,
        "request",
        lambda *a, **k: _BiliResponse({"code": 127007, "message": "no permission"}),
    )
    with pytest.raises(clip_bilibili.BilibiliAPIError):
        _bili_uploader().upload_video(video, title="Hi")


def test_bilibili_dry_run_sends_nothing(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    def _boom(*_args, **_kwargs):
        raise AssertionError("dry-run must not touch the network")

    monkeypatch.setattr(clip_bilibili.requests, "request", _boom)
    assert _bili_uploader(dry_run=True).upload_video(video, title="Hi") == "dry-run"


def test_bilibili_cover_failure_does_not_block_submit(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"img")

    def _request(method, url, **kwargs):
        if url.endswith("/archive/video/init"):
            return _BiliResponse({"code": 0, "data": {"upload_token": "utok"}})
        if url.endswith("/video/v2/upload"):
            return _BiliResponse()
        if url.endswith("/cover/upload"):
            return _BiliResponse({"code": 4010, "message": "service error"})
        if url.endswith("/archive/add-by-utoken"):
            assert json.loads(kwargs["data"].decode())["cover"] == ""
            return _BiliResponse({"code": 0, "data": {"resource_id": "BV3zz"}})
        raise AssertionError(f"unexpected call {url}")

    monkeypatch.setattr(clip_bilibili.requests, "request", _request)
    monkeypatch.setattr(clip_bilibili.requests, "post", lambda url, **kw: _request("POST", url, **kw))
    assert _bili_uploader().upload_video(video, title="Hi", cover_path=cover) == "BV3zz"


def test_bilibili_tag_string_respects_limit():
    assert clip_bilibili.build_tag_string(["#a", "a", "b c"]) == "a,b c"
    long_tags = [f"tag{i:03d}" * 3 for i in range(50)]
    assert len(clip_bilibili.build_tag_string(long_tags)) <= clip_bilibili.TAG_MAX_LEN


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_bilibili_success_and_failure(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch)
    video = tmp_path / f"{name}-bili.mp4"
    video.write_bytes(b"data")
    account = {"name": "A", "bilibili_enabled": True,
               "bilibili_client_id": "cid", "bilibili_client_secret": "sec",
               "bilibili_access_token": "tok"}
    metadata = {"title": "Hi", "tags": ["x"]}

    monkeypatch.setattr(
        social.BilibiliUploader, "upload_video",
        lambda self, path, **kwargs: "BV1abc",
    )
    assert poster.crosspost(account, "vid1", video, None, metadata) == {
        "bilibili": social.SOCIAL_POSTED
    }
    assert db.get_social_post("vid1", "A", "bilibili")["remote_id"] == "BV1abc"

    def _fail(self, path, **kwargs):
        raise social.BilibiliAPIError("bilibili says no")

    monkeypatch.setattr(social.BilibiliUploader, "upload_video", _fail)
    assert poster.crosspost(account, "vid2", video, None, metadata) == {
        "bilibili": social.SOCIAL_FAILED
    }
    row = db.get_social_post("vid2", "A", "bilibili")
    assert row["status"] == social.SOCIAL_FAILED and "bilibili says no" in row["error_msg"]


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_bilibili_needs_local_file(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch, public_url="https://cdn.example")
    account = {"name": "A", "bilibili_enabled": True,
               "bilibili_client_id": "cid", "bilibili_client_secret": "sec",
               "bilibili_access_token": "tok"}
    # An R2 URL is not enough: Bilibili has no pull-from-URL flow.
    assert poster.crosspost(account, "vid1", None, "k.mp4", {}) == {
        "bilibili": social.SOCIAL_SKIPPED
    }
    assert "file on disk" in db.get_social_post("vid1", "A", "bilibili")["error_msg"]


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_bilibili_missing_credentials_skips(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch)
    account = {"name": "A", "bilibili_enabled": True, "bilibili_client_id": "cid"}
    assert poster.crosspost(account, "vid1", None, None, {}) == {
        "bilibili": social.SOCIAL_SKIPPED
    }
    assert "missing" in db.get_social_post("vid1", "A", "bilibili")["error_msg"]


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_bilibili_dry_run_and_idempotency(name, tmp_path, monkeypatch):
    social = SOCIAL_MODULES[name]
    poster, db = _poster(name, tmp_path, monkeypatch, dry_run=True)
    account = {"name": "A", "bilibili_enabled": True,
               "bilibili_client_id": "cid", "bilibili_client_secret": "sec",
               "bilibili_access_token": "tok"}
    assert poster.crosspost(account, "vid1", None, None, {}) == {
        "bilibili": social.SOCIAL_DRY_RUN
    }
    live, db2 = _poster(name, tmp_path / "b", monkeypatch)
    db2.record_social_post("vid9", "A", "bilibili", remote_id="BV0", status="POSTED")

    def _boom(*_args, **_kwargs):
        raise AssertionError("already-posted must not touch the network")

    monkeypatch.setattr(social.BilibiliUploader, "upload_video", _boom)
    assert live.crosspost(account, "vid9", None, None, {}) == {
        "bilibili": social.SOCIAL_ALREADY_POSTED
    }


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_crosspost_runs_both_destinations(name, tmp_path, monkeypatch):
    """TikTok and Bilibili are attempted independently in the same pass."""
    social = SOCIAL_MODULES[name]
    poster, _db = _poster(name, tmp_path, monkeypatch)
    video = tmp_path / f"{name}-both.mp4"
    video.write_bytes(b"data")
    account = {"name": "A",
               "tiktok_enabled": True, "tiktok_open_id": "o", "tiktok_access_token": "t",
               "bilibili_enabled": True, "bilibili_client_id": "cid",
               "bilibili_client_secret": "sec", "bilibili_access_token": "tok"}

    def _tt_fail(self, *_args, **_kwargs):
        raise social.TikTokAPIError("tiktok down")

    monkeypatch.setattr(social.TikTokUploader, "upload_video", _tt_fail)
    monkeypatch.setattr(
        social.BilibiliUploader, "upload_video", lambda self, path, **kw: "BV9"
    )
    # A TikTok failure must not stop the Bilibili post.
    assert poster.crosspost(account, "vid1", video, None, {}) == {
        "tiktok": social.SOCIAL_FAILED,
        "bilibili": social.SOCIAL_POSTED,
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
            "bilibili_enabled": "true",
            "bilibili_client_id": "bcid",
            "bilibili_client_secret": "bsec",
            "bilibili_access_token": "btok",
            "bilibili_tid": "17",
        },
    )
    assert response.status_code == 302
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["tiktok_enabled"] is True
    assert saved["tiktok_access_token"] == "tttok"
    assert saved["bilibili_enabled"] is True
    assert saved["bilibili_access_token"] == "btok"
    assert saved["tiktok_privacy_level"] == "SELF_ONLY"
    assert saved["bilibili_enabled"] is True
    assert saved["bilibili_client_secret"] == "bsec"
    assert saved["bilibili_tid"] == "17"

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
    assert saved["bilibili_access_token"] == "btok"  # blank secret keeps it
    assert saved["bilibili_enabled"] is True

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
            "bilibili_enabled": "true",
            "bilibili_access_token": "btok",
            "tiktok_privacy_level": "bogus",
        }
    )
    assert cleaned["tiktok_enabled"] is True
    assert cleaned["tiktok_privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert cleaned["bilibili_enabled"] is True
    assert cleaned["bilibili_access_token"] == "btok"


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_sources_save_keeps_social_credentials(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    accounts_file = _panel(
        tmp_path, monkeypatch, webui,
        [{"name": "A", "target_channels": [], "enabled": True,
          "tiktok_enabled": True, "tiktok_access_token": "tttok",
          "bilibili_enabled": True, "bilibili_access_token": "btok"}],
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
    assert saved["bilibili_enabled"] is True
    assert saved["bilibili_access_token"] == "btok"


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

    def fake_crosspost(self, account, video_id, video_path, r2_key=None,
                       metadata=None, only_platforms=None):
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

    def fake_crosspost(self, account, video_id, video_path, r2_key=None,
                       metadata=None, only_platforms=None):
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


# ---------------------------------------------------------------------------
# Panel layout: platform tabs (top level) + account sub-tabs (second level)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["clip", "repost"])
def test_platform_tabs_render_only_their_own_panel(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    _panel(
        tmp_path, monkeypatch, webui,
        [{"name": "A", "target_channels": [], "enabled": True},
         {"name": "B", "target_channels": [], "enabled": True}],
    )
    client = webui.create_app(testing=True).test_client()
    panels = {
        "youtube": "🔑 Credentials",
        "tiktok": "🎵 Post to TikTok",
        "bilibili": "📺 Post to Bilibili",
    }
    for platform, marker in panels.items():
        html = client.get(f"/?account=A&platform={platform}").get_data(as_text=True)
        assert marker in html
        # The other platforms' connection panels stay hidden.
        for other, other_marker in panels.items():
            if other != platform:
                assert other_marker not in html
        # The active platform tab is highlighted...
        assert f'ptab ptab-active" href="/?account=A&platform={platform}"' in html
        # ...and both account sub-tabs remain available underneath it.
        assert f'href="/?account=B&platform={platform}"' in html
        # Shared per-account cards are not duplicated per platform.
        assert html.count("Settings for this account") == 1
        assert html.count("Source channels for this account") == 1


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_unknown_platform_falls_back_to_youtube(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    _panel(tmp_path, monkeypatch, webui,
           [{"name": "A", "target_channels": [], "enabled": True}])
    client = webui.create_app(testing=True).test_client()
    html = client.get("/?account=A&platform=../evil").get_data(as_text=True)
    assert "🔑 Credentials" in html
    assert 'ptab ptab-active" href="/?account=A&platform=youtube"' in html


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_saves_return_to_the_platform_tab_they_came_from(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    _panel(tmp_path, monkeypatch, webui,
           [{"name": "A", "target_channels": [], "enabled": True}])
    client = webui.create_app(testing=True).test_client()
    response = client.post(
        "/api/social/save",
        data={"account": "A", "platform": "bilibili", "bilibili_enabled": "true"},
    )
    assert "platform=bilibili" in response.headers["Location"]
    # A brand-new account tab also stays on the platform it was added from.
    response = client.post("/api/accounts/add", data={"platform": "tiktok"})
    assert "platform=tiktok" in response.headers["Location"]


@pytest.mark.parametrize("name", ["clip", "repost"])
def test_platform_tab_dot_reflects_connection(name, tmp_path, monkeypatch):
    webui = WEBUI_MODULES[name]
    _panel(
        tmp_path, monkeypatch, webui,
        [{"name": "A", "target_channels": [], "enabled": True,
          "tiktok_enabled": True, "tiktok_open_id": "o", "tiktok_access_token": "t"}],
    )
    client = webui.create_app(testing=True).test_client()
    html = client.get("/?account=A&platform=youtube").get_data(as_text=True)

    def _tab_markup(platform: str) -> str:
        # Each platform tab is one <a> element; take it up to its </a>.
        start = html.index(f'href="/?account=A&platform={platform}"')
        return html[start:html.index("</a>", start)]

    # Configured TikTok gets the green dot; unconfigured Bilibili does not.
    assert "var(--green)" in _tab_markup("tiktok")
    assert "var(--muted)" in _tab_markup("bilibili")
