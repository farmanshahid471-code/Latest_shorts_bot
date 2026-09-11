"""Per-platform settings: clip length, title overrides, per-length renders."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import yt_shorts_bot.platform_settings as ps
import yt_shorts_bot.scheduler as clip_scheduler_module
import yt_shorts_bot.social as clip_social
import yt_shorts_bot.webui as clip_webui
from yt_shorts_bot.config import CLIP_DURATION_SEC, clamp_clip_duration
from yt_shorts_bot.fetcher import YouTubeFetcher
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.scheduler import ShortsBotScheduler


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------
def test_clamp_clip_duration_bounds_and_garbage():
    assert clamp_clip_duration(20) == 20.0
    assert clamp_clip_duration(60) == 60.0
    assert clamp_clip_duration(1) == 5.0        # below the floor
    assert clamp_clip_duration(9999) == 180.0   # above the ceiling
    for junk in ("abc", None, "", 0, -5):
        assert clamp_clip_duration(junk) == float(CLIP_DURATION_SEC)


def test_clip_seconds_per_platform_with_fallbacks():
    account = {
        "clip_seconds": 30,            # account-wide default
        "youtube_clip_seconds": 20,    # YouTube overrides it
        "tiktok_enabled": True,
    }
    assert ps.clip_seconds_for(account, "youtube") == 20.0
    assert ps.clip_seconds_for(account, "tiktok") == 30.0   # inherits shared
    assert ps.clip_seconds_for({}, "tiktok") == float(CLIP_DURATION_SEC)


def test_render_groups_share_a_pass_when_lengths_match():
    account = {"tiktok_enabled": True, "bilibili_enabled": True,
               "youtube_clip_seconds": 20,
               "tiktok_clip_seconds": 60, "bilibili_clip_seconds": 60}
    # TikTok and Bilibili want the same length, so they share ONE render.
    assert ps.render_groups(account) == {20.0: ["youtube"],
                                         60.0: ["tiktok", "bilibili"]}
    # A disabled destination never gets a render pass.
    assert ps.render_groups({"youtube_clip_seconds": 20}) == {20.0: ["youtube"]}


def test_platform_metadata_overrides_only_what_is_set():
    account = {"title_prefix": "Shared", "tiktok_title_prefix": "TT",
               "title_hashtags": "shared", "tiktok_title_hashtags": "tiktokonly"}
    base = {"title": "Shared Funny cat", "tags": ["shared", "cats"]}
    tiktok = ps.platform_metadata(account, "tiktok", base)
    # The shared prefix is swapped for TikTok's, not stacked on top of it.
    assert tiktok["title"] == "TT Funny cat"
    assert "tiktokonly" in tiktok["tags"] and "shared" not in tiktok["tags"]
    assert "cats" in tiktok["tags"]  # unrelated tags survive
    # A platform without overrides passes through untouched.
    assert ps.platform_metadata(account, "youtube", base)["title"] == base["title"]


# ---------------------------------------------------------------------------
# Fetcher honours the requested length
# ---------------------------------------------------------------------------
def test_window_helpers_use_the_requested_length():
    fetcher = YouTubeFetcher.__new__(YouTubeFetcher)
    fetcher.clip_duration = None
    heatmap = [{"start_time": t, "end_time": t + 5, "value": t} for t in range(0, 300, 5)]
    for wanted in (20.0, 60.0):
        window = fetcher._best_window_from_heatmap(heatmap, 400.0, clip_duration=wanted)
        assert window["end"] - window["start"] == pytest.approx(wanted)
        candidates = fetcher._heatmap_candidates(heatmap, 400.0, clip_duration=wanted)
        assert all(
            c["end"] - c["start"] == pytest.approx(wanted) for c in candidates
        )
    # The default keeps the historic 15-20s band.
    start, end = fetcher._finalize_window(10.0, 12.0, 400.0)
    assert 15.0 <= end - start <= 20.0
    # A custom length is honoured exactly instead of being squashed to 20s.
    start, end = fetcher._finalize_window(10.0, 70.0, 400.0, clip_duration=60.0)
    assert end - start == pytest.approx(60.0)


def test_finalize_window_never_exceeds_the_source_video():
    fetcher = YouTubeFetcher.__new__(YouTubeFetcher)
    fetcher.clip_duration = None
    start, end = fetcher._finalize_window(0.0, 60.0, 25.0, clip_duration=60.0)
    assert start >= 0.0 and end <= 25.0


# ---------------------------------------------------------------------------
# Scheduler: one render pass per distinct clip length
# ---------------------------------------------------------------------------
class _Storage:
    client = None

    def upload_file(self, path, r2_key=None):
        return r2_key

    def public_url_for_key(self, key):
        return ""

    def get_bucket_usage(self):
        return (0, 0)

    def cleanup_local_files(self, *args, **kwargs):
        pass


class _Processor:
    detected_language = ""
    detected_language_probability = 0.0

    def process_clip_to_short(self, source, output_path=None, **kwargs):
        out = Path(output_path)
        out.write_bytes(source.read_bytes())
        return out


def _run_cycle(tmp_path, monkeypatch, account):
    asked, uploads, posts = [], [], []

    class _Fetcher:
        def __init__(self, *a, **k):
            pass

        def fetch_channel_recent_videos(self, url, order="newest"):
            return [{"video_id": "vid1", "url": "https://yt/watch?v=vid1",
                     "title": "VOD", "duration": 400}]

        def extract_heatmap_and_select_window(self, url, clip_duration=None):
            asked.append(clip_duration)
            length = clip_duration or float(CLIP_DURATION_SEC)
            return ({"title": "VOD"}, 10.0, 10.0, 10.0 + length)

    class _Uploader:
        dry_run = False

        def __init__(self, *a, **k):
            self.last_metadata = {}

        def generate_short_metadata(self, **kwargs):
            prefix = kwargs.get("title_prefix") or ""
            return {"title": f"{prefix} VOD".strip(), "tags": []}

        def upload_short(self, **kwargs):
            self.last_metadata = self.generate_short_metadata(
                title_prefix=kwargs.get("title_prefix")
            )
            uploads.append((kwargs["original_video_id"], self.last_metadata["title"]))
            return "yt-real-1"

    def _fake_crosspost(self, account_config, video_id, video_path, r2_key=None,
                        metadata=None, only_platforms=None):
        posts.append({
            "video_id": video_id,
            "platforms": tuple(only_platforms or ()),
            "title": (metadata or {}).get("title"),
            "seconds": Path(video_path).stat().st_size,
        })
        return {p: "POSTED" for p in (only_platforms or ())}

    monkeypatch.setattr(clip_scheduler_module, "YouTubeFetcher", _Fetcher)
    monkeypatch.setattr(clip_scheduler_module, "YouTubeUploader", _Uploader)
    monkeypatch.setattr(clip_social.SocialDestinations, "crosspost", _fake_crosspost)

    scheduler = ShortsBotScheduler(
        accounts=[account], state_db=StateDB(tmp_path / "s.db"),
        processor=_Processor(), storage=_Storage(),
    )

    def _download(url, start, end):
        # File size encodes the window length so assertions can check it.
        path = tmp_path / f"raw_{int(start)}_{int(end)}.mp4"
        path.write_bytes(b"x" * int(end - start))
        return path

    scheduler._download_window = _download
    count = scheduler.run_single_cycle(accounts=scheduler.accounts)
    return count, asked, uploads, posts


def test_different_lengths_render_separately(tmp_path, monkeypatch):
    account = {
        "name": "E2E", "target_channels": ["https://yt/@s"], "enabled": True,
        "shorts_per_video": 1, "min_minutes_between_uploads": 0,
        "max_daily_uploads": 10,
        "youtube_clip_seconds": 20, "tiktok_clip_seconds": 60,
        "tiktok_enabled": True, "tiktok_open_id": "o", "tiktok_access_token": "t",
        "title_prefix": "YT!", "tiktok_title_prefix": "TT!",
    }
    count, asked, uploads, posts = _run_cycle(tmp_path, monkeypatch, account)

    # Two independent moment selections: one per requested length.
    assert sorted(asked) == [20.0, 60.0]
    # YouTube uploaded exactly once, from the 20s pass.
    assert count == 1 and len(uploads) == 1
    # TikTok got its OWN 60s render, its own part id, and its own prefix.
    tiktok_posts = [p for p in posts if p["platforms"] == ("tiktok",)]
    assert len(tiktok_posts) == 1
    assert tiktok_posts[0]["seconds"] == 60
    assert tiktok_posts[0]["title"] == "TT! VOD"
    assert tiktok_posts[0]["video_id"] != uploads[0][0]  # no id collision


def test_same_length_uses_a_single_render(tmp_path, monkeypatch):
    account = {
        "name": "E2E", "target_channels": ["https://yt/@s"], "enabled": True,
        "shorts_per_video": 1, "min_minutes_between_uploads": 0,
        "max_daily_uploads": 10,
        "youtube_clip_seconds": 30, "tiktok_clip_seconds": 30,
        "tiktok_enabled": True, "tiktok_open_id": "o", "tiktok_access_token": "t",
    }
    count, asked, uploads, posts = _run_cycle(tmp_path, monkeypatch, account)
    # One length => one pass => one download, one upload, one cross-post.
    assert asked == [30.0]
    assert count == 1 and len(uploads) == 1
    assert [p["platforms"] for p in posts] == [("tiktok",)]
    assert posts[0]["seconds"] == 30


def test_account_without_clip_settings_keeps_default_behaviour(tmp_path, monkeypatch):
    account = {
        "name": "E2E", "target_channels": ["https://yt/@s"], "enabled": True,
        "shorts_per_video": 1, "min_minutes_between_uploads": 0,
        "max_daily_uploads": 10,
    }
    count, asked, uploads, _posts = _run_cycle(tmp_path, monkeypatch, account)
    assert asked == [float(CLIP_DURATION_SEC)]
    assert count == 1 and len(uploads) == 1


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------
def _panel(tmp_path, monkeypatch, accounts):
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    monkeypatch.setattr(clip_webui, "ACCOUNTS_FILE", accounts_file)
    return accounts_file


def test_platform_settings_save_and_clamp(tmp_path, monkeypatch):
    accounts_file = _panel(tmp_path, monkeypatch,
                           [{"name": "A", "target_channels": [], "enabled": True}])
    client = clip_webui.create_app(testing=True).test_client()
    response = client.post("/api/platform-settings/save", data={
        "account": "A", "platform": "tiktok",
        "tiktok_clip_seconds": "60", "tiktok_title_prefix": "TT",
    })
    assert response.status_code == 302 and "platform=tiktok" in response.headers["Location"]
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["tiktok_clip_seconds"] == 60.0
    assert saved["tiktok_title_prefix"] == "TT"

    # Out-of-range values are clamped rather than rejected.
    client.post("/api/platform-settings/save", data={
        "account": "A", "platform": "youtube", "youtube_clip_seconds": "9999"})
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["youtube_clip_seconds"] == 180.0

    # Clearing the field removes the override (inherit again).
    client.post("/api/platform-settings/save", data={
        "account": "A", "platform": "tiktok", "tiktok_clip_seconds": ""})
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert "tiktok_clip_seconds" not in saved
    assert saved["tiktok_title_prefix"] == "TT"  # untouched by a partial save


def test_platform_settings_survive_other_saves(tmp_path, monkeypatch):
    accounts_file = _panel(tmp_path, monkeypatch, [{
        "name": "A", "target_channels": [], "enabled": True,
        "tiktok_clip_seconds": 60, "tiktok_title_prefix": "TT",
    }])
    client = clip_webui.create_app(testing=True).test_client()
    client.post("/api/accounts/save", data={
        "acc_name_0": "A", "acc_channels_0": "https://www.youtube.com/@x",
        "acc_maxdaily_0": "5", "acc_enabled_0": "true",
        "acc_processmode_0": "copy", "acc_order_0": "newest",
    })
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["tiktok_clip_seconds"] == 60
    assert saved["tiktok_title_prefix"] == "TT"


def test_platform_settings_card_shows_per_tab_fields(tmp_path, monkeypatch):
    _panel(tmp_path, monkeypatch, [{
        "name": "A", "target_channels": [], "enabled": True,
        "tiktok_clip_seconds": 60,
    }])
    client = clip_webui.create_app(testing=True).test_client()
    html = client.get("/?account=A&platform=tiktok").get_data(as_text=True)
    assert 'name="tiktok_clip_seconds"' in html and 'value="60"' in html
    assert 'name="tiktok_title_prefix"' in html
    # The YouTube tab edits YouTube's own fields, not TikTok's.
    html = client.get("/?account=A&platform=youtube").get_data(as_text=True)
    assert 'name="youtube_clip_seconds"' in html
    assert 'name="tiktok_clip_seconds"' not in html
