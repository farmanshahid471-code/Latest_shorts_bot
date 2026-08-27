"""The clip bot must always use the ORIGINAL-language audio track, never a dub."""
from __future__ import annotations


import yt_shorts_bot.fetcher as fetcher_module
from yt_shorts_bot.fetcher import YouTubeFetcher


def _fmt(fmt_id, lang, pref, abr, height=None, vcodec="none"):
    f = {
        "format_id": fmt_id,
        "url": f"https://stream.test/{fmt_id}",
        "language": lang,
        "language_preference": pref,
        "abr": abr,
        "tbr": abr,
        "ext": "m4a",
        "acodec": "mp4a.40.2",
        "vcodec": vcodec,
    }
    if height is not None:
        f["height"] = height
    return f


def test_audio_track_key_prefers_original_over_higher_bitrate_dub():
    # French dub is 256k but NOT original; English original is only 128k.
    original = _fmt("sb0", "en", 10, 128)
    dub = _fmt("sb1", "fr", -1, 256)
    best = max([dub, original], key=YouTubeFetcher._audio_track_key)
    assert best["format_id"] == "sb0"


def test_default_track_beats_plain_track_but_loses_to_original():
    original = _fmt("sb0", "en", 10, 64)
    default = _fmt("sb1", "fr", 5, 256)
    plain = _fmt("sb2", "es", -1, 300)
    best = max([plain, default, original], key=YouTubeFetcher._audio_track_key)
    assert best["format_id"] == "sb0"
    best = max([plain, default], key=YouTubeFetcher._audio_track_key)
    assert best["format_id"] == "sb1"


def test_ydl_opts_sort_original_language_first():
    opts = YouTubeFetcher._original_audio_opt()
    assert opts["format_sort"][0] == "lang"


def test_resolve_direct_audio_url_picks_original_track():
    fetcher = YouTubeFetcher()
    info = {
        "formats": [
            _fmt("sb0", "en", 10, 128),
            _fmt("sb1", "fr", -1, 256),
            _fmt("sb2", "es", -1, 320),
        ]
    }
    url = fetcher._resolve_direct_audio_url(info, "https://youtu.be/abc")
    assert url == "https://stream.test/sb0"


class FakeYDL:
    seen_opts: dict = {}

    def __init__(self, opts):
        self.__class__.seen_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        return {
            "formats": [
                _fmt("sb0", "en", 10, 128),
                _fmt("sb1", "fr", -1, 256),
            ],
            "url": "https://stream.test/sb0",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
        }


def test_metadata_extraction_uses_language_first_sort(monkeypatch):
    monkeypatch.setattr(fetcher_module.yt_dlp, "YoutubeDL", FakeYDL)
    fetcher = YouTubeFetcher()
    info = fetcher._get_info("https://youtu.be/abc")
    assert FakeYDL.seen_opts["format_sort"][0] == "lang"
    # The selected stream is the original-language one.
    assert info["url"] == "https://stream.test/sb0"
