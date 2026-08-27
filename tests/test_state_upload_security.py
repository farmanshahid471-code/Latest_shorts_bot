from __future__ import annotations

import subprocess
import threading
from pathlib import Path


from yt_shorts_bot.models import StateDB
from yt_shorts_bot.uploader import (
    UPLOAD_AUTH_REQUIRED,
    UPLOAD_DRY_RUN,
    YouTubeUploader,
    is_real_upload_id,
)


def make_video_stub(path: Path) -> Path:
    path.write_bytes(b"not-empty-video-for-mocked-api")
    return path


def test_retryable_states_are_not_terminal(tmp_path):
    db = StateDB(tmp_path / "state.db")
    for state in (
        "UPLOADED_R2",
        "PENDING_UPLOAD",
        "QUOTA_WAIT",
        "DRY_RUN_READY",
        "AUTH_REQUIRED",
        "UPLOAD_FAILED",
        "CHANNEL_MISMATCH",
        "PROCESSING_FAILED",
    ):
        db.record_video_state("video", status=state, account="A")
        assert not db.is_video_processed("video", "A"), state
    db.record_video_state("video", status="UPLOADED_YOUTUBE", account="A")
    assert db.is_video_processed("video", "A")


def test_dry_run_and_missing_auth_do_not_record_upload_or_quota(tmp_path):
    db = StateDB(tmp_path / "state.db")
    video = make_video_stub(tmp_path / "video.mp4")
    dry = YouTubeUploader(
        client_secret_file=tmp_path / "missing.json",
        token_file=tmp_path / "missing-token.json",
        state_db=db,
        dry_run=True,
    )
    result = dry.upload_short(
        video,
        "source1",
        "Title",
        account="A",
        expected_channel="Channel A",
    )
    assert result == UPLOAD_DRY_RUN
    assert db.get_uploads_in_last_24_hours(account="A") == 0
    assert not db.is_video_processed("source1", "A")

    live = YouTubeUploader(
        client_secret_file=tmp_path / "missing.json",
        token_file=tmp_path / "missing-token.json",
        state_db=db,
        dry_run=False,
    )
    result = live.upload_short(
        video,
        "source2",
        "Title",
        account="A",
        expected_channel="Channel A",
    )
    assert result == UPLOAD_AUTH_REQUIRED
    assert db.get_uploads_in_last_24_hours(account="A") == 0


class _Execute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Channels:
    def __init__(self, payload):
        self.payload = payload

    def list(self, **_kwargs):
        return _Execute(self.payload)


class _UploadRequest:
    def next_chunk(self, num_retries=0):
        assert num_retries == 3
        return None, {"id": "realYoutubeId"}


class _Videos:
    def insert(self, **_kwargs):
        return _UploadRequest()


class FakeYouTube:
    def __init__(self, channel_payload):
        self.channel_payload = channel_payload

    def channels(self):
        return _Channels(self.channel_payload)

    def videos(self):
        return _Videos()


def test_real_upload_reserves_and_records_exactly_once(tmp_path):
    db = StateDB(tmp_path / "state.db")
    video = make_video_stub(tmp_path / "video.mp4")
    db.record_video_state("source", status="PENDING_UPLOAD", account="A")
    uploader = YouTubeUploader(state_db=db, dry_run=False)
    uploader.youtube_service = FakeYouTube(
        {"items": [{"id": "UC123", "snippet": {"title": "Channel A"}}]}
    )
    result = uploader.upload_short(
        video,
        "source",
        "Title",
        account="A",
        account_max_daily=1,
        expected_channel="Channel A",
        expected_channel_id="UC123",
    )
    assert result == "realYoutubeId"
    assert is_real_upload_id(result)
    assert db.get_uploads_in_last_24_hours(account="A") == 1
    assert db.is_video_processed("source", "A")
    # Unique source/account index prevents duplicate quota rows.
    db.record_upload("source", "realYoutubeId", account="A")
    assert db.get_uploads_in_last_24_hours(account="A") == 1


def test_upload_setup_failure_releases_quota_reservation(monkeypatch, tmp_path):
    import yt_shorts_bot.uploader as uploader_module

    db = StateDB(tmp_path / "state.db")
    video = make_video_stub(tmp_path / "video.mp4")
    uploader = YouTubeUploader(state_db=db, dry_run=False)
    uploader.youtube_service = FakeYouTube(
        {"items": [{"id": "UC123", "snippet": {"title": "Channel A"}}]}
    )

    def fail_media(*_args, **_kwargs):
        raise RuntimeError("media setup failed")

    monkeypatch.setattr(uploader_module, "MediaFileUpload", fail_media)
    result = uploader.upload_short(
        video,
        "source",
        "Title",
        account="A",
        account_max_daily=1,
        expected_channel="Channel A",
        expected_channel_id="UC123",
    )
    assert result is None
    assert db.can_upload_today(1, "A") == (True, 1)


def test_channel_lock_is_exact_and_fails_closed():
    service = FakeYouTube(
        {"items": [{"id": "UC1", "snippet": {"title": "PeterAKing"}}]}
    )
    assert YouTubeUploader._verify_channel(service, "PeterAKing") == (
        True,
        "PeterAKing",
    )
    assert YouTubeUploader._verify_channel(service, "Peter")[0] is False
    assert YouTubeUploader._verify_channel(service, "anything", "UC1")[0] is True
    broken = FakeYouTube(RuntimeError("network unavailable"))
    assert YouTubeUploader._verify_channel(broken, "PeterAKing")[0] is False


def test_atomic_video_claims_and_quota_reservations(tmp_path):
    db = StateDB(tmp_path / "state.db")
    claims = []
    barrier = threading.Barrier(5)

    def claim_worker():
        barrier.wait()
        claims.append(db.claim_video("same", "A"))

    threads = [threading.Thread(target=claim_worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(value is not None for value in claims) == 1

    reservations = []
    barrier2 = threading.Barrier(5)

    def reserve_worker():
        barrier2.wait()
        reservations.append(db.reserve_upload_slot(2, "B")[0])

    threads = [threading.Thread(target=reserve_worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(value is not None for value in reservations) == 2


def test_delete_account_data_resets_history_quota_and_claims(tmp_path):
    db = StateDB(tmp_path / "state.db")
    db.record_video_state("video", status="UPLOADED_YOUTUBE", account="A")
    db.record_upload("video", "youtube-id", account="A")
    assert db.claim_video("working", "A")
    assert db.reserve_upload_slot(5, "A")[0]
    db.delete_account_data("A")
    assert db.get_video_state("video", "A") is None
    assert db.get_uploads_in_last_24_hours(account="A") == 0
    assert db.claim_video("working", "A")
    assert db.reserve_upload_slot(1, "A")[0]


def test_total_upload_count_uses_all_accounts(tmp_path):
    db = StateDB(tmp_path / "state.db")
    db.record_upload("a", "id-a", account="A")
    db.record_upload("b", "id-b", account="B")
    assert db.get_uploads_in_last_24_hours() == 2
    assert db.get_uploads_in_last_24_hours(account="A") == 1


def test_clean_channel_tag_skips_generic_feed_suffixes():
    clean = YouTubeUploader._clean_channel_tag
    assert clean("https://www.youtube.com/@Owner/shorts") == "Owner"
    assert clean("https://www.youtube.com/@Owner/videos") == "Owner"
    assert clean("@Owner") == "Owner"


def test_sensitive_runtime_files_are_not_tracked():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    forbidden = (
        "/.env",
        "accounts.json",
        "client_secret.json",
        "token.json",
        "cookies.txt",
        ".db",
        ".log",
        "/temp_clips/",
        "/finished_shorts/",
        "/ffmpeg/bin/",
    )
    assert not [
        path
        for path in tracked
        if not path.endswith(".env.example")
        and any(marker in path for marker in forbidden)
    ]
