"""Tests for combined Most Replayed + voice-excitement moment selection."""
from __future__ import annotations

import copy

import pytest

import yt_shorts_bot.fetcher as fetcher_module
from yt_shorts_bot.fetcher import YouTubeFetcher


def make_heatmap(duration: float = 600.0, bucket_sec: float = 10.0, peak_at: float = 200.0):
    """Build a heatmap with one clear Most Replayed peak."""
    buckets = []
    t = 0.0
    while t < duration:
        value = 10.0 if abs(t - peak_at) < bucket_sec else 1.0
        buckets.append({"start_time": t, "end_time": t + bucket_sec, "value": value})
        t += bucket_sec
    return buckets


class FakeYDL:
    """Returns the configured info dict for every extraction (metadata only)."""

    info = {}
    extract_count = 0

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        self.__class__.extract_count += 1
        return copy.deepcopy(self.info)


@pytest.fixture
def fake_ydl(monkeypatch):
    info = {
        "id": "abcdefghijk",
        "title": "Test video",
        "url": "https://example.com/stream",
        "duration": 600,
    }
    FakeYDL.info = info
    FakeYDL.extract_count = 0
    monkeypatch.setattr(fetcher_module.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(fetcher_module, "FFMPEG_PATH", "ffmpeg")
    return info


def synthesize_features(start: float) -> dict:
    """Pretend t < 30s is very loud + high-pitched; everything else quiet."""
    if start < 30.0:
        return {"energy": 0.9, "centroid": 4000.0, "flux": 0.8}
    return {"energy": 0.05, "centroid": 500.0, "flux": 0.02}


def make_fetcher(strategy="combined", **kwargs):
    return YouTubeFetcher(strategy=strategy, **kwargs)


def test_normalize_scores_handles_equal_and_missing():
    assert YouTubeFetcher._normalize_scores([3.0, 3.0, 3.0]) == [0.5, 0.5, 0.5]
    assert YouTubeFetcher._normalize_scores([0.0, 5.0]) == [0.0, 1.0]
    assert YouTubeFetcher._normalize_scores([]) == []


def test_invalid_strategy_is_normalized_to_combined():
    assert make_fetcher(strategy="bogus").strategy == "combined"
    assert make_fetcher(strategy="HEATMAP").strategy == "heatmap"


def test_combined_uses_heatmap_and_audio(monkeypatch, fake_ydl):
    """Both signals must contribute to the combined score."""
    info = fake_ydl
    info["heatmap"] = make_heatmap(peak_at=200.0)
    fetcher = make_fetcher()
    monkeypatch.setattr(
        fetcher, "_measure_audio_features",
        lambda stream_url, start, dur: synthesize_features(start),
    )

    windows, used_heatmap, used_audio = fetcher._build_ranked_windows(
        "https://www.youtube.com/watch?v=abcdefghijk", info, 600.0, count=1
    )
    assert used_heatmap and used_audio
    assert windows
    best = windows[0]
    assert best["source"] == "combined"
    assert best["heatmap_score"] is not None
    assert best["audio_score"] is not None
    # Default weights (0.55/0.45): the strong 200s Most Replayed peak wins, but
    # its combined score is the weighted blend (heat=1.0, audio≈0.0), not 1.0.
    assert 195.0 <= (best["start"] + best["end"]) / 2.0 <= 205.0
    assert best["score"] == pytest.approx(0.55, abs=0.05)
    assert best["start"] < best["end"]


def test_audio_weight_can_override_heatmap(monkeypatch, fake_ydl):
    """A loud/high-pitched early moment wins when audio weight dominates."""
    info = fake_ydl
    info["heatmap"] = make_heatmap(peak_at=200.0)
    fetcher = make_fetcher(audio_weight=0.9, heatmap_weight=0.1)
    monkeypatch.setattr(
        fetcher, "_measure_audio_features",
        lambda stream_url, start, dur: synthesize_features(start),
    )

    windows, _h, _a = fetcher._build_ranked_windows(
        "https://www.youtube.com/watch?v=abcdefghijk", info, 600.0, count=1
    )
    assert windows
    assert windows[0]["start"] < 60.0


def test_missing_heatmap_falls_back_to_audio(monkeypatch, fake_ydl):
    """Live VODs without heatmap must rank by voice excitement instead."""
    info = fake_ydl
    fetcher = make_fetcher()
    monkeypatch.setattr(
        fetcher, "_measure_audio_features",
        lambda stream_url, start, dur: synthesize_features(start),
    )

    windows, used_heatmap, used_audio = fetcher._build_ranked_windows(
        "https://www.youtube.com/watch?v=abcdefghijk", info, 600.0, count=1
    )
    assert not used_heatmap
    assert used_audio
    assert windows[0]["source"] == "audio"
    assert windows[0]["start"] < 60.0


def test_missing_audio_falls_back_to_heatmap(monkeypatch, fake_ydl):
    """If every audio probe fails, Most Replayed alone must still rank."""
    info = fake_ydl
    info["heatmap"] = make_heatmap(peak_at=200.0)
    fetcher = make_fetcher()

    def fail_probe(stream_url, start, dur):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(fetcher, "_measure_audio_features", fail_probe)

    windows, used_heatmap, used_audio = fetcher._build_ranked_windows(
        "https://www.youtube.com/watch?v=abcdefghijk", info, 600.0, count=1
    )
    assert used_heatmap
    assert not used_audio
    assert windows
    assert windows[0]["source"] == "heatmap"
    # Heatmap peak is at ~200s, so the best window must be near it.
    assert 150.0 <= (windows[0]["start"] + windows[0]["end"]) / 2.0 <= 250.0


def test_extract_window_annotates_strategy_metadata(monkeypatch, fake_ydl):
    info = fake_ydl
    info["heatmap"] = make_heatmap(peak_at=200.0)
    fetcher = make_fetcher()
    monkeypatch.setattr(
        fetcher, "_measure_audio_features",
        lambda stream_url, start, dur: synthesize_features(start),
    )

    out_info, peak, start, end = fetcher.extract_heatmap_and_select_window(
        "https://www.youtube.com/watch?v=abcdefghijk"
    )
    assert out_info["_strategy"] == "combined"
    assert out_info["_used_heatmap"] is True
    assert out_info["_used_audio"] is True
    assert 0.0 <= start < end <= 600.0
    assert 15.0 <= end - start <= 20.0
    assert abs(peak - (start + end) / 2.0) < 0.01


def test_select_top_windows_are_non_overlapping(monkeypatch, fake_ydl):
    info = fake_ydl
    info["heatmap"] = make_heatmap(peak_at=200.0)
    fetcher = make_fetcher()
    monkeypatch.setattr(
        fetcher, "_measure_audio_features",
        lambda stream_url, start, dur: synthesize_features(start),
    )

    windows = fetcher.select_top_windows(
        "https://www.youtube.com/watch?v=abcdefghijk", count=3
    )
    assert 1 <= len(windows) <= 3
    for i, first in enumerate(windows):
        for second in windows[i + 1:]:
            assert first["end"] - 2.0 <= second["start"] or second["end"] - 2.0 <= first["start"]
    # Scores are sorted best-first.
    scores = [w["score"] for w in windows]
    assert scores == sorted(scores, reverse=True)


def test_heatmap_strategy_ignores_audio(monkeypatch, fake_ydl):
    info = fake_ydl
    info["heatmap"] = make_heatmap(peak_at=200.0)
    fetcher = make_fetcher(strategy="heatmap")
    monkeypatch.setattr(
        fetcher, "_measure_audio_features",
        lambda stream_url, start, dur: synthesize_features(start),
    )

    windows, _h, used_audio = fetcher._build_ranked_windows(
        "https://www.youtube.com/watch?v=abcdefghijk", info, 600.0, count=1
    )
    assert not used_audio
    assert windows[0]["source"] == "heatmap"
    assert 150.0 <= (windows[0]["start"] + windows[0]["end"]) / 2.0 <= 250.0


def test_audio_strategy_does_not_need_heatmap(monkeypatch, fake_ydl):
    info = fake_ydl
    fetcher = make_fetcher(strategy="audio")
    monkeypatch.setattr(
        fetcher, "_measure_audio_features",
        lambda stream_url, start, dur: synthesize_features(start),
    )

    windows, _h, used_audio = fetcher._build_ranked_windows(
        "https://www.youtube.com/watch?v=abcdefghijk", info, 600.0, count=1
    )
    assert used_audio
    assert windows[0]["source"] == "audio"
    assert windows[0]["start"] < 60.0


class FakeYDLChannel:
    """Records the URL it is asked to extract (for channel-tab normalization)."""

    last_url = None
    entries = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        self.__class__.last_url = url
        return {"entries": self.entries}


def test_streams_tab_url_is_not_mangled(monkeypatch):
    """A source channel pointed at /streams (live VODs) must be scanned as-is."""
    FakeYDLChannel.last_url = None
    monkeypatch.setattr(fetcher_module.yt_dlp, "YoutubeDL", FakeYDLChannel)
    fetcher = make_fetcher()

    fetcher.fetch_channel_recent_videos("https://www.youtube.com/@SomeLiveChannel/streams")
    assert FakeYDLChannel.last_url == "https://www.youtube.com/@SomeLiveChannel/streams"

    fetcher.fetch_channel_recent_videos("https://www.youtube.com/@SomeChannel")
    assert FakeYDLChannel.last_url == "https://www.youtube.com/@SomeChannel/videos"


def test_live_stream_is_rejected_with_clear_error(monkeypatch, fake_ydl):
    """A still-airing stream must not be sliced as if it were a VOD."""
    info = fake_ydl
    info["is_live"] = True
    fetcher = make_fetcher()
    with pytest.raises(RuntimeError, match="STILL LIVE"):
        fetcher.extract_heatmap_and_select_window(
            "https://www.youtube.com/watch?v=abcdefghijk"
        )
