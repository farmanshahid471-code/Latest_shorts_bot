"""Chinese (or other language) dubbing for clips headed to Bilibili.

The pipeline already transcribes every clip with faster-whisper to build
subtitles, so the timed source text is free. Dubbing reuses it:

    SRT  ->  translate each cue  ->  TTS per cue  ->  place at the cue's
             timestamp  ->  mux over the original audio

Each cue is synthesised separately and laid down at its own start time, so the
dub stays in sync with the picture even when the translation is much longer or
shorter than the original line. Cues that would overrun the next one are gently
sped up (never beyond ``MAX_CUE_SPEEDUP``) rather than allowed to overlap.

Both the translator and the TTS engine are pluggable: the defaults need no API
key (``deep-translator``'s Google endpoint and Microsoft ``edge-tts``), but a
caller can inject its own callables, which is also how the tests avoid the
network entirely.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

# Voices that exist on Microsoft's free endpoint. Xiaoxiao is the safe default:
# warm, natural, and the one most Bilibili viewers are used to hearing.
CHINESE_VOICES: dict[str, str] = {
    "zh-CN-XiaoxiaoNeural": "晓晓 — female, warm (default)",
    "zh-CN-YunxiNeural": "云希 — male, lively",
    "zh-CN-YunjianNeural": "云健 — male, deep//sports",
    "zh-CN-XiaoyiNeural": "晓伊 — female, youthful",
    "zh-CN-YunyangNeural": "云扬 — male, news anchor",
    "zh-CN-liaoning-XiaobeiNeural": "晓北 — female, northeastern accent",
    "zh-TW-HsiaoChenNeural": "曉臻 — female, Taiwanese Mandarin",
    "zh-HK-HiuMaanNeural": "曉曼 — female, Cantonese",
}
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# A cue may be compressed up to this factor to fit its slot; past that the
# speech turns into chipmunk noise, so we let it run long instead.
MAX_CUE_SPEEDUP = 1.6
# How loud the original audio stays underneath the dub (0 = fully muted).
DEFAULT_ORIGINAL_VOLUME = 0.12

_TIMESTAMP = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


class DubbingError(RuntimeError):
    """Raised when a dub cannot be produced."""


@dataclass
class Cue:
    """One subtitle line: when it is spoken and what is said."""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# ---------------------------------------------------------------------------
# SRT parsing
# ---------------------------------------------------------------------------
def _to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    # SRT allows 1-3 digit milliseconds; "5" means 500ms, not 5ms.
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")) / 1000.0
    )


def parse_srt(srt_path: Path) -> list[Cue]:
    """Read an SRT into cues, merging the word-level chunks the bot writes.

    The viral subtitle style emits 1-2 words per cue, which would produce
    choppy, unnatural speech. Adjacent cues are therefore merged into sentence
    -like groups before translation.
    """
    raw = Path(srt_path).read_text(encoding="utf-8", errors="replace")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        match = _TIMESTAMP.search(block)
        if not match:
            continue
        start = _to_seconds(*match.group(1, 2, 3, 4))
        end = _to_seconds(*match.group(5, 6, 7, 8))
        # Text is everything after the timestamp line.
        text = block[match.end():].strip()
        text = re.sub(r"<[^>]+>", "", text)          # strip styling tags
        text = " ".join(text.split())
        if text and end > start:
            cues.append(Cue(start, end, text))
    return cues


def merge_cues(
    cues: Sequence[Cue],
    max_gap: float = 0.6,
    max_duration: float = 8.0,
) -> list[Cue]:
    """Join neighbouring cues into natural sentences for smoother speech."""
    merged: list[Cue] = []
    for cue in cues:
        if not merged:
            merged.append(Cue(cue.start, cue.end, cue.text))
            continue
        previous = merged[-1]
        ends_sentence = previous.text.rstrip().endswith((".", "!", "?", "…", "。", "！", "？"))
        if (
            not ends_sentence
            and cue.start - previous.end <= max_gap
            and cue.end - previous.start <= max_duration
        ):
            previous.end = cue.end
            previous.text = f"{previous.text} {cue.text}".strip()
        else:
            merged.append(Cue(cue.start, cue.end, cue.text))
    return merged


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------
def translate_texts(
    texts: Sequence[str],
    target: str = "zh-CN",
    source: str = "auto",
) -> list[str]:
    """Translate with deep-translator (free Google endpoint, no API key)."""
    if not texts:
        return []
    try:
        from deep_translator import GoogleTranslator
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DubbingError(
            "Translation needs the 'deep-translator' package. Install the "
            "project requirements, or turn dubbing off for this account."
        ) from exc

    # deep-translator speaks 'zh-CN'; normalise the common aliases.
    normalised = {"zh": "zh-CN", "zh-hans": "zh-CN", "zh-cn": "zh-CN",
                  "zh-hant": "zh-TW", "zh-tw": "zh-TW"}.get(target.lower(), target)
    translator = GoogleTranslator(source=source, target=normalised)
    out: list[str] = []
    for text in texts:
        try:
            result = translator.translate(text)
        except Exception as exc:
            # One bad line must not sink the whole dub.
            logger.warning("Translation failed for %r (%s); keeping original.", text, exc)
            result = text
        out.append(str(result or text).strip() or text)
    return out


# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------
def synthesize_edge_tts(
    text: str,
    out_path: Path,
    voice: str = DEFAULT_VOICE,
    rate: str = "+0%",
) -> Path:
    """Speak ``text`` into ``out_path`` using Microsoft edge-tts (free)."""
    try:
        import edge_tts
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DubbingError(
            "Dubbing needs the 'edge-tts' package. Install the project "
            "requirements, or turn dubbing off for this account."
        ) from exc

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(out_path))

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise DubbingError(f"Text-to-speech failed: {exc}") from exc
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise DubbingError("Text-to-speech produced an empty audio file.")
    return out_path


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def _ffmpeg() -> str:
    try:
        from .config import FFMPEG_PATH
    except ImportError:  # pragma: no cover - standalone use
        FFMPEG_PATH = None
    path = FFMPEG_PATH or shutil.which("ffmpeg")
    if not path:
        raise DubbingError("FFmpeg is required for dubbing but was not found.")
    return str(path)


def _run_ffmpeg(args: list[str], timeout: int = 900) -> None:
    command = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *args]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise DubbingError(f"FFmpeg timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise DubbingError(
            f"FFmpeg failed ({result.returncode}): {(result.stderr or '').strip()[:400]}"
        )


def audio_duration(path: Path) -> float:
    """Length of an audio file in seconds (0.0 when it cannot be probed)."""
    try:
        from .config import FFPROBE_PATH
    except ImportError:  # pragma: no cover
        FFPROBE_PATH = None
    probe = FFPROBE_PATH or shutil.which("ffprobe")
    if not probe:
        return 0.0
    try:
        result = subprocess.run(
            [str(probe), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        return float((result.stdout or "0").strip() or 0.0)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


def _atempo_chain(speed: float) -> str:
    """ffmpeg's atempo only accepts 0.5-2.0, so chain it for larger factors."""
    speed = max(0.5, min(speed, 4.0))
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.6f}")
    return ",".join(filters)


# ---------------------------------------------------------------------------
# The dub itself
# ---------------------------------------------------------------------------
def build_dub_track(
    cues: Sequence[Cue],
    out_path: Path,
    total_duration: float,
    *,
    voice: str = DEFAULT_VOICE,
    tts: Optional[Callable[[str, Path, str], Path]] = None,
    workdir: Optional[Path] = None,
) -> Path:
    """Synthesise every cue and lay them out on one silent timeline."""
    if not cues:
        raise DubbingError("Nothing to dub: the transcript had no usable lines.")

    speak = tts or (lambda text, path, v: synthesize_edge_tts(text, path, v))
    temp_root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="dub_"))
    temp_root.mkdir(parents=True, exist_ok=True)

    pieces: list[tuple[float, Path]] = []
    for index, cue in enumerate(cues):
        piece = temp_root / f"cue_{index:04d}.mp3"
        try:
            speak(cue.text, piece, voice)
        except DubbingError:
            raise
        except Exception as exc:
            logger.warning("Skipping cue %d (%s): %s", index, cue.text[:40], exc)
            continue
        if not piece.is_file() or piece.stat().st_size == 0:
            continue

        # Fit the clip into its slot when it overruns, but never past the point
        # where the voice stops sounding human.
        spoken = audio_duration(piece)
        slot = cue.duration
        if spoken > 0 and slot > 0 and spoken > slot:
            speed = min(spoken / slot, MAX_CUE_SPEEDUP)
            if speed > 1.01:
                fitted = temp_root / f"cue_{index:04d}_fit.mp3"
                _run_ffmpeg(["-i", str(piece), "-filter:a", _atempo_chain(speed),
                             "-c:a", "libmp3lame", str(fitted)])
                piece = fitted
        pieces.append((cue.start, piece))

    if not pieces:
        raise DubbingError("Text-to-speech produced no usable audio.")

    # One silent bed, with every cue delayed to its own start time, mixed down.
    duration = max(total_duration, max(start for start, _ in pieces) + 1.0)
    inputs: list[str] = ["-f", "lavfi", "-t", f"{duration:.3f}",
                         "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    filters: list[str] = []
    labels: list[str] = ["[0:a]"]
    for index, (start, piece) in enumerate(pieces, start=1):
        inputs += ["-i", str(piece)]
        filters.append(
            f"[{index}:a]aresample=44100,adelay={int(start * 1000)}|{int(start * 1000)}[d{index}]"
        )
        labels.append(f"[d{index}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:dropout_transition=0[out]"
    )
    _run_ffmpeg([*inputs, "-filter_complex", ";".join(filters), "-map", "[out]",
                 "-t", f"{duration:.3f}", "-c:a", "libmp3lame", "-b:a", "192k",
                 str(out_path)])
    return Path(out_path)


def mux_dub_onto_video(
    video_path: Path,
    dub_path: Path,
    out_path: Path,
    *,
    original_volume: float = DEFAULT_ORIGINAL_VOLUME,
    has_original_audio: bool = True,
) -> Path:
    """Replace the video's audio with the dub (optionally keeping a bed).

    Video is stream-copied, so this is fast and lossless for the picture.
    """
    if original_volume > 0 and has_original_audio:
        # Duck the original under the dub so ambience/music survives.
        filters = (
            f"[0:a]volume={original_volume:.3f}[bed];"
            f"[1:a]volume=1.0[dub];"
            f"[bed][dub]amix=inputs=2:normalize=0:dropout_transition=0[out]"
        )
        args = ["-i", str(video_path), "-i", str(dub_path),
                "-filter_complex", filters, "-map", "0:v:0", "-map", "[out]"]
    else:
        args = ["-i", str(video_path), "-i", str(dub_path),
                "-map", "0:v:0", "-map", "1:a:0"]
    _run_ffmpeg([*args, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-shortest", str(out_path)])
    return Path(out_path)


def dub_video(
    video_path: Path,
    srt_path: Path,
    out_path: Path,
    *,
    voice: str = DEFAULT_VOICE,
    target_language: str = "zh-CN",
    original_volume: float = DEFAULT_ORIGINAL_VOLUME,
    video_duration: float = 0.0,
    translator: Optional[Callable[[Sequence[str], str], Iterable[str]]] = None,
    tts: Optional[Callable[[str, Path, str], Path]] = None,
    workdir: Optional[Path] = None,
    has_original_audio: bool = True,
) -> Path:
    """Full pipeline: SRT -> translated cues -> TTS -> dubbed video file."""
    video_path, srt_path, out_path = Path(video_path), Path(srt_path), Path(out_path)
    if not video_path.is_file():
        raise DubbingError(f"Video to dub is missing: {video_path}")
    if not srt_path.is_file():
        raise DubbingError(
            "Dubbing needs a transcript. Enable subtitles for this account so "
            "the clip gets transcribed."
        )

    cues = merge_cues(parse_srt(srt_path))
    if not cues:
        raise DubbingError("The transcript had no usable lines to dub.")

    translate = translator or (lambda texts, target: translate_texts(texts, target))
    translated = list(translate([cue.text for cue in cues], target_language))
    if len(translated) != len(cues):
        raise DubbingError("Translator returned a different number of lines.")
    for cue, text in zip(cues, translated):
        cue.text = str(text or "").strip() or cue.text

    temp_root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="dub_"))
    temp_root.mkdir(parents=True, exist_ok=True)
    dub_track = temp_root / "dub_track.mp3"
    build_dub_track(
        cues, dub_track,
        total_duration=video_duration or (cues[-1].end + 1.0),
        voice=voice, tts=tts, workdir=temp_root,
    )
    return mux_dub_onto_video(
        video_path, dub_track, out_path,
        original_volume=original_volume,
        has_original_audio=has_original_audio,
    )
