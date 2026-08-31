"""Age-restricted source authentication and retry behavior."""
from __future__ import annotations

import io

import pytest

import yt_shorts_bot.fetcher as clip_fetcher_module
import yt_shorts_bot.uploader as clip_uploader_module
import yt_shorts_bot.webui as clip_webui
import yt_shorts_repost_bot.fetcher as repost_fetcher_module
import yt_shorts_repost_bot.scheduler as repost_scheduler_module
import yt_shorts_repost_bot.uploader as repost_uploader_module
import yt_shorts_repost_bot.webui as repost_webui
from yt_shorts_bot.fetcher import YouTubeFetcher
from yt_shorts_bot.models import StateDB
from yt_shorts_repost_bot.fetcher import ShortsFetcher
from yt_shorts_repost_bot.models import StateDB as RepostStateDB
from yt_shorts_repost_bot.scheduler import ShortsRepostScheduler

VALID_COOKIES = (
    b"# Netscape HTTP Cookie File\n"
    b"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2147483647\t"
    b"__Secure-1PSID\tprivate-cookie-value\n"
)


@pytest.mark.parametrize("webui", [clip_webui, repost_webui])
def test_control_panel_accepts_authenticated_netscape_cookie_export(
    monkeypatch, tmp_path, webui
):
    destination = tmp_path / "cookies.txt"
    monkeypatch.setattr(webui, "_shared_cookie_path", lambda: destination)
    monkeypatch.setattr(webui, "StateDB", lambda: object())
    client = webui.create_app(testing=True).test_client()

    response = client.post(
        "/api/youtube-cookies",
        data={"file": (io.BytesIO(VALID_COOKIES), "cookies.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert "viewer+cookies+saved" in response.headers["Location"]
    assert destination.read_bytes() == VALID_COOKIES
    assert "private-cookie-value" not in response.headers["Location"]


@pytest.mark.parametrize("webui", [clip_webui, repost_webui])
def test_control_panel_rejects_cookie_files_without_youtube_login(
    monkeypatch, tmp_path, webui
):
    destination = tmp_path / "cookies.txt"
    monkeypatch.setattr(webui, "_shared_cookie_path", lambda: destination)
    client = webui.create_app(testing=True).test_client()

    response = client.post(
        "/api/youtube-cookies",
        data={
            "file": (
                io.BytesIO(b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"),
                "cookies.txt",
            )
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert "Invalid+cookies+file" in response.headers["Location"]
    assert not destination.exists()


@pytest.mark.parametrize(
    "fetcher_module,fetcher_class,package_name",
    [
        (clip_fetcher_module, YouTubeFetcher, "yt_shorts_bot"),
        (repost_fetcher_module, ShortsFetcher, "yt_shorts_repost_bot"),
    ],
)
def test_fetchers_notice_shared_cookie_upload_without_restart(
    monkeypatch, tmp_path, fetcher_module, fetcher_class, package_name
):
    package_dir = tmp_path / package_name
    package_dir.mkdir(parents=True)
    shared = tmp_path / "cookies.txt"
    monkeypatch.setattr(fetcher_module, "BASE_DIR", package_dir)
    monkeypatch.setattr(fetcher_module, "YT_COOKIES_FILE", "")
    monkeypatch.setattr(fetcher_module, "YT_COOKIES_FROM_BROWSER", "")

    assert "cookiefile" not in fetcher_class._cookies_opts()
    shared.write_bytes(VALID_COOKIES)
    assert fetcher_class._cookies_opts()["cookiefile"] == str(shared)
    assert fetcher_class._cookies_look_valid(shared)


@pytest.mark.parametrize("state_class", [StateDB, RepostStateDB])
def test_legacy_skipped_age_gates_are_migrated_back_to_retryable(
    tmp_path, state_class
):
    db_path = tmp_path / f"{state_class.__module__.replace('.', '-')}.db"
    db = state_class(db_path)
    db.record_video_state(
        "age-source",
        status="SKIPPED",
        error_msg="Sign in to confirm your age. This video is age-restricted.",
        account="A",
    )
    db.record_video_state(
        "removed-source",
        status="SKIPPED",
        error_msg="Video unavailable: removed by uploader",
        account="A",
    )

    # Reopening applies the upgrade migration used by real existing databases.
    state_class(db_path)

    assert db.get_video_state("age-source", "A")["status"] == "SOURCE_AUTH_REQUIRED"
    assert not db.is_video_processed("age-source", "A")
    assert db.get_video_state("removed-source", "A")["status"] == "SKIPPED"
    assert db.is_video_processed("removed-source", "A")


@pytest.mark.parametrize(
    "uploader_module",
    [clip_uploader_module, repost_uploader_module],
)
def test_uploader_tracks_source_age_without_inventing_an_api_age_rating(
    tmp_path, uploader_module
):
    video = tmp_path / "mature.mp4"
    video.write_bytes(b"video")
    uploader = uploader_module.YouTubeUploader(state_db=object(), dry_run=True)

    result = uploader.upload_short(
        video_path=video,
        original_video_id="mature-source",
        original_title="Permitted mature source",
        info={"age_limit": 18},
    )

    assert result == uploader_module.UPLOAD_DRY_RUN
    assert uploader.last_metadata["source_age_limit"] == 18
    # Only explicit snippet/status fields are ever copied into the API body.
    assert "contentRating" not in uploader.last_metadata


def test_repost_scheduler_keeps_age_gate_retryable_and_tries_next_short(
    monkeypatch, tmp_path
):
    age_error = RuntimeError("Sign in to confirm your age; this video is age-restricted")

    class FakeFetcher:
        def __init__(self, **_kwargs):
            pass

        def fetch_channel_recent_shorts(self, *_args, **_kwargs):
            return [
                {
                    "video_id": "age-source",
                    "url": "https://www.youtube.com/shorts/age-source",
                    "title": "Age source",
                },
                {
                    "video_id": "good-source",
                    "url": "https://www.youtube.com/shorts/good-source",
                    "title": "Good source",
                },
            ]

    monkeypatch.setattr(repost_scheduler_module, "ShortsFetcher", FakeFetcher)
    db = RepostStateDB(tmp_path / "repost.db")
    scheduler = ShortsRepostScheduler(
        accounts=[
            {
                "name": "A",
                "enabled": True,
                "target_channels": ["https://www.youtube.com/@source/shorts"],
                "max_shorts_per_channel_cycle": 2,
                "max_daily_uploads": 10,
            }
        ],
        state_db=db,
    )
    attempted = []

    def fake_process(video_id, *_args, **_kwargs):
        attempted.append(video_id)
        if video_id == "age-source":
            raise age_error
        return True

    scheduler._process_one = fake_process

    assert scheduler.run_single_cycle(accounts=scheduler.accounts) == 1
    assert attempted == ["age-source", "good-source"]
    assert db.get_video_state("age-source", "A")["status"] == "SOURCE_AUTH_REQUIRED"
    assert not db.is_video_processed("age-source", "A")
