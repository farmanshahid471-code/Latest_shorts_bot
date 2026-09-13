"""Bilibili manual mode: save the clip + a notes file instead of uploading."""
from __future__ import annotations

from pathlib import Path

import pytest

from yt_shorts_bot import social as clip_social
from yt_shorts_bot.manual_export import (
    build_notes,
    export_for_manual_upload,
    safe_filename,
)
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.social import SOCIAL_EXPORTED, SocialDestinations


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------
def test_safe_filename_strips_illegal_characters():
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == "a-b-c-d-e-f-g-h-i-j"
    assert safe_filename("  spaced   out  ") == "spaced-out"
    assert safe_filename("") == "clip"
    assert safe_filename("...") == "clip"
    # Windows silently drops trailing dots/spaces, which would break lookups.
    assert not safe_filename("name...").endswith(".")
    # Reserved DOS device names cannot be filenames.
    assert safe_filename("CON") == "_CON"
    # Unicode is preserved so Chinese titles stay readable.
    assert safe_filename("这个猫太好笑了") == "这个猫太好笑了"
    # Long titles are truncated to stay under filesystem limits.
    assert len(safe_filename("x" * 500)) <= 60


# ---------------------------------------------------------------------------
# Notes file
# ---------------------------------------------------------------------------
def test_notes_contain_every_upload_field():
    notes = build_notes(
        {"title": "My clip", "description": "Desc here", "tags": ["funny", "daily life"]},
        account_name="Chan", video_id="v1", video_filename="v1.mp4",
        source_url="https://youtu.be/x",
        extra_fields={"分区 / category (tid)": "21", "copyright": "转载 / Repost"},
    )
    assert "My clip" in notes and "Desc here" in notes
    assert "funny, daily life" in notes          # comma form, spaces kept
    assert "#dailylife" in notes                 # hashtag form, spaces removed
    assert "21" in notes and "转载 / Repost" in notes
    assert "https://youtu.be/x" in notes and "v1.mp4" in notes


def test_notes_omit_empty_optional_fields():
    notes = build_notes({"title": "T"}, video_filename="a.mp4",
                        extra_fields={"repost source": "", "copyright": "自制"})
    assert "REPOST SOURCE" not in notes
    assert "自制" in notes
    assert "(none)" in notes  # no tags


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def test_export_writes_matching_video_and_notes(tmp_path):
    clip = tmp_path / "render.mp4"
    clip.write_bytes(b"data")
    video, notes = export_for_manual_upload(
        clip, tmp_path / "out", {"title": "Hello world", "tags": ["a"]},
        account_name="My Chan", video_id="vid1",
    )
    # One subfolder per account, and the pair shares a stem.
    assert video.parent.name == "My-Chan"
    assert notes.stem == video.stem and notes.suffix == ".txt"
    assert video.read_bytes() == b"data"
    # The original is copied, not moved: the pipeline still cleans it up.
    assert clip.exists()
    assert "Hello world" in notes.read_text(encoding="utf-8")
    # The notes reference the real, final filename.
    assert video.name in notes.read_text(encoding="utf-8")


def test_export_never_overwrites_a_previous_file(tmp_path):
    clip = tmp_path / "render.mp4"
    clip.write_bytes(b"one")
    meta = {"title": "Same title"}
    first, first_notes = export_for_manual_upload(
        clip, tmp_path / "out", meta, account_name="A", video_id="v1")
    clip.write_bytes(b"two")
    second, second_notes = export_for_manual_upload(
        clip, tmp_path / "out", meta, account_name="A", video_id="v1")
    assert first != second
    assert first.read_bytes() == b"one"   # untouched
    assert second.read_bytes() == b"two"
    # Each notes file still points at its own video.
    assert second.name in second_notes.read_text(encoding="utf-8")
    assert first_notes != second_notes


def test_export_copies_the_cover_when_present(tmp_path):
    clip = tmp_path / "render.mp4"
    clip.write_bytes(b"d")
    cover = tmp_path / "thumb.jpg"
    cover.write_bytes(b"img")
    video, _ = export_for_manual_upload(
        clip, tmp_path / "out", {"title": "T", "thumbnail_path": str(cover)},
        account_name="A")
    assert video.with_suffix(".jpg").read_bytes() == b"img"


def test_export_rejects_a_missing_video(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_for_manual_upload(tmp_path / "nope.mp4", tmp_path / "out", {"title": "T"})


# ---------------------------------------------------------------------------
# Wiring through the cross-post path
# ---------------------------------------------------------------------------
def _account(tmp_path, **extra):
    account = {
        "name": "My Channel", "bilibili_enabled": True, "bilibili_mode": "manual",
        "bilibili_export_dir": str(tmp_path / "exports"),
        "bilibili_tid": "21", "bilibili_copyright": "2",
        "bilibili_source": "https://youtu.be/abc",
    }
    account.update(extra)
    return account


def test_manual_mode_exports_without_any_credentials(tmp_path):
    clip = tmp_path / "render.mp4"
    clip.write_bytes(b"v")
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    result = destinations.crosspost(
        _account(tmp_path), "abc123", clip,
        metadata={"title": "Funny cat", "tags": ["cats"]},
    )
    assert result == {"bilibili": SOCIAL_EXPORTED}
    exports = tmp_path / "exports" / "My-Channel"
    assert len(list(exports.glob("*.mp4"))) == 1
    notes = next(exports.glob("*.txt")).read_text(encoding="utf-8")
    # Bilibili-specific form fields ride along in the notes.
    assert "Funny cat" in notes and "21" in notes and "转载 / Repost" in notes


def test_manual_export_is_idempotent(tmp_path):
    clip = tmp_path / "render.mp4"
    clip.write_bytes(b"v")
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    account, meta = _account(tmp_path), {"title": "T", "tags": []}
    destinations.crosspost(account, "abc123", clip, metadata=meta)
    again = destinations.crosspost(account, "abc123", clip, metadata=meta)
    assert again == {"bilibili": "ALREADY_POSTED"}
    assert len(list((tmp_path / "exports" / "My-Channel").glob("*.mp4"))) == 1


def test_manual_mode_never_calls_the_api(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("manual mode must not touch the Bilibili API")

    monkeypatch.setattr(clip_social, "BilibiliUploader", _boom)
    clip = tmp_path / "render.mp4"
    clip.write_bytes(b"v")
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    result = destinations.crosspost(_account(tmp_path), "v1", clip,
                                    metadata={"title": "T"})
    assert result == {"bilibili": SOCIAL_EXPORTED}


def test_api_mode_is_unaffected_and_still_needs_credentials(tmp_path):
    clip = tmp_path / "render.mp4"
    clip.write_bytes(b"v")
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    result = destinations.crosspost(
        _account(tmp_path, bilibili_mode="api"), "v1", clip, metadata={"title": "T"})
    assert result == {"bilibili": "SKIPPED"}   # missing client id/secret/token
    assert not (tmp_path / "exports").exists()


def test_manual_export_uses_per_platform_title_override(tmp_path):
    clip = tmp_path / "render.mp4"
    clip.write_bytes(b"v")
    account = _account(tmp_path, title_prefix="Shared", bilibili_title_prefix="B站")
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    destinations.crosspost(account, "v1", clip,
                           metadata={"title": "Shared Funny cat", "tags": []})
    notes = next((tmp_path / "exports" / "My-Channel").glob("*.txt"))
    assert "B站 Funny cat" in notes.read_text(encoding="utf-8")
