"""
fetcher.py - Target selection, newest-to-oldest channel listing, combined
"most watched + high-pitched/high-energy voice" moment analysis, and
section-only downloading using yt-dlp and FFmpeg.
"""
import math
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from uuid import uuid4

import yt_dlp
import numpy as np

from .config import (
    TARGET_CHANNELS,
    FETCH_LIMIT_PER_CHANNEL,
    CLIP_DURATION_SEC,
    MIN_CLIP_DURATION_SEC,
    MAX_CLIP_DURATION_SEC,
    SELECTION_STRATEGY,
    FETCH_SCAN_LIMIT,
    YTDL_SOCKET_TIMEOUT_SEC,
    HEATMAP_WEIGHT,
    AUDIO_EXCITEMENT_WEIGHT,
    AUDIO_ENERGY_WEIGHT,
    AUDIO_PITCH_WEIGHT,
    AUDIO_FLUX_WEIGHT,
    AUDIO_SAMPLE_SEC,
    MAX_AUDIO_SAMPLES,
    FFMPEG_PATH,
    FFPROBE_PATH,
    YT_COOKIES_FILE,
    YT_COOKIES_FROM_BROWSER,
    BASE_DIR,
    TEMP_DIR,
    logger,
)


class YouTubeFetcher:
    """
    Fetches video metadata from target channels, extracts 'Most Replayed' (heatmap) data
    without downloading full videos, calculates the optimal 15-20s engagement window,
    and downloads ONLY that segment.

    Moment selection supports three strategies:
      - "combined": blends YouTube Most Replayed with audio excitement (loudness +
        high-pitched voice + sudden bursts). Uses whichever signal exists, and
        falls back to the other when one is missing.
      - "heatmap": Most Replayed only (classic behaviour).
      - "audio": audio excitement only.
    """
    def __init__(
        self,
        channels: Optional[List[str]] = None,
        fetch_limit: int = FETCH_LIMIT_PER_CHANNEL,
        strategy: Optional[str] = None,
        heatmap_weight: Optional[float] = None,
        audio_weight: Optional[float] = None,
    ):
        self.channels = channels if channels is not None else TARGET_CHANNELS
        self.fetch_limit = fetch_limit
        self.strategy = str(strategy or SELECTION_STRATEGY).strip().lower()
        if self.strategy not in ("combined", "heatmap", "audio"):
            self.strategy = "combined"

        def _weight(value: Optional[float], default: float) -> float:
            try:
                if value is None or str(value).strip() == "":
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        self.heatmap_weight = min(1.0, max(0.0, _weight(heatmap_weight, HEATMAP_WEIGHT)))
        self.audio_weight = min(1.0, max(0.0, _weight(audio_weight, AUDIO_EXCITEMENT_WEIGHT)))
        # Small caches so the scheduler's per-video metadata + audio probes are
        # not fetched twice when making multiple Shorts from one source.
        self._info_cache: Dict[str, Dict[str, Any]] = {}
        self._profile_cache: Dict[str, List[Dict[str, Any]]] = {}

    @staticmethod
    def _ffmpeg_opt() -> dict:
        """
        Tells yt-dlp where our FFmpeg lives so format merging (video+audio)
        works even when the bundled ffmpeg is not on the system PATH.
        """
        if not FFMPEG_PATH:
            return {}
        return {"ffmpeg_location": str(Path(FFMPEG_PATH).resolve().parent)}

    @staticmethod
    def _timeout_opt() -> dict:
        """
        Bound every yt-dlp network call so a stalled YouTube connection cannot
        hang a cycle with no log output (looks like the bot is frozen).
        """
        return {
            "socket_timeout": YTDL_SOCKET_TIMEOUT_SEC,
            "retries": 2,
            "extractor_retries": 2,
        }

    @staticmethod
    def _original_audio_opt() -> dict:
        """
        Always prefer the ORIGINAL-language audio track of multi-language
        videos. YouTube exposes official dubs as separate tracks and yt-dlp's
        sort puts quality/bitrate BEFORE language, so a higher-bitrate dub can
        win over the original - that is how French/Vietnamese 'dubbing' got
        into clips. 'lang' sorts by language_preference: original=10,
        default=5, other tracks=-1 and single-track videos fall through to
        the regular quality sort.
        """
        return {
            "format_sort": ["lang", "quality", "tbr", "abr", "vbr", "ext", "proto"],
        }

    @staticmethod
    def _audio_track_key(fmt: dict) -> tuple:
        """Sort key: original language track first, then quality."""
        return (
            int(fmt.get("language_preference") or -1),
            float(fmt.get("abr") or 0.0),
            float(fmt.get("tbr") or 0.0),
        )

    @staticmethod
    def _log_audio_tracks(info: Dict[str, Any]) -> None:
        """One-time visibility: which audio languages does the source offer?"""
        tracks = [
            (
                str(f.get("language") or "?"),
                int(f.get("language_preference") or -1),
                float(f.get("abr") or 0.0),
                str(f.get("format_id") or ""),
            )
            for f in info.get("formats", [])
            if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
        ]
        if not tracks:
            return
        summary = ", ".join(
            f"{lang} ({'original' if pref >= 10 else 'default' if pref >= 5 else 'track'}, {abr:.0f}k, {fid})"
            for lang, pref, abr, fid in sorted(tracks, key=lambda t: (-t[1], -t[2]))
        )
        logger.info("Source audio tracks: %s - the ORIGINAL track is preferred.", summary)

    @staticmethod
    def _cookies_opts() -> dict:
        """
        Returns yt-dlp options for YouTube authentication cookies.
        Fixes: "Sign in to confirm you're not a bot".
        """
        opts = {}

        if YT_COOKIES_FILE:
            cf_path = Path(YT_COOKIES_FILE)
            if not cf_path.is_absolute():
                cf_path = BASE_DIR / cf_path
            if cf_path.exists():
                opts["cookiefile"] = str(cf_path)
                logger.info(f"Using YouTube cookies from file: {cf_path}")
                if not YouTubeFetcher._cookies_look_valid(cf_path):
                    logger.warning(
                        "cookies.txt exists but does NOT contain YouTube login cookies "
                        "(looks empty or expired). Re-export it while LOGGED IN on "
                        "youtube.com - otherwise YouTube can still show the bot check."
                    )
            else:
                logger.warning(
                    f"YT_COOKIES_FILE is set to '{YT_COOKIES_FILE}' but that file was not found. "
                    "Skipping cookies - YouTube may ask you to sign in."
                )

        browser = YT_COOKIES_FROM_BROWSER.strip().lower()
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
            logger.info(f"Using YouTube cookies from browser: {browser}")

        return opts

    @staticmethod
    def _cookies_look_valid(cf_path: Path) -> bool:
        """
        Cheap sanity check: a valid exported YouTube cookie file (Netscape format)
        contains lines for .youtube.com with auth cookies like SID/SSID/APISID/HSID
        or __Secure-1PSID. Returns False if the file looks empty/expired.
        """
        try:
            text = cf_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        if "# Netscape HTTP Cookie File" not in text and ".youtube.com" not in text:
            return False
        auth_markers = ("SID", "APISID", "HSID", "SSID", "SAPISID")
        has_auth = any(
            marker in line
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
            and ".youtube.com" in line
            for marker in auth_markers
        )
        return has_auth

    def fetch_channel_recent_videos(
        self,
        channel_url: str,
        order: str = "newest",
    ) -> List[Dict[str, Any]]:
        """
        Lists videos from a YouTube channel URL without downloading, in the
        requested order:

          - "newest": most recent first
          - "oldest": oldest in the scanned window first
          - "random": shuffled sample of the scanned window

        The scan is deep (FETCH_SCAN_LIMIT, flat metadata only) so the caller
        can skip videos that are already uploaded and still find the next real
        candidate in the chosen order. Reversing/shuffling only the newest N
        never works: YouTube's tabs no longer support server-side sorting, so
        the list always arrives newest-first.

        Returns a list of dicts with video_id, url, title, duration.
        """
        order = str(order or "newest").strip().lower()
        if order not in ("newest", "oldest", "random"):
            order = "newest"
        logger.info(
            "Scanning channel for recent videos (%s, %s order): %s",
            channel_url,
            order,
            channel_url,
        )
        # Normalize channel URL to its videos tab if needed. If the user
        # explicitly points at the channel's Live tab (/streams) or Shorts tab
        # (/shorts), keep it - useful for channels whose content is live VODs.
        url = channel_url.rstrip("/")
        if "@" in url and not url.endswith(("/videos", "/streams", "/shorts")):
            url = f"{url}/videos"

        # Scan deep enough that the caller can skip already-uploaded videos and
        # still find the next candidate in the chosen order. Flat metadata only -
        # never downloads videos. FETCH_LIMIT_PER_CHANNEL remains the minimum
        # (back-compat: it used to be the only window).
        window = max(self.fetch_limit, FETCH_SCAN_LIMIT)
        ydl_opts = {
            "extract_flat": "in_playlist",
            "playlistend": window,
            "quiet": True,
            "no_warnings": True,
            **self._cookies_opts(),
            **self._ffmpeg_opt(),
            **self._timeout_opt(),
            **self._original_audio_opt(),
        }

        videos = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(url, download=False)
                if not res:
                    logger.warning(f"No response from yt-dlp for channel: {channel_url}")
                    return []
                entries = res.get("entries", [])
                for entry in entries:
                    if not entry:
                        continue
                    v_id = entry.get("id")
                    if not v_id:
                        continue
                    v_url = entry.get("webpage_url") or entry.get("url") or ""
                    if not str(v_url).startswith(("http://", "https://")):
                        v_url = f"https://www.youtube.com/watch?v={v_id}"
                    duration = entry.get("duration", 0) or 0
                    # Ignore existing YouTube Shorts (< 60s) or extremely short videos (< 45s)
                    if 0 < duration < 60:
                        logger.debug(f"Skipping video '{entry.get('title')}' - already a short ({duration}s)")
                        continue

                    videos.append({
                        "video_id": v_id,
                        "url": v_url,
                        "title": entry.get("title", f"Video {v_id}"),
                        "duration": duration,
                        "channel": channel_url,
                    })
        except Exception as e:
            logger.error(f"Error listing channel {channel_url}: {e}")

        # The /videos tab always arrives newest-first. Apply the order HERE (not
        # in the scheduler) so every caller gets the same guaranteed ordering.
        if order == "oldest":
            videos.reverse()
            logger.info(
                "Selected oldest-first from %d candidate videos (scanned %s).",
                len(videos),
                window,
            )
        elif order == "random":
            import random as _random
            _random.shuffle(videos)
            logger.info("Shuffled %d candidate videos (random order).", len(videos))
        else:
            logger.info("Selected %d newest candidate videos.", len(videos))
        return videos

    @staticmethod
    def _is_bot_check_error(error: Exception) -> bool:
        """True if yt-dlp hit YouTube's 'Sign in to confirm you're not a bot' wall."""
        text = str(error)
        return "Sign in to confirm" in text or "not a bot" in text or "cookies" in text.lower() and "bot" in text.lower()

    @staticmethod
    def _ensure_not_live(info: Dict[str, Any]) -> None:
        """
        Reject still-airing streams with a clear message. `-ss` seeking cannot
        work on a live edge; the stream must END first so it becomes a VOD.
        """
        live_status = str(info.get("live_status") or "").lower()
        is_live = bool(info.get("is_live")) or live_status == "is_live"
        if is_live or live_status in ("is_upcoming", "post_live", "premiering"):
            if is_live:
                raise RuntimeError(
                    "This stream is STILL LIVE. It cannot be clipped while it is "
                    "airing - wait until it ends (a few minutes), then try again."
                )
            raise RuntimeError(
                "This video is not available for clipping yet (live status: "
                f"{live_status or 'unknown'}). Wait until the stream has ended."
            )

    def _get_info(self, video_url: str) -> Dict[str, Any]:
        """Fetch video metadata once per URL (cached for the lifetime of this fetcher)."""
        cached = self._info_cache.get(video_url)
        if cached is not None:
            return cached
        logger.info(f"Extracting video metadata for: {video_url}")
        ydl_opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            **self._cookies_opts(),
            **self._ffmpeg_opt(),
            **self._timeout_opt(),
            **self._original_audio_opt(),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
        except Exception as e:
            if self._is_bot_check_error(e):
                logger.error(
                    "YouTube blocked the request with 'Sign in to confirm you're not a bot'. "
                    "Fix: set YT_COOKIES_FILE (exported cookies.txt) or YT_COOKIES_FROM_BROWSER "
                    "in yt_shorts_bot/.env - see README 'YouTube cookies' section."
                )
            raise
        self._info_cache[video_url] = info
        self._log_audio_tracks(info)
        return info

    def extract_heatmap_and_select_window(self, video_url: str) -> Tuple[Dict[str, Any], float, float, float]:
        """
        Extracts full video metadata including 'heatmap' (Most Replayed) data without downloading.
        Then picks the best 15-20s moment using the configured strategy:

          - "combined": Most Replayed + audio excitement (loud/high-pitched voice)
          - "heatmap":  Most Replayed only
          - "audio":    audio excitement only

        If one signal is missing (e.g. no heatmap for a live VOD, or the audio
        stream cannot be probed), the other signal is used. Only if BOTH fail
        does it fall back to a smart hook window.

        Returns:
            (metadata_dict, peak_time_sec, clip_start_sec, clip_end_sec)
        """
        info = self._get_info(video_url)
        self._ensure_not_live(info)
        duration = float(info.get("duration") or 0.0)

        ranked, used_heatmap, used_audio = self._build_ranked_windows(
            video_url, info, duration, count=1
        )
        used_energy = used_audio  # compatibility alias: probe-based audio analysis

        if not ranked:
            # Last-resort fallback: classic audio-energy scan, then hook window.
            logger.warning("Combined ranking unavailable; falling back to hook window.")
            try:
                win = self.select_window_by_audio_energy(video_url, duration)
                used_audio = True
                used_energy = True
            except Exception as exc:
                logger.debug("Audio-energy fallback failed too: %s", exc)
                if duration >= 60.0:
                    peak_time = min(45.0, max(15.0, duration * 0.15))
                else:
                    peak_time = duration / 2.0
                clip_duration = CLIP_DURATION_SEC
                clip_start = max(0.0, peak_time - clip_duration / 2.0)
                clip_end = clip_start + clip_duration
                if duration > 0 and clip_end > duration:
                    clip_end = duration
                    clip_start = max(0.0, clip_end - clip_duration)
                win = {"start": clip_start, "end": clip_end, "score": 0.0}

        best = ranked[0] if ranked else win
        best_score = float(best.get("score") or 0.0)
        clip_start = float(best["start"])
        clip_end = float(best["end"])
        clip_start, clip_end = self._finalize_window(clip_start, clip_end, duration)
        peak_time = (clip_start + clip_end) / 2.0

        logger.info(
            "Selected segment [%.2fs -> %.2fs] (Duration: %.2fs, strategy=%s, "
            "heatmap used: %s, audio used: %s, score=%.3f)",
            clip_start,
            clip_end,
            clip_end - clip_start,
            self.strategy,
            used_heatmap,
            used_audio,
            best_score,
        )

        # Annotate info with selection metadata
        info["_peak_time"] = peak_time
        info["_clip_start"] = clip_start
        info["_clip_end"] = clip_end
        info["_strategy"] = self.strategy
        info["_used_heatmap"] = used_heatmap
        info["_used_audio"] = used_audio
        info["_used_energy"] = used_energy
        info["_selection_score"] = best_score
        if best.get("heatmap_score") is not None:
            info["_heatmap_score"] = best["heatmap_score"]
        if best.get("audio_score") is not None:
            info["_audio_score"] = best["audio_score"]

        return info, peak_time, clip_start, clip_end

    # ------------------------------------------------------------------
    # Window scoring helpers (heatmap + audio-energy)
    # ------------------------------------------------------------------
    @staticmethod
    def _best_window_from_heatmap(heatmap: List[Dict[str, Any]], duration: float) -> Dict[str, Any]:
        """Finds the highest-average-engagement 15-20s window from YouTube's heatmap buckets."""
        best_win_score = -1.0
        best_win_center = 0.0

        for item in heatmap:
            t_mid = (item.get("start_time", 0.0) + item.get("end_time", 0.0)) / 2.0
            half_win = CLIP_DURATION_SEC / 2.0
            win_start = max(0.0, t_mid - half_win)
            win_end = win_start + CLIP_DURATION_SEC

            in_win = [
                h.get("value", 0.0) for h in heatmap
                if win_start <= (h.get("start_time", 0.0) + h.get("end_time", 0.0)) / 2.0 <= win_end
            ]
            score = sum(in_win) / max(1, len(in_win))
            if score > best_win_score:
                best_win_score = score
                best_win_center = t_mid

        clip_start = max(0.0, best_win_center - CLIP_DURATION_SEC / 2.0)
        clip_end = clip_start + CLIP_DURATION_SEC
        if duration > 0 and clip_end > duration:
            clip_end = duration
            clip_start = max(0.0, clip_end - CLIP_DURATION_SEC)

        return {"start": clip_start, "end": clip_end, "score": best_win_score}

    # ------------------------------------------------------------------
    # Combined "most watched + high-pitched/high-energy voice" ranking
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_scores(values: List[float]) -> List[float]:
        """Min-max normalize to 0..1; all-equal inputs become neutral 0.5."""
        finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
        if not finite:
            return [0.0] * len(values)
        lo, hi = min(finite), max(finite)
        if hi - lo < 1e-12:
            return [0.5] * len(values)
        return [(float(v) - lo) / (hi - lo) for v in values]

    @staticmethod
    def _heatmap_candidates(heatmap: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
        """One 15-20s candidate window per heatmap bucket, scored by average bucket value."""
        half = CLIP_DURATION_SEC / 2.0
        candidates: List[Dict[str, Any]] = []
        for item in heatmap:
            t_mid = (item.get("start_time", 0.0) + item.get("end_time", 0.0)) / 2.0
            start = max(0.0, t_mid - half)
            end = start + CLIP_DURATION_SEC
            if duration > 0 and end > duration:
                end = duration
                start = max(0.0, end - CLIP_DURATION_SEC)
            in_win = [
                h.get("value", 0.0)
                for h in heatmap
                if start <= (h.get("start_time", 0.0) + h.get("end_time", 0.0)) / 2.0 <= end
            ]
            score = float(sum(in_win) / max(1, len(in_win)))
            candidates.append(
                {
                    "start": start,
                    "end": end,
                    "heatmap_score": score,
                    "audio_score": None,
                    "score": score,
                    "source": "heatmap",
                }
            )
        return candidates

    def _audio_candidates(
        self, probes: List[Dict[str, Any]], duration: float
    ) -> List[Dict[str, Any]]:
        """Candidate windows for audio-only mode, centered on each audio probe."""
        half = CLIP_DURATION_SEC / 2.0
        candidates: List[Dict[str, Any]] = []
        for probe in probes:
            center = (probe["start"] + probe["end"]) / 2.0
            start = max(0.0, center - half)
            end = start + CLIP_DURATION_SEC
            if duration > 0 and end > duration:
                end = duration
                start = max(0.0, end - CLIP_DURATION_SEC)
            candidates.append(
                {
                    "start": start,
                    "end": end,
                    "heatmap_score": None,
                    "audio_score": float(probe.get("score") or 0.0),
                    "score": float(probe.get("score") or 0.0),
                    "source": "audio",
                }
            )
        return candidates

    @staticmethod
    def _window_audio_score(
        probes: List[Dict[str, Any]], start: float, end: float
    ) -> Optional[float]:
        """Average excitement of the probes inside a window; nearest probe as fallback."""
        if not probes:
            return None
        center = (start + end) / 2.0
        inside = [p for p in probes if start <= (p["start"] + p["end"]) / 2.0 <= end]
        if inside:
            return float(sum(p.get("score", 0.0) for p in inside) / len(inside))
        nearest = min(probes, key=lambda p: abs(((p["start"] + p["end"]) / 2.0) - center))
        return float(nearest.get("score") or 0.0)

    @staticmethod
    def _finalize_window(start: float, end: float, duration: float) -> Tuple[float, float]:
        """Clamp a window to [0, duration] and enforce 15-20s bounds."""
        start = max(0.0, float(start))
        end = max(start, float(end))
        if duration > 0 and end > duration:
            end = duration
            start = min(start, max(0.0, end - MAX_CLIP_DURATION_SEC))
        if end - start < MIN_CLIP_DURATION_SEC and (duration <= 0 or duration >= MIN_CLIP_DURATION_SEC):
            if duration > 0 and start + MIN_CLIP_DURATION_SEC > duration:
                start = max(0.0, duration - MIN_CLIP_DURATION_SEC)
            end = min(end + (MIN_CLIP_DURATION_SEC - (end - start)), duration if duration > 0 else end + MIN_CLIP_DURATION_SEC)
        if end - start > MAX_CLIP_DURATION_SEC:
            end = start + MAX_CLIP_DURATION_SEC
        return start, end

    def _measure_audio_features(self, stream_url: str, start: float, dur: float) -> Dict[str, float]:
        """
        Decodes a short audio snippet from the direct stream URL and returns
        {energy, centroid, flux}:
          - energy   = RMS loudness (0.0 = silence)
          - centroid = average spectral centroid (Hz) -> high-pitched voice
          - flux     = frame-to-frame spectral change -> shouts/bursts
        Only a few seconds are pulled per probe - never the whole video.
        """
        if not FFMPEG_PATH:
            raise RuntimeError("FFmpeg not found - cannot run audio-excitement analysis.")

        cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
            "-i", stream_url,
            "-map", "0:a:0", "-vn",
            "-f", "s16le", "-ac", "1", "-ar", "16000",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0 or len(proc.stdout) < 1600:  # at least 0.05s of audio
            raise RuntimeError(f"Could not decode audio at {start:.0f}s")

        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return {"energy": 0.0, "centroid": 0.0, "flux": 0.0}

        frame, hop = 2048, 1024
        window = np.hanning(frame).astype(np.float32)
        freqs = np.fft.rfftfreq(frame, d=1.0 / 16000.0)
        n_frames = max(1, 1 + (samples.size - frame) // hop)

        energies = np.zeros(n_frames, dtype=np.float64)
        centroids = np.zeros(n_frames, dtype=np.float64)
        fluxes = np.zeros(n_frames, dtype=np.float64)
        prev_mag: Optional[np.ndarray] = None
        for i in range(n_frames):
            seg = samples[i * hop: i * hop + frame]
            if seg.size < frame:
                seg = np.pad(seg, (0, frame - seg.size))
            seg = seg * window
            mag = np.abs(np.fft.rfft(seg))
            energies[i] = float(np.sqrt(np.mean(seg ** 2)))
            denom = float(np.sum(mag))
            if denom > 1e-9:
                centroids[i] = float(np.sum(freqs * mag) / denom)
            if prev_mag is not None:
                fluxes[i] = float(np.sum(np.maximum(0.0, mag - prev_mag)))
            prev_mag = mag

        return {
            "energy": float(np.mean(energies)),
            "centroid": float(np.mean(centroids)),
            "flux": float(np.mean(fluxes)),
        }

    def _resolve_direct_audio_url(self, info: Dict[str, Any], video_url: str) -> str:
        """
        Find a direct audio-only stream URL for the probes.
        YouTube metadata often stores the audio stream in requested_formats
        while the top-level url is the video stream - check both, then do one
        fast bestaudio metadata request as a guaranteed fallback.
        """
        candidates = [
            fmt
            for group in (info.get("requested_formats") or [], info.get("formats") or [])
            for fmt in group
            if (
                fmt.get("acodec") not in (None, "none")
                and fmt.get("vcodec") in (None, "none")
                and fmt.get("url")
            )
        ]
        if candidates:
            best = max(candidates, key=self._audio_track_key)
            logger.info(
                "Selected audio track: %s (%s, %.0fk, %s) for probing.",
                best.get("language") or "?",
                "original" if int(best.get("language_preference") or -1) >= 10
                else "default" if int(best.get("language_preference") or -1) >= 5
                else "track",
                float(best.get("abr") or 0.0),
                best.get("format_id") or "",
            )
            return str(best["url"])
        if info.get("url") and info.get("vcodec") in (None, "none"):
            return str(info["url"])

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            **self._cookies_opts(),
            **self._ffmpeg_opt(),
            **self._timeout_opt(),
            **self._original_audio_opt(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            audio_info = ydl.extract_info(video_url, download=False)
        stream_url = audio_info.get("url")
        if not stream_url:
            raise RuntimeError("No direct audio stream URL available")
        return str(stream_url)

    def _audio_profile(
        self, info: Dict[str, Any], video_url: str, duration: float
    ) -> List[Dict[str, Any]]:
        """
        Probe loudness + pitch + burstiness across the video with short audio
        snippets (never the whole video) and return per-probe excitement scores.
        Cached per video URL.
        """
        if video_url in self._profile_cache:
            return list(self._profile_cache[video_url])
        if duration <= 0:
            raise RuntimeError("Unknown duration - cannot analyze audio excitement")
        if not FFMPEG_PATH:
            raise RuntimeError("FFmpeg not found - cannot analyze audio excitement")

        stream_url = self._resolve_direct_audio_url(info, video_url)
        sample_len = min(AUDIO_SAMPLE_SEC, 10.0)
        n_samples = min(MAX_AUDIO_SAMPLES, max(12, int(round(duration / 25.0))))
        step = max(sample_len, (duration - sample_len) / max(1, n_samples - 1))

        logger.info(
            "Audio-excitement scan: %d probes (~%.1fs each) over %.0fs video (energy + pitch)...",
            n_samples, sample_len, duration,
        )
        probes: List[Dict[str, Any]] = []
        for i in range(n_samples):
            t = min(max(0.0, i * step), max(0.0, duration - sample_len))
            try:
                feats = self._measure_audio_features(stream_url, t, sample_len)
            except Exception as exc:
                logger.debug("Audio probe at %.1fs failed: %s", t, exc)
                continue
            probes.append({"start": t, "end": min(duration, t + sample_len), **feats})

        if not probes:
            raise RuntimeError("All audio probes failed")

        scores = self._audio_excitement_scores(probes)
        for probe, score in zip(probes, scores):
            probe["score"] = score
        self._profile_cache[video_url] = probes
        return probes

    @staticmethod
    def _audio_excitement_scores(probes: List[Dict[str, Any]]) -> List[float]:
        """
        Combine per-probe loudness, high-pitched spectral content, and burstiness
        into one normalized 0..1 excitement score (per-video min-max scaling).
        """
        energy_norm = YouTubeFetcher._normalize_scores(
            [math.log1p(max(0.0, float(p.get("energy") or 0.0))) for p in probes]
        )
        pitch_norm = YouTubeFetcher._normalize_scores(
            [math.log1p(max(0.0, float(p.get("centroid") or 0.0))) for p in probes]
        )
        flux_norm = YouTubeFetcher._normalize_scores(
            [max(0.0, float(p.get("flux") or 0.0)) for p in probes]
        )
        weights = (AUDIO_ENERGY_WEIGHT, AUDIO_PITCH_WEIGHT, AUDIO_FLUX_WEIGHT)
        total = sum(weights) or 1.0
        scores = []
        for energy, pitch, flux in zip(energy_norm, pitch_norm, flux_norm):
            raw = (energy * AUDIO_ENERGY_WEIGHT + pitch * AUDIO_PITCH_WEIGHT + flux * AUDIO_FLUX_WEIGHT)
            scores.append(min(1.0, max(0.0, raw / total)))
        return scores

    def _build_ranked_windows(
        self,
        video_url: str,
        info: Dict[str, Any],
        duration: float,
        count: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], bool, bool]:
        """
        Rank candidate 15-20s windows using the configured strategy.

        Returns (windows, used_heatmap, used_audio). A window dict contains
        start/end, score, source, plus heatmap_score/audio_score when available.
        """
        count = 1 if count is None else max(1, int(count))
        heatmap = info.get("heatmap") or []
        used_heatmap = bool(heatmap)
        used_audio = False
        probes: Optional[List[Dict[str, Any]]] = None

        strategy = self.strategy
        if strategy in ("combined", "audio"):
            try:
                probes = self._audio_profile(info, video_url, duration)
                used_audio = bool(probes)
            except Exception as exc:
                logger.warning(
                    "Audio-excitement analysis unavailable (%s); using %s.",
                    exc,
                    "heatmap only" if heatmap else "smart fallback",
                )
                probes = None

        # Candidate windows
        candidates: List[Dict[str, Any]] = []
        if strategy == "audio" and probes:
            logger.info("Audio-only mode: ranking windows by loud/high-pitched voice excitation...")
            candidates = self._audio_candidates(probes, duration)
        elif heatmap:
            logger.info(
                "Heatmap data found (%d buckets). Ranking top %d window(s) with strategy '%s'...",
                len(heatmap), count, strategy,
            )
            candidates = self._heatmap_candidates(heatmap, duration)
            if strategy == "audio" and not probes:
                logger.warning("Audio requested but unavailable; ranking by Most Replayed instead.")
        elif probes:
            logger.info("No heatmap data. Ranking windows by audio excitement (voice/pitch/energy)...")
            candidates = self._audio_candidates(probes, duration)
            strategy = "audio"

        if not candidates:
            return [], used_heatmap, used_audio

        # Score every candidate
        heat_values = [c["heatmap_score"] for c in candidates]
        heat_norm = self._normalize_scores(
            [v if v is not None else 0.0 for v in heat_values]
        )
        audio_values = [
            self._window_audio_score(probes, c["start"], c["end"]) if probes else None
            for c in candidates
        ]
        has_audio = any(v is not None for v in audio_values)
        has_heat = all(v is not None for v in heat_values)

        for index, candidate in enumerate(candidates):
            heat_score = heat_norm[index] if has_heat else 0.5
            audio_score = audio_values[index] if audio_values[index] is not None else 0.5
            candidate["heatmap_score"] = candidate.get("heatmap_score")
            candidate["audio_score"] = audio_values[index]

            if strategy == "heatmap" and has_heat:
                combined = heat_score
                source = "heatmap"
            elif strategy == "audio" and has_audio:
                combined = audio_score
                source = "audio"
            elif has_heat and has_audio:
                total_weight = self.heatmap_weight + self.audio_weight
                combined = (
                    self.heatmap_weight * heat_score + self.audio_weight * audio_score
                ) / total_weight if total_weight > 0 else (heat_score + audio_score) / 2.0
                source = "combined"
            elif has_heat:
                combined = heat_score
                source = "heatmap"
            else:
                combined = audio_score
                source = "audio"
            candidate["score"] = combined
            candidate["source"] = source

        # Greedy: pick highest-scoring window, then skip anything overlapping it
        candidates.sort(key=lambda w: w["score"], reverse=True)
        chosen: List[Dict[str, Any]] = []
        for window in candidates:
            if len(chosen) >= count:
                break
            overlap = any(
                window["start"] < c["end"] - 2.0 and c["start"] < window["end"] - 2.0
                for c in chosen
            )
            if not overlap:
                chosen.append(window)

        logger.info(
            "Selected %d top window(s): %s",
            len(chosen),
            ", ".join(
                f"[{w['start']:.0f}s-{w['end']:.0f}s]({w['score']:.2f}/{w['source']})"
                for w in chosen
            ),
        )
        return chosen, used_heatmap, used_audio

    def _measure_audio_energy(self, stream_url: str, start: float, dur: float) -> float:
        """
        Pulls a short audio segment from the direct stream URL and returns its
        RMS energy (0.0 = silence, larger = louder/more exciting). Downloads only
        the few seconds it needs - never the whole video.
        """
        if not FFMPEG_PATH:
            raise RuntimeError("FFmpeg not found - cannot run audio-energy analysis.")

        cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
            "-i", stream_url,
            "-f", "s16le", "-ac", "1", "-ar", "16000",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0 or len(proc.stdout) < 3200:  # at least 0.1s of audio
            raise RuntimeError(f"Could not decode audio at {start:.0f}s")

        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples ** 2)))

    def select_window_by_audio_energy(self, video_url: str, duration: float) -> Dict[str, Any]:
        """
        Fallback for videos WITHOUT heatmap data (live stream VODs etc.).
        Samples audio energy across the whole video, finds the loudest/most
        exciting 15-20s window, then refines around it with a finer pass.
        Returns {"start", "end", "score"}.
        """
        if duration <= 0:
            raise RuntimeError("Unknown duration - cannot analyze energy")

        # Get a direct audio stream URL (fast - metadata only)
        ydl_opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, **self._cookies_opts(), **self._ffmpeg_opt(), **self._timeout_opt(), **self._original_audio_opt()}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
        stream_url = info.get("url")
        if not stream_url:
            raise RuntimeError("No direct stream URL available")

        sample_len = min(CLIP_DURATION_SEC, 20.0)
        # Coarse pass: up to 40 evenly-spaced samples (keeps download tiny)
        n_samples = max(8, min(40, int(duration / max(30.0, sample_len * 2))))
        step = max(sample_len, (duration - sample_len) / max(1, n_samples - 1))

        logger.info(f"Audio-energy scan: {n_samples} sample points over {duration:.0f}s video...")
        best_t, best_e = 0.0, -1.0
        for i in range(n_samples):
            t = min(max(0.0, i * step), max(0.0, duration - sample_len))
            try:
                e = self._measure_audio_energy(stream_url, t, sample_len)
            except Exception as exc:
                logger.debug("Audio sample at %.1fs failed: %s", t, exc)
                continue
            if e > best_e:
                best_e, best_t = e, t

        if best_e < 0:
            raise RuntimeError("All audio samples failed")

        # Refine pass: 9 finer samples around the coarse winner
        refine_center = best_t + sample_len / 2.0
        refine_half = 45.0
        refined_t, refined_e = best_t, best_e
        for i in range(9):
            t = refine_center - refine_half + i * (2 * refine_half / 8.0)
            t = min(max(0.0, t - sample_len / 2.0), max(0.0, duration - sample_len))
            try:
                e = self._measure_audio_energy(stream_url, t, sample_len)
            except Exception as exc:
                logger.debug("Refined audio sample at %.1fs failed: %s", t, exc)
                continue
            if e > refined_e:
                refined_e, refined_t = e, t

        clip_start = max(0.0, refined_t)
        clip_end = min(duration, clip_start + sample_len)
        if clip_end - clip_start < MIN_CLIP_DURATION_SEC:
            clip_end = min(duration, clip_start + MIN_CLIP_DURATION_SEC)

        logger.info(
            f"Audio-energy analysis complete: best window [{clip_start:.1f}s -> {clip_end:.1f}s] "
            f"(energy={refined_e:.4f})"
        )
        return {"start": clip_start, "end": clip_end, "score": refined_e}

    def select_top_windows(self, video_url: str, count: int = 3) -> List[Dict[str, Any]]:
        """
        Returns the top `count` non-overlapping 15-20s windows for a video.
        Uses the configured strategy (combined heatmap + audio excitement, or
        whichever signal is available). Used to make MULTIPLE shorts from one video.
        Returns list of {"start", "end", "score", "source", ...}.
        """
        info = self._get_info(video_url)
        self._ensure_not_live(info)
        duration = float(info.get("duration") or 0.0)
        ranked, _used_heatmap, _used_audio = self._build_ranked_windows(
            video_url, info, duration, count=count
        )
        if not ranked:
            raise RuntimeError("Could not rank any windows for this video")
        return ranked

    def download_clip_section(
        self,
        video_url: str,
        clip_start: float,
        clip_end: float,
        output_path: Optional[Path] = None
    ) -> Path:
        """
        Downloads ONLY the specific [clip_start -> clip_end] segment.
        Tries three strategies in order:
          A. Fast single progressive-format stream URL (no merging)  -> ~1s
          B. Fast video + audio stream pair (1080p h264 + m4a)        -> ~1-2s
          C. Full yt-dlp download then local FFmpeg slice (always works)
        Each result is verified with ffprobe before being accepted.
        """
        if output_path is None:
            v_id = self._extract_video_id(video_url)
            output_path = TEMP_DIR / (
                f"raw_clip_{v_id}_{int(clip_start)}_{int(clip_end)}_{uuid4().hex[:10]}.mp4"
            )

        logger.info(
            f"Downloading segment [{clip_start:.2f}s -> {clip_end:.2f}s] "
            f"for {video_url} to {output_path}..."
        )
        if output_path.exists():
            output_path.unlink()

        # Strategy A: single progressive URL (video+audio in one stream)
        try:
            clip_path = self._slice_progressive(video_url, clip_start, clip_end, output_path)
            logger.info(f"Downloaded clip section via progressive stream ({clip_path.stat().st_size / 1024 / 1024:.2f} MB)")
            return clip_path
        except Exception as e:
            output_path.unlink(missing_ok=True)
            logger.debug(f"Strategy A (progressive single URL) failed: {e}")

        # Strategy B: best video + best audio stream pair
        try:
            clip_path = self._slice_av_pair(video_url, clip_start, clip_end, output_path)
            logger.info(f"Downloaded clip section via video+audio streams ({clip_path.stat().st_size / 1024 / 1024:.2f} MB)")
            return clip_path
        except Exception as e:
            output_path.unlink(missing_ok=True)
            logger.debug(f"Strategy B (AV pair) failed: {e}")

        # Strategy C: full download + local slice (slow, but always works)
        logger.warning("Fast stream slicing failed. Falling back to full download + local slice...")
        try:
            clip_path = self._download_full_then_slice(
                video_url, clip_start, clip_end, output_path
            )
            logger.info(
                "Downloaded clip via full download + slice (%.2f MB)",
                clip_path.stat().st_size / 1024 / 1024,
            )
            return clip_path
        except Exception:
            output_path.unlink(missing_ok=True)
            for fragment in output_path.parent.glob(f"{output_path.name}*.part*"):
                fragment.unlink(missing_ok=True)
            raise

    @staticmethod
    def _extract_video_id(video_url: str) -> str:
        """Extracts an 11-char YouTube video ID from watch/shorts/youtu.be URLs."""
        import re
        m = re.search(r"(?:v=|shorts/|youtu\.be/)([\w-]{11})", video_url)
        if m:
            return m.group(1)
        return video_url.split("v=")[-1].split("&")[0]

    @staticmethod
    def _verify_slice(output_path: Path, clip_start: float, clip_end: float) -> bool:
        """Returns True only if the file exists and its duration roughly matches the requested window."""
        if not output_path.exists() or output_path.stat().st_size < 10_000:
            return False
        if not FFPROBE_PATH:
            logger.warning("ffprobe not found - skipping clip verification.")
            return True
        try:
            out = subprocess.run(
                [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(output_path)],
                capture_output=True, text=True, timeout=30
            )
            dur = float(out.stdout.strip())
            expected = clip_end - clip_start
            return abs(dur - expected) <= max(2.0, expected * 0.15)
        except Exception:
            return False

    @staticmethod
    def _ffmpeg_slice(input_args: List[str], output_path: Path) -> None:
        """Runs ffmpeg: input_args = ['-ss', start, '-to', end, '-i', <input>...] -> output_path."""
        if not FFMPEG_PATH:
            raise RuntimeError(
                "FFmpeg was not found. Run setup.bat / setup.sh, or install FFmpeg "
                "and add it to PATH."
            )
        cmd = [
            FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
            *input_args,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, timeout=180)

    def _slice_progressive(self, video_url: str, clip_start: float, clip_end: float, output_path: Path) -> Path:
        """Strategy A: find the best single progressive (combined A/V) mp4 format and slice its URL directly."""
        ydl_opts = {"format": "best[ext=mp4]/best", "quiet": True, "no_warnings": True, **self._cookies_opts(), **self._ffmpeg_opt(), **self._timeout_opt(), **self._original_audio_opt()}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        # Prefer yt-dlp's own selection (our format_sort puts the original
        # language track FIRST); fall back to a language-aware manual pick
        # instead of the old height-only pick.
        fmt = None
        if info.get("url") and info.get("vcodec") not in (None, "none"):
            fmt = info
        progressive = [
            f for f in info.get("formats", [])
            if f.get("vcodec") not in ("none", None)
            and f.get("acodec") not in ("none", None)
            and f.get("ext") == "mp4"
            and f.get("url")
        ]
        if fmt is None and progressive:
            fmt = max(
                progressive,
                key=lambda x: (
                    int(x.get("language_preference") or -1),
                    int(x.get("height") or 0),
                ),
            )
        if fmt is None:
            raise RuntimeError("No progressive mp4 format available")
        logger.info(
            "Selected progressive track: %s (%s, %sp, %s).",
            fmt.get("language") or "?",
            "original" if int(fmt.get("language_preference") or -1) >= 10
            else "default" if int(fmt.get("language_preference") or -1) >= 5
            else "track",
            fmt.get("height") or "?",
            fmt.get("format_id") or "",
        )

        self._ffmpeg_slice(
            ["-ss", str(clip_start), "-to", str(clip_end), "-i", fmt["url"]],
            output_path
        )
        if not self._verify_slice(output_path, clip_start, clip_end):
            raise RuntimeError("Progressive slice produced an invalid file")
        return output_path

    def _slice_av_pair(self, video_url: str, clip_start: float, clip_end: float, output_path: Path) -> Path:
        """Strategy B: slice the best mp4 video (h264 preferred) + best m4a audio stream pair."""
        ydl_opts = {
            "format": "bestvideo[height<=2160][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/""bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
            **self._cookies_opts(),
            **self._ffmpeg_opt(),
            **self._timeout_opt(),
            **self._original_audio_opt(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        # yt-dlp's selector already picked the pair (format_sort = original
        # language first). Use it when present; otherwise pick manually.
        requested = [
            f for f in (info.get("requested_formats") or [])
            if f.get("url")
        ]
        if requested:
            videos = [f for f in requested if f.get("vcodec") not in (None, "none")]
            audios = [f for f in requested if f.get("acodec") not in (None, "none")]
        else:
            formats = info.get("formats", [])
            videos = [
                f for f in formats
                if f.get("vcodec") not in ("none", None)
                and f.get("height", 0) <= 2160
                and f.get("ext") == "mp4"
                and f.get("url")
            ]
            audios = [
                f for f in formats
                if f.get("acodec") not in ("none", None)
                and f.get("vcodec") in ("none", None)
                and f.get("ext") == "m4a"
                and f.get("url")
            ]
        if not videos or not audios:
            raise RuntimeError("No suitable video/audio stream pair available")

        # Prefer h264 (avc1) streams over av1/vp9 - far more reliable for direct slicing.
        videos.sort(
            key=lambda x: (
                str(x.get("vcodec", "")).startswith("avc1"),
                x.get("height", 0) or 0,
            ),
            reverse=True,
        )
        # ORIGINAL/audio language first (a louder dub must never win), then bitrate.
        audios.sort(key=self._audio_track_key, reverse=True)
        chosen_audio = audios[0]
        logger.info(
            "Selected audio pair: %s (%s, %.0fk, %s).",
            chosen_audio.get("language") or "?",
            "original" if int(chosen_audio.get("language_preference") or -1) >= 10
            else "default" if int(chosen_audio.get("language_preference") or -1) >= 5
            else "track",
            float(chosen_audio.get("abr") or 0.0),
            chosen_audio.get("format_id") or "",
        )

        self._ffmpeg_slice(
            [
                "-ss", str(clip_start), "-to", str(clip_end), "-i", videos[0]["url"],
                "-ss", str(clip_start), "-to", str(clip_end), "-i", audios[0]["url"],
            ],
            output_path
        )
        if not self._verify_slice(output_path, clip_start, clip_end):
            raise RuntimeError("AV pair slice produced an invalid file")
        return output_path

    def _download_full_then_slice(self, video_url: str, clip_start: float, clip_end: float, output_path: Path) -> Path:
        """Strategy C: yt-dlp downloads the full video, FFmpeg slices it locally, then the full file is deleted."""
        v_id = self._extract_video_id(video_url)
        job_id = uuid4().hex[:10]
        full_prefix = f"full_{v_id}_{job_id}"
        full_tmpl = str(TEMP_DIR / f"{full_prefix}.%(ext)s")

        ydl_opts = {
            "format": "bestvideo[height<=2160][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/""bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": full_tmpl,
            "quiet": True,
            "no_warnings": True,
            **self._cookies_opts(),
            **self._ffmpeg_opt(),
            **self._timeout_opt(),
            **self._original_audio_opt(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        matches = [p for p in TEMP_DIR.glob(f"{full_prefix}.*") if p.stat().st_size > 0]
        if not matches:
            raise RuntimeError("Full download produced no usable file")
        full_path = max(matches, key=lambda p: p.stat().st_size)

        try:
            self._ffmpeg_slice(
                ["-ss", str(clip_start), "-to", str(clip_end), "-i", str(full_path)],
                output_path
            )
            if not self._verify_slice(output_path, clip_start, clip_end):
                raise RuntimeError("Slice of full download produced an invalid file")
        finally:
            # Free disk: delete the full video + any fragments immediately
            for p in TEMP_DIR.glob(f"{full_prefix}*"):
                try:
                    p.unlink()
                except OSError:
                    pass
        return output_path
