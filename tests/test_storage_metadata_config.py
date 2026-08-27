from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from yt_shorts_bot.hashtags import build_hashtags, make_catchy_title
from yt_shorts_bot.storage import CloudStorageManager
from yt_shorts_bot.uploader import YouTubeUploader


class Paginator:
    def __init__(self, objects=None, error=None):
        self.objects = objects or []
        self.error = error

    def paginate(self, **_kwargs):
        if self.error:
            raise self.error
        return [{"Contents": self.objects}]


class FakeS3:
    def __init__(self, objects=None, error=None):
        self.objects = objects or []
        self.error = error
        self.deleted = []
        self.uploaded = []

    def get_paginator(self, _name):
        return Paginator(self.objects, self.error)

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)

    def upload_file(self, **kwargs):
        self.uploaded.append(kwargs)


def manager_with(client, limit=1000):
    manager = CloudStorageManager(
        access_key_id="configured",
        secret_access_key="configured",
        endpoint_url="https://r2.invalid",
        bucket_name="bucket",
        max_bucket_bytes=limit,
    )
    manager.client = client
    return manager


def test_r2_incoming_size_is_counted_and_only_bot_keys_are_pruned():
    now = datetime.now(timezone.utc)
    client = FakeS3(
        [
            {"Key": "unrelated/keep.bin", "Size": 400, "LastModified": now},
            {"Key": "shorts/a.mp4", "Size": 350, "LastModified": now},
            {"Key": "reposts/b.mp4", "Size": 200, "LastModified": now},
        ]
    )
    manager = manager_with(client, limit=1000)
    manager.enforce_storage_limit(incoming_bytes=200)
    assert "unrelated/keep.bin" not in client.deleted
    assert client.deleted


def test_r2_refuses_upload_when_usage_cannot_be_verified(tmp_path):
    client = FakeS3(error=RuntimeError("list failed"))
    manager = manager_with(client)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 100)
    with pytest.raises(RuntimeError, match="usage could not be verified"):
        manager.upload_file(video)
    assert not client.uploaded


def test_unconfigured_r2_skips_instead_of_simulating_success(tmp_path):
    manager = CloudStorageManager(
        access_key_id="",
        secret_access_key="",
        endpoint_url="",
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    assert manager.upload_file(video) is None


def test_metadata_remains_user_controlled_and_source_suffix_is_not_a_tag():
    info = {"title": "Source #sourceTag"}
    assert build_hashtags(
        info=info,
        transcript_text="automatic content words",
        title_hashtags="mine, ExactTag",
        smart_titles=True,
    ) == ["mine", "exacttag"]
    title = make_catchy_title(
        info=info,
        title_prefix="PREFIX",
        title_hashtags="mine",
        smart_titles=True,
    )
    assert title == "PREFIX Source #mine"
    metadata = YouTubeUploader.generate_short_metadata(
        original_title=info["title"],
        channel_name="https://www.youtube.com/@Owner/shorts",
        info=info,
        title_prefix="PREFIX",
        title_hashtags="mine",
    )
    assert metadata["tags"] == ["mine", "Owner"]
    assert "shorts" not in metadata["tags"]


def test_env_templates_have_no_duplicate_keys():
    root = Path(__file__).resolve().parents[1]
    for template in (
        root / "yt_shorts_bot" / ".env.example",
        root / "yt_shorts_repost_bot" / ".env.example",
    ):
        keys = [
            line.split("=", 1)[0].strip()
            for line in template.read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        ]
        assert len(keys) == len(set(keys)), template
