"""Chinese dubbing for Bilibili-bound clips."""
from __future__ import annotations

from pathlib import Path

import pytest

from yt_shorts_bot import social as clip_social
from yt_shorts_bot.dubbing import (
    CHINESE_VOICES,
    DEFAULT_VOICE,
    Cue,
    DubbingError,
    _atempo_chain,
    _to_seconds,
    dub_video,
    merge_cues,
    parse_srt,
)
from yt_shorts_bot.models import StateDB
from yt_shorts_bot.social import SOCIAL_EXPORTED, SocialDestinations

SRT = """1
00:00:00,500 --> 00:00:02,000
Hello there

2
00:00:02,100 --> 00:00:03,500
this is a test.

3
00:00:06,000 --> 00:00:08,000
<i>Second sentence</i> here
"""


def _write_srt(tmp_path: Path, text: str = SRT) -> Path:
    path = tmp_path / "clip.srt"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# SRT parsing
# ---------------------------------------------------------------------------
def test_parse_srt_reads_times_and_strips_markup(tmp_path):
    cues = parse_srt(_write_srt(tmp_path))
    assert [(c.start, c.end) for c in cues] == [(0.5, 2.0), (2.1, 3.5), (6.0, 8.0)]
    # Styling tags never reach the text-to-speech engine.
    assert cues[2].text == "Second sentence here"


def test_srt_millisecond_padding():
    # The field is a decimal fraction: ".5" == ".50" == ".500" == 500ms.
    # Reading "5" as 5ms instead would desync the whole dub.
    assert _to_seconds("00", "00", "01", "5") == 1.5
    assert _to_seconds("00", "00", "01", "50") == 1.5
    assert _to_seconds("00", "00", "01", "500") == 1.5
    assert _to_seconds("00", "00", "01", "004") == 1.004
    assert _to_seconds("01", "02", "03", "004") == 3723.004


def test_parse_srt_ignores_malformed_blocks(tmp_path):
    path = tmp_path / "bad.srt"
    path.write_text(
        "1\nnot a timestamp\nsome text\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nGood line\n\n"
        "3\n00:00:05,000 --> 00:00:04,000\nEnds before it starts\n",
        encoding="utf-8",
    )
    cues = parse_srt(path)
    assert [c.text for c in cues] == ["Good line"]


def test_merge_cues_builds_sentences():
    cues = parse_srt.__wrapped__ if False else [
        Cue(0.5, 2.0, "Hello there"),
        Cue(2.1, 3.5, "this is a test."),
        Cue(6.0, 8.0, "Second sentence here"),
    ]
    merged = merge_cues(cues)
    # The word-level chunks join; a long gap and a finished sentence split.
    assert len(merged) == 2
    assert merged[0].text == "Hello there this is a test."
    assert (merged[0].start, merged[0].end) == (0.5, 3.5)
    assert merged[1].text == "Second sentence here"


def test_merge_cues_respects_the_duration_cap():
    cues = [Cue(i * 1.0, i * 1.0 + 0.9, f"word{i}") for i in range(20)]
    merged = merge_cues(cues, max_duration=5.0)
    assert len(merged) > 1
    assert all(cue.duration <= 5.0 + 0.01 for cue in merged)


# ---------------------------------------------------------------------------
# Speed fitting
# ---------------------------------------------------------------------------
def test_atempo_chain_stays_inside_ffmpeg_limits():
    assert _atempo_chain(1.5) == "atempo=1.500000"
    # ffmpeg's atempo caps at 2.0, so bigger factors must be chained.
    assert _atempo_chain(2.5) == "atempo=2.0,atempo=1.250000"
    assert _atempo_chain(4.0) == "atempo=2.0,atempo=2.000000"
    for speed in (0.3, 1.0, 2.5, 4.0, 9.0):
        for part in _atempo_chain(speed).split(","):
            assert 0.5 <= float(part.split("=")[1]) <= 2.0


def test_voice_catalogue_has_a_valid_default():
    assert DEFAULT_VOICE in CHINESE_VOICES
    assert all(v.startswith("zh-") for v in CHINESE_VOICES)


# ---------------------------------------------------------------------------
# dub_video orchestration (ffmpeg stubbed out)
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_ffmpeg(monkeypatch):
    """Record ffmpeg invocations instead of running them."""
    calls: list[list[str]] = []

    def _run(args, timeout=900):
        calls.append(list(args))
        # Emulate ffmpeg writing its output file (always the last argument).
        Path(args[-1]).write_bytes(b"rendered")

    monkeypatch.setattr("yt_shorts_bot.dubbing._run_ffmpeg", _run)
    monkeypatch.setattr("yt_shorts_bot.dubbing.audio_duration", lambda path: 1.0)
    return calls


def test_dub_video_translates_then_speaks_every_cue(tmp_path, stub_ffmpeg):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    spoken: list[tuple[str, str]] = []
    seen: list[str] = []

    def _translate(texts, target):
        seen.append(target)
        return [f"中文:{t}" for t in texts]

    def _tts(text, path, voice):
        spoken.append((text, voice))
        Path(path).write_bytes(b"audio")
        return path

    out = tmp_path / "dubbed.mp4"
    dub_video(video, _write_srt(tmp_path), out,
              voice="zh-CN-YunxiNeural", target_language="zh-CN",
              video_duration=10.0, translator=_translate, tts=_tts,
              workdir=tmp_path / "w")

    assert seen == ["zh-CN"]
    # Merged into two sentences, each spoken with the chosen voice.
    assert len(spoken) == 2
    assert all(text.startswith("中文:") for text, _ in spoken)
    assert {voice for _, voice in spoken} == {"zh-CN-YunxiNeural"}
    assert out.is_file()


def test_dub_video_places_cues_at_their_timestamps(tmp_path, stub_ffmpeg):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    dub_video(video, _write_srt(tmp_path), tmp_path / "out.mp4",
              video_duration=10.0,
              translator=lambda texts, target: list(texts),
              tts=lambda text, path, voice: Path(path).write_bytes(b"a") or path,
              workdir=tmp_path / "w")
    mix = next(c for c in stub_ffmpeg if any("adelay" in str(a) for a in c))
    filters = mix[mix.index("-filter_complex") + 1]
    # Cues start at 0.5s and 6.0s -> 500ms and 6000ms delays.
    assert "adelay=500|500" in filters
    assert "adelay=6000|6000" in filters


def test_dub_video_ducks_the_original_audio(tmp_path, stub_ffmpeg):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    dub_video(video, _write_srt(tmp_path), tmp_path / "out.mp4",
              original_volume=0.2, video_duration=10.0,
              translator=lambda t, target: list(t),
              tts=lambda text, path, voice: Path(path).write_bytes(b"a") or path,
              workdir=tmp_path / "w")
    mux = stub_ffmpeg[-1]
    filters = mux[mux.index("-filter_complex") + 1]
    assert "volume=0.200" in filters
    # The picture is stream-copied, never re-encoded.
    assert "copy" in mux


def test_dub_video_can_fully_replace_the_audio(tmp_path, stub_ffmpeg):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    dub_video(video, _write_srt(tmp_path), tmp_path / "out.mp4",
              original_volume=0.0, video_duration=10.0,
              translator=lambda t, target: list(t),
              tts=lambda text, path, voice: Path(path).write_bytes(b"a") or path,
              workdir=tmp_path / "w")
    mux = stub_ffmpeg[-1]
    assert "-filter_complex" not in mux      # no mixing needed
    assert "1:a:0" in mux                    # dub used directly


def test_dub_video_needs_a_transcript(tmp_path, stub_ffmpeg):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    with pytest.raises(DubbingError, match="transcript"):
        dub_video(video, tmp_path / "missing.srt", tmp_path / "out.mp4")


def test_dub_video_rejects_a_bad_translator(tmp_path, stub_ffmpeg):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    with pytest.raises(DubbingError, match="different number"):
        dub_video(video, _write_srt(tmp_path), tmp_path / "out.mp4",
                  translator=lambda texts, target: ["only one"],
                  tts=lambda text, path, voice: path)


def test_dub_video_survives_one_failing_cue(tmp_path, stub_ffmpeg):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    calls = {"n": 0}

    def _flaky(text, path, voice):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tts hiccup")
        Path(path).write_bytes(b"a")
        return path

    out = tmp_path / "out.mp4"
    dub_video(video, _write_srt(tmp_path), out, video_duration=10.0,
              translator=lambda t, target: list(t), tts=_flaky,
              workdir=tmp_path / "w")
    # The surviving cue still produces a dub rather than losing the whole post.
    assert out.is_file()


# ---------------------------------------------------------------------------
# Wiring into the Bilibili path
# ---------------------------------------------------------------------------
def _account(tmp_path, **extra):
    account = {
        "name": "Chan", "bilibili_enabled": True, "bilibili_mode": "manual",
        "bilibili_export_dir": str(tmp_path / "exports"),
        "bilibili_dub_enabled": True,
    }
    account.update(extra)
    return account


def test_bilibili_export_uses_the_dubbed_file(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"original")
    srt = _write_srt(tmp_path)
    seen: dict = {}

    def _fake_dub(src, srt_path, out, **kwargs):
        seen.update(kwargs)
        seen["src"] = Path(src)
        Path(out).write_bytes(b"dubbed-audio")
        return Path(out)

    monkeypatch.setattr("yt_shorts_bot.dubbing.dub_video", _fake_dub)
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    result = destinations.crosspost(
        _account(tmp_path, bilibili_dub_voice="zh-CN-YunxiNeural"),
        "v1", video, metadata={"title": "T"}, srt_path=srt)

    assert result == {"bilibili": SOCIAL_EXPORTED}
    assert seen["voice"] == "zh-CN-YunxiNeural"
    # The EXPORTED file is the dub, not the original.
    exported = next((tmp_path / "exports" / "Chan").glob("*.mp4"))
    assert exported.read_bytes() == b"dubbed-audio"


def test_dubbing_failure_falls_back_to_the_original(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"original")

    def _boom(*args, **kwargs):
        raise DubbingError("tts is down")

    monkeypatch.setattr("yt_shorts_bot.dubbing.dub_video", _boom)
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    result = destinations.crosspost(_account(tmp_path), "v1", video,
                                    metadata={"title": "T"},
                                    srt_path=_write_srt(tmp_path))
    # A failed dub must never cost the post.
    assert result == {"bilibili": SOCIAL_EXPORTED}
    exported = next((tmp_path / "exports" / "Chan").glob("*.mp4"))
    assert exported.read_bytes() == b"original"


def test_dubbing_without_a_transcript_posts_the_original(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"original")
    monkeypatch.setattr(
        "yt_shorts_bot.dubbing.dub_video",
        lambda *a, **k: pytest.fail("must not dub without a transcript"))
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    result = destinations.crosspost(_account(tmp_path), "v1", video,
                                    metadata={"title": "T"}, srt_path=None)
    assert result == {"bilibili": SOCIAL_EXPORTED}
    exported = next((tmp_path / "exports" / "Chan").glob("*.mp4"))
    assert exported.read_bytes() == b"original"


def test_dubbing_off_never_touches_the_clip(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"original")
    monkeypatch.setattr("yt_shorts_bot.dubbing.dub_video",
                        lambda *a, **k: pytest.fail("dubbing is disabled"))
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"))
    destinations.crosspost(_account(tmp_path, bilibili_dub_enabled=False),
                           "v1", video, metadata={"title": "T"},
                           srt_path=_write_srt(tmp_path))
    exported = next((tmp_path / "exports" / "Chan").glob("*.mp4"))
    assert exported.read_bytes() == b"original"


def test_tiktok_is_never_dubbed(tmp_path, monkeypatch):
    """Dubbing is a Bilibili setting: other destinations keep their audio."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"original")
    monkeypatch.setattr("yt_shorts_bot.dubbing.dub_video",
                        lambda *a, **k: pytest.fail("TikTok must not be dubbed"))
    account = _account(tmp_path, bilibili_enabled=False, tiktok_enabled=True,
                       tiktok_open_id="o", tiktok_access_token="t")
    destinations = SocialDestinations(state_db=StateDB(tmp_path / "s.db"),
                                      dry_run=True)
    result = destinations.crosspost(account, "v1", video, metadata={"title": "T"},
                                    srt_path=_write_srt(tmp_path))
    assert set(result) == {"tiktok"}
