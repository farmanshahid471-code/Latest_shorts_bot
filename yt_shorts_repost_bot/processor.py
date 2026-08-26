"""
processor.py - Video editing, 9:16 center cropping, CPU transcription with faster-whisper,
timestamped SRT generation, and TikTok-style subtitle burning via FFmpeg.
"""
import os
import re
import sys
import subprocess
import datetime
import random
from pathlib import Path
from typing import Any, List, Optional, Tuple
from uuid import uuid4

from .config import (
    VERTICAL_WIDTH,
    VERTICAL_HEIGHT,
    SHORT_ASPECT,
    FILL_MODE,
    VIDEO_CRF,
    VIDEO_PRESET,
    AUDIO_BITRATE,
    WHISPER_MODEL_SIZE,
    WHISPER_LANGUAGE,
    WHISPER_DEVICE,
    WHISPER_COMPUTE_TYPE,
    MAX_WORDS_PER_SUBTITLE_LINE,
    SUBTITLE_MAX_DURATION_SEC,
    SUBTITLE_STYLE_MODE,
    VIRAL_WORDS_PER_LINE,
    SUBTITLE_UPPERCASE,
    SUBTITLE_FONT_NAME,
    SUBTITLE_FORCE_STYLE,
    BGM_DIR,
    BGM_ENABLED,
    BGM_VOLUME,
    VOICE_VOLUME,
    LOGO_REMOVE_ENABLED,
    LOGO_POSITION,
    LOGO_SIZE_PCT,
    LOGO_POSITIONS,
    LIKE_AND_SUBSCRIBE_ENABLED,
    LIKE_AND_SUBSCRIBE_TEXT,
    TOP_WATERMARK_ENABLED,
    TOP_WATERMARK_TEXT,
    TOP_WATERMARK_COLOR,
    TOP_WATERMARK_OPACITY,
    TOP_WATERMARK_FONT_SIZE,
    TOP_WATERMARK_ITALIC,
    TOP_WATERMARK_Y_PCT,
    BOTTOM_BANNER_FONT_SIZE,
    BOTTOM_BANNER_OPACITY,
    BOTTOM_BANNER_ITALIC,
    BOTTOM_BANNER_Y_PCT,
    FFMPEG_PATH,
    FFMPEG_TIMEOUT_SEC,
    TEMP_DIR,
    logger,
)


def _italic_font() -> Optional[str]:
    """
    Returns a fontfile path that renders ITALIC, or None if no italic font is
    installed. Uses fontfile (not font name) because drawtext has no fontstyle
    option and font-name-with-style doesn't work. Falls back to regular font.
    """
    import glob
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Oblique.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansOblique.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # generic search
    found = glob.glob("/usr/share/fonts/**/*Italic*.ttf", recursive=True) +             glob.glob("/usr/share/fonts/**/*Oblique*.ttf", recursive=True) +             glob.glob("/usr/share/fonts/**/*italic*.ttf", recursive=True)
    if found:
        return found[0]
    if sys.platform.startswith("win"):
        windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name in ("ariali.ttf", "calibrii.ttf", "segoeuii.ttf"):
            candidate = windows_fonts / name
            if candidate.is_file():
                return str(candidate).replace("\\", "/")
    return None


class VideoProcessor:
    """
    Processes video segments for YouTube Shorts:
    1. Transcribes audio locally on CPU for free using faster-whisper.
    2. Generates dynamic, high-readability timestamped SRT subtitles (TikTok/Shorts style).
    3. Uses FFmpeg to center-crop the video to 9:16 vertical aspect ratio (1080x1920)
       and burns subtitles directly into the video with a bold, readable font.
    """
    def __init__(self, model_size: str = WHISPER_MODEL_SIZE):
        self.model_size = model_size
        self._whisper_model: Optional[Any] = None
        self._model_size_used: Optional[str] = None
        self._ffmpeg_filters: Optional[set[str]] = None

    def _resolve_model_size(self) -> str:
        """
        English-only whisper models (*.en) cannot detect other languages and
        force English output - on French/Vietnamese/Urdu audio the burned
        subtitles become English gibberish (looks like dubbing). Subtitles must
        follow the SOURCE spoken language, so *.en models are always replaced
        by their multilingual equivalent (tiny.en -> tiny, base.en -> base,
        small.en -> small).
        """
        model = str(self.model_size or WHISPER_MODEL_SIZE).strip()
        if model.endswith(".en"):
            multilingual = model[: -len(".en")] or "tiny"
            logger.warning(
                "Whisper model '%s' is ENGLISH ONLY - using multilingual model "
                "'%s' instead so subtitles can follow the source audio language.",
                model,
                multilingual,
            )
            return multilingual
        return model

    def _get_whisper_model(self):
        """Lazy-load Faster-Whisper only when subtitles are requested."""
        if self._whisper_model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "Subtitles require faster-whisper. Install the project requirements "
                    "or disable subtitles for this account."
                ) from exc
            self._model_size_used = self._resolve_model_size()
            logger.info(
                f"Loading faster-whisper model '{self._model_size_used}' on "
                f"{WHISPER_DEVICE.upper()} ({WHISPER_COMPUTE_TYPE})..."
            )
            self._whisper_model = WhisperModel(
                self._model_size_used,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
            )
            logger.info("faster-whisper CPU model loaded successfully.")
        return self._whisper_model

    @staticmethod
    def _format_srt_timestamp(seconds: float) -> str:
        """Convert float seconds to SRT timestamp format: HH:MM:SS,mmm"""
        safe_seconds = max(0.0, float(seconds))
        td = datetime.timedelta(seconds=safe_seconds)
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        millis = int(round((safe_seconds - total_seconds) * 1000))
        if millis == 1000:
            total_seconds += 1
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            secs = total_seconds % 60
            millis = 0
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def transcribe_and_generate_srt(
        self,
        video_path: Path,
        srt_path: Optional[Path] = None,
        mode: str = SUBTITLE_STYLE_MODE
    ) -> Path:
        """
        Transcribes the video audio using faster-whisper on CPU and writes an SRT file.
        Supports:
          - mode='viral': 1-2 words per line, bold UPPERCASE (CapCut / Hormozi / TikTok style)
          - mode='standard': 3-4 words per line
        """
        if srt_path is None:
            srt_path = video_path.with_suffix(".srt")

        logger.info(f"Transcribing audio from {video_path.name} using CPU whisper (mode={mode})...")
        model = self._get_whisper_model()

        # ALWAYS detect the spoken language first. Subtitles must follow the
        # SOURCE language (the bot never dubs or translates); WHISPER_LANGUAGE
        # is only a fallback when detection fails or is very uncertain.
        segments, info = model.transcribe(
            str(video_path), word_timestamps=True, language=None
        )
        detected = str(getattr(info, "language", "") or "").lower()
        probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        configured = str(WHISPER_LANGUAGE or "auto").strip().lower()
        if not detected and configured not in ("", "auto", "detect"):
            logger.warning(
                "No language detected; retrying once with configured WHISPER_LANGUAGE='%s'.",
                configured,
            )
            segments, info = model.transcribe(
                str(video_path), word_timestamps=True, language=configured
            )
            detected = str(getattr(info, "language", "") or "").lower()
            probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        elif (
            detected
            and configured not in ("", "auto", "detect")
            and detected[:2] != configured[:2]
        ):
            logger.warning(
                "Audio is '%s' (%.2f) but WHISPER_LANGUAGE forces '%s' - IGNORING "
                "the forced language so subtitles match the source audio.",
                detected,
                probability,
                configured,
            )
        logger.info(
            "Spoken language detected: '%s' (%.2f confidence) - subtitles are "
            "generated in THAT language; the audio is never dubbed or replaced.",
            detected or "unknown",
            probability,
        )

        subtitle_entries: List[Tuple[float, float, str]] = []
        words_limit = VIRAL_WORDS_PER_LINE if mode == "viral" else MAX_WORDS_PER_SUBTITLE_LINE

        for segment in segments:
            words = getattr(segment, "words", None)
            if words and len(words) > 0:
                current_words = []
                chunk_start = words[0].start
                chunk_end = words[0].end

                for w in words:
                    raw_word = w.word.strip()
                    word_text = raw_word.upper() if (mode == "viral" or SUBTITLE_UPPERCASE) else raw_word
                    current_words.append(word_text)
                    chunk_end = w.end
                    if (
                        len(current_words) >= words_limit
                        or (chunk_end - chunk_start) >= SUBTITLE_MAX_DURATION_SEC
                    ):
                        text_line = " ".join(current_words).strip()
                        if text_line:
                            subtitle_entries.append((chunk_start, chunk_end, text_line))
                        current_words = []
                        chunk_start = w.end
                
                if current_words:
                    text_line = " ".join(current_words).strip()
                    if text_line:
                        subtitle_entries.append((chunk_start, chunk_end, text_line))
            else:
                text = segment.text.strip()
                if mode == "viral" or SUBTITLE_UPPERCASE:
                    text = text.upper()
                if text:
                    subtitle_entries.append((segment.start, segment.end, text))

        with open(srt_path, "w", encoding="utf-8") as f:
            for idx, (start_ts, end_ts, text) in enumerate(subtitle_entries, start=1):
                ts_start_str = self._format_srt_timestamp(start_ts)
                ts_end_str = self._format_srt_timestamp(end_ts)
                f.write(f"{idx}\n")
                f.write(f"{ts_start_str} --> {ts_end_str}\n")
                f.write(f"{text}\n\n")

        logger.info(f"Generated SRT subtitles at {srt_path} ({len(subtitle_entries)} lines)")
        return srt_path

    @staticmethod
    def _probe_video_size(path) -> Optional[Tuple[int, int]]:
        """Probe the source video's pixel dimensions (width, height) with ffprobe."""
        try:
            from .config import FFPROBE_PATH
            if not FFPROBE_PATH:
                return None
            r = subprocess.run(
                [FFPROBE_PATH, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=30)
            line = r.stdout.strip().splitlines()
            if not line:
                return None
            parts = line[0].split(",")
            if len(parts) != 2:
                return None
            return int(parts[0]), int(parts[1])
        except Exception:
            return None

    @staticmethod
    def _probe_has_audio(path: Path) -> bool:
        try:
            from .config import FFPROBE_PATH

            if FFPROBE_PATH:
                result = subprocess.run(
                    [
                        FFPROBE_PATH,
                        "-v",
                        "error",
                        "-select_streams",
                        "a:0",
                        "-show_entries",
                        "stream=index",
                        "-of",
                        "csv=p=0",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                return result.returncode == 0 and bool(result.stdout.strip())
            if not FFMPEG_PATH:
                return False
            # Fallback when ffprobe is unavailable: map only the first 0.1s of
            # audio. FFmpeg exits non-zero when no such stream exists.
            result = subprocess.run(
                [
                    FFMPEG_PATH, "-v", "error", "-i", str(path), "-t", "0.1",
                    "-map", "0:a:0", "-f", "null", "-",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _require_ffmpeg_filters(self, required: set[str]) -> None:
        if not FFMPEG_PATH:
            raise RuntimeError(
                "FFmpeg was not found. Run setup.bat/setup.sh or configure FFMPEG_PATH."
            )
        if self._ffmpeg_filters is None:
            result = subprocess.run(
                [FFMPEG_PATH, "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Could not inspect FFmpeg capabilities: {result.stderr.strip()}")
            names: set[str] = set()
            for line in result.stdout.splitlines():
                match = re.match(r"^\s*[.A-Z|]{3}\s+([A-Za-z0-9_]+)\s", line)
                if match:
                    names.add(match.group(1))
            self._ffmpeg_filters = names
        missing = sorted(required - self._ffmpeg_filters)
        if missing:
            raise RuntimeError(
                "This FFmpeg build is missing required filter(s): "
                + ", ".join(missing)
                + ". Install a full FFmpeg build with libfreetype/fontconfig/libass support."
            )

    @staticmethod
    def _escape_filter_path(path: Path | str) -> str:
        value = str(path).replace("\\", "/")
        for char in (":", "'", ",", ";", "[", "]"):
            value = value.replace(char, "\\" + char)
        return value

    @staticmethod
    def _resolve_logo_region(
        frame_x: int,
        frame_y: int,
        frame_w: int,
        frame_h: int,
        position: Optional[str],
        size_pct: float,
        enabled: bool,
    ) -> Optional[Tuple[int, int, int, int]]:
        """Resolve a logo patch inside the visible foreground, not the blur bars."""
        if position is None:
            if not enabled:
                return None
            pos = LOGO_POSITION
        else:
            pos = str(position).strip().lower()
        if pos in ("", "off", "none", "false", "disabled"):
            return None
        if pos not in LOGO_POSITIONS:
            logger.warning("Unknown logo position '%s'; logo blur disabled.", pos)
            return None
        width = min(frame_w, max(48, int(frame_w * float(size_pct) / 100.0)))
        height = min(frame_h, max(30, int(width * 0.62)))
        margin = max(6, int(frame_w * 0.015))
        left = frame_x + margin
        right = frame_x + frame_w - width - margin
        top = frame_y + margin
        bottom = frame_y + frame_h - height - margin
        x = left if pos.endswith("left") else right
        y = top if pos.startswith("top") else bottom
        return max(0, x), max(0, y), width, height

    def process_clip_to_short(
        self,
        input_path: Path,
        output_path: Optional[Path] = None,
        srt_path: Optional[Path] = None,
        bgm_path: Optional[Path] = None,
        aspect: Optional[str] = None,
        fill: Optional[str] = None,
        logo_position: Optional[str] = None,
        like_subscribe: Optional[bool] = None,
        like_subscribe_text: Optional[str] = None,
        top_watermark_enabled: Optional[bool] = None,
        top_watermark_text: Optional[str] = None,
        subtitles: Optional[bool] = None,
    ) -> Path:
        """Render a safe, validated vertical Short with optional captions/BGM/text."""
        input_path = Path(input_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"Input clip does not exist: {input_path}")

        source_size = self._probe_video_size(input_path)
        requested_aspect = str(aspect or SHORT_ASPECT).strip().lower()
        requested_fill = str(fill or FILL_MODE).strip().lower()
        if requested_fill not in {"crop", "blur"}:
            requested_fill = "blur"

        if requested_aspect in {"auto", "match", "source", "original"}:
            if source_size and source_size[0] > 0 and source_size[1] > source_size[0]:
                source_w, source_h = source_size
                width = 1080
                height = max(2, int(round((1080.0 * source_h / source_w) / 2.0) * 2))
                resolved_aspect = f"{source_w}:{source_h}"
                requested_fill = "crop"
                logger.info(
                    "Auto aspect: vertical source %sx%s -> %sx%s.",
                    source_w,
                    source_h,
                    width,
                    height,
                )
            else:
                # Landscape/square output is not reliably classified as a Short.
                width, height = 1080, 1920
                resolved_aspect = "9:16"
                logger.warning(
                    "Auto aspect received a landscape/square or unreadable source; "
                    "using a vertical 9:16 canvas instead."
                )
        elif requested_aspect == "3:4":
            width, height, resolved_aspect = 1080, 1440, "3:4"
        elif requested_aspect == "9:16":
            width, height, resolved_aspect = 1080, 1920, "9:16"
        else:
            width, height, resolved_aspect = VERTICAL_WIDTH, VERTICAL_HEIGHT, SHORT_ASPECT
            logger.warning("Unknown aspect '%s'; using %s.", requested_aspect, resolved_aspect)

        if output_path is None:
            output_path = TEMP_DIR / f"processed_{input_path.stem}_{uuid4().hex[:10]}.mp4"
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.resolve() == input_path.resolve():
            raise ValueError("Input and output video paths must be different")

        has_audio = self._probe_has_audio(input_path)
        subtitles_enabled = True if subtitles is None else bool(subtitles)
        if subtitles_enabled and has_audio:
            if srt_path is None or not Path(srt_path).is_file():
                srt_path = self.transcribe_and_generate_srt(input_path, srt_path=srt_path)
        elif subtitles_enabled and not has_audio:
            logger.warning("Source has no audio; transcription/subtitles were skipped.")
            subtitles_enabled = False

        srt_usable = bool(
            subtitles_enabled
            and srt_path
            and Path(srt_path).is_file()
            and Path(srt_path).stat().st_size > 0
        )

        selected_bgm: Optional[Path] = None
        if bgm_path and Path(bgm_path).is_file():
            selected_bgm = Path(bgm_path)
        elif BGM_ENABLED and BGM_DIR.exists():
            candidates = [
                track
                for track in BGM_DIR.iterdir()
                if track.is_file() and track.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac"}
            ]
            if candidates:
                selected_bgm = random.choice(candidates)
                logger.info("Selected BGM track: %s", selected_bgm.name)

        show_banner = LIKE_AND_SUBSCRIBE_ENABLED if like_subscribe is None else bool(like_subscribe)
        if like_subscribe_text is None:
            banner_text = str(LIKE_AND_SUBSCRIBE_TEXT or "").strip()
        else:
            banner_text = str(like_subscribe_text).strip()
        show_banner = bool(show_banner and banner_text)

        show_top = TOP_WATERMARK_ENABLED if top_watermark_enabled is None else bool(top_watermark_enabled)
        top_text = (
            str(TOP_WATERMARK_TEXT or "").strip()
            if top_watermark_text is None
            else str(top_watermark_text).strip()
        )
        show_top = bool(show_top and top_text)

        required_filters = {"drawtext"} if (show_banner or show_top) else set()
        if srt_usable:
            required_filters.add("subtitles")
        self._require_ffmpeg_filters(required_filters)

        # Build the fitted base video first. Logo removal is then applied to the
        # final canvas using coordinates calculated from the visible foreground.
        if requested_fill == "crop":
            ratio = f"{width}/{height}"
            base_chain = (
                f"[0:v]crop='if(gt(iw/ih,{ratio}),ih*({ratio}),iw)':"
                f"'if(gt(iw/ih,{ratio}),ih,iw/({ratio}))',"
                f"scale={width}:{height}[vbase]"
            )
            frame_x, frame_y, frame_w, frame_h = 0, 0, width, height
        else:
            base_chain = (
                f"[0:v]split=2[bg0][fg0];"
                f"[bg0]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=20:5[bg];"
                f"[fg0]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[vbase]"
            )
            if source_size:
                sw, sh = source_size
                scale = min(width / sw, height / sh)
                frame_w = max(2, int(round(sw * scale / 2.0) * 2))
                frame_h = max(2, int(round(sh * scale / 2.0) * 2))
                frame_x = max(0, (width - frame_w) // 2)
                frame_y = max(0, (height - frame_h) // 2)
            else:
                frame_x, frame_y, frame_w, frame_h = 0, 0, width, height

        logo = self._resolve_logo_region(
            frame_x,
            frame_y,
            frame_w,
            frame_h,
            logo_position,
            LOGO_SIZE_PCT,
            LOGO_REMOVE_ENABLED,
        )
        if logo:
            x, y, logo_w, logo_h = logo
            video_chain = (
                f"{base_chain};[vbase]split[logo_base][logo_crop];"
                f"[logo_crop]crop={logo_w}:{logo_h}:{x}:{y},boxblur=20:5[logo_patch];"
                f"[logo_base][logo_patch]overlay={x}:{y}[vfit]"
            )
            logger.info("Blurring logo region x=%s y=%s w=%s h=%s.", x, y, logo_w, logo_h)
        else:
            video_chain = f"{base_chain};[vbase]null[vfit]"

        temporary_filter_files: list[Path] = []
        stages: list[str] = [video_chain]
        current = "vfit"

        if srt_usable:
            # Copy to a controlled filename so arbitrary source paths never enter
            # FFmpeg's filter parser directly.
            import shutil

            safe_srt = TEMP_DIR / f"subtitle_{uuid4().hex}.srt"
            shutil.copy2(Path(srt_path), safe_srt)
            temporary_filter_files.append(safe_srt)
            escaped = self._escape_filter_path(safe_srt)
            stages.append(
                f"[{current}]subtitles=filename='{escaped}':force_style='{SUBTITLE_FORCE_STYLE}'[vsub]"
            )
            current = "vsub"

        def add_text_stage(text: str, label: str, font_size: int, opacity: float, y: int, italic: bool, color: str = "white") -> None:
            nonlocal current
            text_file = TEMP_DIR / f"overlay_{uuid4().hex}.txt"
            text_file.write_text(text, encoding="utf-8")
            temporary_filter_files.append(text_file)
            escaped_text_file = self._escape_filter_path(text_file)
            italic_file = _italic_font() if italic else None
            if italic_file:
                font_option = f"fontfile='{self._escape_filter_path(italic_file)}'"
            else:
                safe_font = str(SUBTITLE_FONT_NAME).replace("'", "").replace(":", "\\:")
                font_option = f"font='{safe_font}'"
            safe_color = re.sub(r"[^A-Za-z0-9#@._-]", "", str(color)) or "white"
            safe_opacity = min(1.0, max(0.0, float(opacity)))
            stages.append(
                f"[{current}]drawtext=textfile='{escaped_text_file}':{font_option}:"
                f"fontsize={max(8, int(font_size))}:fontcolor={safe_color}@{safe_opacity}:"
                f"x=(w-text_w)/2:y={max(0, int(y))}[{label}]"
            )
            current = label

        if show_banner:
            banner_y = int(height * BOTTOM_BANNER_Y_PCT / 100.0) - int(BOTTOM_BANNER_FONT_SIZE * 0.6)
            add_text_stage(
                banner_text,
                "vbanner",
                BOTTOM_BANNER_FONT_SIZE,
                BOTTOM_BANNER_OPACITY,
                banner_y,
                BOTTOM_BANNER_ITALIC,
            )
            logger.info("Adding bottom account watermark.")
        if show_top:
            top_y = int(height * TOP_WATERMARK_Y_PCT / 100.0) - int(TOP_WATERMARK_FONT_SIZE * 0.6)
            add_text_stage(
                top_text,
                "vtop",
                TOP_WATERMARK_FONT_SIZE,
                TOP_WATERMARK_OPACITY,
                top_y,
                TOP_WATERMARK_ITALIC,
                TOP_WATERMARK_COLOR,
            )
            logger.info("Adding top account watermark.")

        stages.append(f"[{current}]null[vout]")
        audio_label: Optional[str] = None
        if selected_bgm:
            if has_audio:
                stages.extend(
                    [
                        f"[0:a]volume={VOICE_VOLUME}[voice]",
                        f"[1:a]volume={BGM_VOLUME}[music]",
                        "[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                    ]
                )
            else:
                stages.append(f"[1:a]volume={BGM_VOLUME}[aout]")
            audio_label = "[aout]"

        cmd = [FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error", "-i", str(input_path)]
        if selected_bgm:
            cmd += ["-stream_loop", "-1", "-i", str(selected_bgm)]
        cmd += ["-filter_complex", ";".join(stages), "-map", "[vout]"]
        if audio_label:
            cmd += ["-map", audio_label]
        elif has_audio:
            cmd += ["-map", "0:a?"]
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            VIDEO_PRESET,
            "-crf",
            str(VIDEO_CRF),
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
        ]
        if audio_label or has_audio:
            cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
        cmd += ["-movflags", "+faststart", "-threads", "0", "-shortest", str(output_path)]

        logger.info(
            "Rendering %sx%s (%s, fill=%s) -> %s",
            width,
            height,
            resolved_aspect,
            requested_fill,
            output_path.name,
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=FFMPEG_TIMEOUT_SEC,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown FFmpeg error").strip()
                raise RuntimeError(f"FFmpeg render failed: {detail[-4000:]}")
            if not output_path.is_file() or output_path.stat().st_size <= 0:
                raise RuntimeError("FFmpeg reported success but produced no output file")
            logger.info(
                "Rendered Short: %s (%.2f MB)",
                output_path,
                output_path.stat().st_size / 1024 / 1024,
            )
            return output_path
        except subprocess.TimeoutExpired as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"FFmpeg render timed out after {FFMPEG_TIMEOUT_SEC} seconds"
            ) from exc
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
        finally:
            for temporary in temporary_filter_files:
                temporary.unlink(missing_ok=True)
