"""Tests for FFmpeg filter capability parsing (FFmpeg 7.x and 8.x formats)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import yt_shorts_bot.processor as processor_module
from yt_shorts_bot.processor import VideoProcessor

# FFmpeg 8.x / master: 2 flag chars (T/S) + media descriptor (V->V etc.).
FFMPEG8_OUTPUT = """Filters:
 T. = Timeline support
 .S. = Slice threading
 A = Audio input/output
 V = Video input/output
 N = Dynamic number and/or type of input/output
 | = Source or sink filter
 ------
 T. split             V->V       Pass on the input to two outputs.
 T. scale             V->V       Scale the input video size and/or convert the image format.
 T. crop              V->V       Crop the input video to a smaller output video.
 T. drawtext          V->V       Draw text on top of video frames.
 .. subtitles         V->V       Render text subtitles as a separate video stream.
 .. overlay           V->V       Overlay a video source on top of another.
 .. boxblur           V->V       Blur the input video.
 T. null              V->V       Pass the source unchanged to the output.
 T. volume           A->A       Change input volume.
 T. amix             A->A       Audio mixing.
"""

# FFmpeg 7.x and older: 3 flag chars (T/S/C).
FFMPEG7_OUTPUT = """Filters:
 T.. = Timeline support
 .S. = Slice threading
 ..C = Command support
 A = Audio input/output
 V = Video input/output
 N = Dynamic number and/or type of input/output
 | = Source or sink filter
 ------
 TSC split             V->V       Pass on the input to two outputs.
 TSC scale             V->V       Scale the input video size and/or convert the image format.
 TSC crop              V->V       Crop the input video to a smaller output video.
 T.C drawtext          V->V       Draw text on top of video frames.
 ..C subtitles         V->V       Render text subtitles as a separate video stream.
 ..C overlay           V->V       Overlay a video source on top of another.
 ..C boxblur           V->V       Blur the input video.
 TSC null              V->V       Pass the source unchanged to the output.
 TSC volume           A->A       Change input volume.
 TSC amix             A->A       Audio mixing.
"""


def test_parse_filter_names_ffmpeg8_two_char_flags():
    names = VideoProcessor._parse_filter_names(FFMPEG8_OUTPUT)
    assert {"drawtext", "subtitles", "scale", "crop", "overlay", "boxblur", "volume", "amix", "split", "null"} <= names
    # Header noise must never leak into the set.
    assert "=" not in names and "Timeline" not in names and "Audio" not in names


def test_parse_filter_names_ffmpeg7_three_char_flags():
    names = VideoProcessor._parse_filter_names(FFMPEG7_OUTPUT)
    assert {"drawtext", "subtitles", "scale", "crop", "overlay", "boxblur", "volume", "amix"} <= names


def test_require_ffmpeg_filters_accepts_ffmpeg8_format(monkeypatch):
    """The exact user error: new FFmpeg rejects nothing - the parser must read it."""
    video_processor = VideoProcessor()

    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=FFMPEG8_OUTPUT, stderr="")

    monkeypatch.setattr(processor_module, "FFMPEG_PATH", "C:/ffmpeg/bin/ffmpeg.exe")
    monkeypatch.setattr(processor_module.subprocess, "run", fake_run)

    # Must NOT raise - this is what failed for the user.
    video_processor._require_ffmpeg_filters({"drawtext", "subtitles"})


def test_require_ffmpeg_filters_accepts_ffmpeg7_format(monkeypatch):
    video_processor = VideoProcessor()

    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=FFMPEG7_OUTPUT, stderr="")

    monkeypatch.setattr(processor_module, "FFMPEG_PATH", "ffmpeg")
    monkeypatch.setattr(processor_module.subprocess, "run", fake_run)
    video_processor._require_ffmpeg_filters({"drawtext", "subtitles"})


def test_require_ffmpeg_filters_missing_reports_path_in_error(monkeypatch):
    """A genuinely minimal build must produce a helpful error naming the binary."""
    video_processor = VideoProcessor()
    minimal = """Filters:
 T. scale             V->V       Scale.
 T. null              V->V       Pass through.
"""

    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=minimal, stderr="")

    monkeypatch.setattr(processor_module, "FFMPEG_PATH", "C:/broken/ffmpeg.exe")
    monkeypatch.setattr(processor_module.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc_info:
        video_processor._require_ffmpeg_filters({"drawtext", "subtitles"})
    message = str(exc_info.value)
    assert "drawtext" in message and "subtitles" in message
    assert "C:/broken/ffmpeg.exe" in message
