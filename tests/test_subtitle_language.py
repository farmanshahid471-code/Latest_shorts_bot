"""Tests: subtitles follow the source language (never dubbing) + language tagging."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import yt_shorts_bot.processor as processor_module
from yt_shorts_bot.processor import VideoProcessor
from yt_shorts_bot.uploader import YouTubeUploader


def fake_whisper_module(monkeypatch, loaded: dict):
    class FakeWhisperModel:
        def __init__(self, model_size, **kwargs):
            loaded["model"] = model_size

        def transcribe(self, *_a, **_k):
            return iter(()), SimpleNamespace(language="vi", language_probability=0.87)

    module = SimpleNamespace(WhisperModel=FakeWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", module)


def test_english_only_model_is_auto_upgraded_to_multilingual(monkeypatch):
    loaded = {}
    fake_whisper_module(monkeypatch, loaded)
    monkeypatch.setattr(processor_module, "WHISPER_LANGUAGE", "auto")
    processor = VideoProcessor(model_size="tiny.en")

    processor._get_whisper_model()
    assert loaded["model"] == "tiny"  # english-only must NOT force English


def test_english_only_model_upgraded_even_when_english_configured(monkeypatch):
    """Subtitles must follow the SOURCE language, so *.en is never kept: it
    cannot detect French/Vietnamese/Urdu and would force English gibberish."""
    loaded = {}
    fake_whisper_module(monkeypatch, loaded)
    monkeypatch.setattr(processor_module, "WHISPER_LANGUAGE", "en")
    processor = VideoProcessor(model_size="tiny.en")

    processor._get_whisper_model()
    assert loaded["model"] == "tiny"  # NOT tiny.en - detection needs multilingual


def test_empty_language_setting_is_treated_as_auto_not_english(monkeypatch):
    loaded = {}
    fake_whisper_module(monkeypatch, loaded)
    monkeypatch.setattr(processor_module, "WHISPER_LANGUAGE", "")
    processor = VideoProcessor(model_size="base.en")

    processor._get_whisper_model()
    assert loaded["model"] == "base"  # "" must not force an English-only model


def test_detection_state_resets_when_transcription_starts(monkeypatch, tmp_path):
    fake_whisper_module(monkeypatch, {})

    class Boom(Exception):
        pass

    def fail_transcribe(*_a, **_k):
        raise Boom()

    import yt_shorts_bot.processor as mod
    monkeypatch.setattr(mod, "WHISPER_LANGUAGE", "auto")
    processor = VideoProcessor(model_size="base")
    processor.detected_language = "en"
    processor.detected_language_probability = 0.99
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stub")

    # Force the inner model used by transcribe() to fail AFTER the reset.
    model = processor._get_whisper_model()
    model.transcribe = fail_transcribe
    with pytest.raises(Boom):
        processor.transcribe_and_generate_srt(source, tmp_path / "clip.srt")
    assert processor.detected_language == ""
    assert processor.detected_language_probability == 0.0


def test_multilingual_base_is_used_when_language_auto(monkeypatch):
    loaded = {}
    fake_whisper_module(monkeypatch, loaded)
    monkeypatch.setattr(processor_module, "WHISPER_LANGUAGE", "auto")
    processor = VideoProcessor(model_size="base")

    processor._get_whisper_model()
    assert loaded["model"] == "base"


def test_forced_language_never_overrides_detection(monkeypatch, tmp_path):
    """WHISPER_LANGUAGE=en must NOT force en: the transcribe call always uses
    auto-detection (language=None) and the detected language wins."""
    calls: list = []

    class RecordingWhisperModel:
        def __init__(self, model_size, **kwargs):
            pass

        def transcribe(self, path, word_timestamps=True, language=None):
            calls.append(language)
            return iter(()), SimpleNamespace(language="fr", language_probability=0.95)

    import yt_shorts_bot.processor as mod
    module = SimpleNamespace(WhisperModel=RecordingWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(mod, "WHISPER_LANGUAGE", "en")
    processor = VideoProcessor(model_size="base")
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stub")

    processor.transcribe_and_generate_srt(source, tmp_path / "clip.srt")
    assert calls == [None]  # detected first, never forced to 'en'
    assert processor.detected_language == "fr"
    assert processor.detected_language_probability == pytest.approx(0.95)


def test_configured_language_is_only_a_low_confidence_fallback(monkeypatch, tmp_path):
    """WHISPER_LANGUAGE is used ONLY when detection fails/is uncertain."""
    calls: list = []

    class UncertainWhisperModel:
        def __init__(self, model_size, **kwargs):
            pass

        def transcribe(self, path, word_timestamps=True, language=None):
            calls.append(language)
            if language is None:
                return iter(()), SimpleNamespace(language="", language_probability=0.0)
            return iter(()), SimpleNamespace(language=language, language_probability=0.99)

    import yt_shorts_bot.processor as mod
    module = SimpleNamespace(WhisperModel=UncertainWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(mod, "WHISPER_LANGUAGE", "ur")
    processor = VideoProcessor(model_size="base")
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stub")

    processor.transcribe_and_generate_srt(source, tmp_path / "clip.srt")
    assert calls == [None, "ur"]  # detect first, fallback only when detection empty
    assert processor.detected_language == "ur"


def test_detected_language_and_probability_are_recorded(monkeypatch, tmp_path):
    fake_whisper_module(monkeypatch, {})
    monkeypatch.setattr(processor_module, "WHISPER_LANGUAGE", "auto")
    processor = VideoProcessor(model_size="base")
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stub")

    processor.transcribe_and_generate_srt(source, tmp_path / "clip.srt")
    assert processor.detected_language == "vi"
    assert processor.detected_language_probability == pytest.approx(0.87)


def test_language_code_normalization():
    assert YouTubeUploader._normalize_language_code("VI") == "vi"
    assert YouTubeUploader._normalize_language_code("eng-US") == "en"
    assert YouTubeUploader._normalize_language_code("uz") == "uz"
    assert YouTubeUploader._normalize_language_code("zzz") == ""  # unknown 3-letter
    assert YouTubeUploader._normalize_language_code("") == ""


def test_resolve_content_language_policy(monkeypatch):
    import yt_shorts_bot.uploader as uploader_module

    monkeypatch.setattr(uploader_module, "VIDEO_LANGUAGE", "auto")
    assert YouTubeUploader.resolve_content_language("vi") == "vi"
    assert YouTubeUploader.resolve_content_language("") == ""

    monkeypatch.setattr(uploader_module, "VIDEO_LANGUAGE", "ur")
    assert YouTubeUploader.resolve_content_language("en") == "ur"

    monkeypatch.setattr(uploader_module, "VIDEO_LANGUAGE", "off")
    assert YouTubeUploader.resolve_content_language("vi") == ""


def test_metadata_carries_content_language():
    meta = YouTubeUploader.generate_short_metadata(
        original_title="Escape 100 Cops",
        content_language="vi",
    )
    assert meta["content_language"] == "vi"


def test_upload_body_sets_default_language(monkeypatch, tmp_path):
    """The API body must tag vi (defaultLanguage) without touching audio."""
    import yt_shorts_bot.uploader as uploader_module

    monkeypatch.setattr(uploader_module, "VIDEO_LANGUAGE", "auto")
    uploader = YouTubeUploader()

    captured: dict = {}

    class FakeRequest:
        def next_chunk(self, num_retries=3):
            return None, {"id": "ytshort_xyz"}

    class FakeVideos:
        def insert(self, part, body, media_body):
            assert part == "snippet,status"
            captured["defaultLanguage"] = body["snippet"].get("defaultLanguage")
            captured["defaultAudioLanguage"] = body["snippet"].get("defaultAudioLanguage")
            return FakeRequest()

    class FakeService:
        def videos(self):
            return FakeVideos()

    class FakeStateDB:
        def reserve_upload_slot(self, max_daily_uploads=10, account="", lease_minutes=120):
            return "res1", 9

        def release_upload_reservation(self, reservation_id):
            pass

        def record_upload(self, *args, **kwargs):
            pass

    uploader.state_db = FakeStateDB()
    monkeypatch.setattr(uploader, "_get_authenticated_service", lambda interactive=True: FakeService())
    monkeypatch.setattr(uploader, "_verify_channel", lambda service, expected="", expected_channel_id="": (True, "Beast Snippets"))
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x")

    short_id = uploader.upload_short(
        video_path=path,
        original_video_id="id1",
        original_title="T",
        account="default",
        content_language="vi",
        expected_channel="Beast Snippets",
    )
    assert short_id == "ytshort_xyz"
    assert captured["defaultLanguage"] == "vi"
    assert captured["defaultAudioLanguage"] == "vi"
