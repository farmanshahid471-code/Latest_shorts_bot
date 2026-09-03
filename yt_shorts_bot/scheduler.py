"""Concurrency-safe, dynamically reloadable scheduler for clip farming."""
from __future__ import annotations

import shutil
import signal
import threading
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from .config import (
    ACCOUNTS,
    CANDIDATE_ATTEMPTS_PER_CHANNEL,
    CYCLE_INTERVAL_HOURS,
    DELETE_AFTER_UPLOAD,
    DELETE_R2_AFTER_UPLOAD,
    ENABLE_SMART_TITLES,
    KEEP_LOCAL_SHORTS,
    KEEP_SHORTS_DIR,
    SELECTION_ORDER,
    SHORTS_PER_VIDEO,
    TARGET_CHANNELS,
    TOP_WATERMARK_TEXT,
    _ENV_FILE,
    logger,
)
from .fetcher import (
    YouTubeFetcher,
    is_age_restricted_source,
    is_permanent_source_failure,
)
from .hashtags import save_metadata_sidecar, srt_to_text
from .models import StateDB
from .pathutils import safe_account_slug
from .processor import VideoProcessor
from .runtime import pipeline_guard
from .storage import CloudStorageManager
from .timewindows import (
    is_within_posting_window,
    posting_window_configured,
    posting_window_label,
    seconds_until_posting_window,
    validate_posting_window,
)
from .uploader import (
    UPLOAD_AUTH_REQUIRED,
    UPLOAD_CHANNEL_MISMATCH,
    UPLOAD_DRY_RUN,
    UPLOAD_QUOTA_REACHED,
    YouTubeUploader,
    is_real_upload_id,
    resolve_credentials,
)


class ShortsBotScheduler:
    """Find high-engagement windows and publish them for each account."""

    def __init__(
        self,
        channels: Optional[list[str]] = None,
        interval_hours: int = CYCLE_INTERVAL_HOURS,
        accounts: Optional[list[dict]] = None,
        state_db: Optional[StateDB] = None,
        processor: Optional[VideoProcessor] = None,
        storage: Optional[CloudStorageManager] = None,
    ):
        self.interval_hours = max(1, int(interval_hours))
        self._account_filter = (
            [str(account.get("name") or "").casefold() for account in accounts]
            if accounts is not None
            else None
        )
        self.accounts = list(accounts) if accounts is not None else self._load_accounts_fresh()
        if (
            channels is not None
            and len(self.accounts) == 1
            and self.accounts[0].get("name") == "default"
        ):
            self.accounts[0]["target_channels"] = list(channels)
        self.state_db = state_db or StateDB()
        self.processor = processor or VideoProcessor()
        self.storage = storage or CloudStorageManager()
        self.stop_event = threading.Event()
        self._running = False
        self._last_upload_result: Optional[str] = None
        self._setup_signal_handlers()

    @staticmethod
    def _load_accounts_fresh() -> list[dict]:
        try:
            from .config import _load_accounts

            return _load_accounts()
        except Exception as exc:
            logger.error("Could not reload accounts.json: %s", exc)
            return list(ACCOUNTS)

    def _current_interval_hours(self) -> int:
        try:
            value = dotenv_values(_ENV_FILE).get("CYCLE_INTERVAL_HOURS")
            return max(1, int(float(value))) if value else self.interval_hours
        except (TypeError, ValueError, OSError):
            return self.interval_hours

    def _setup_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handle_shutdown(_signum, _frame):
            logger.info("Termination requested; stopping after the active operation.")
            self.stop()

        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)

    def run_single_cycle(
        self,
        accounts: Optional[list[dict]] = None,
        preflight_oauth: bool = False,
    ) -> int:
        with pipeline_guard(blocking=False) as acquired:
            if not acquired:
                logger.warning("Another clip pipeline is active; cycle skipped.")
                return 0
            if accounts is None:
                fresh = self._load_accounts_fresh()
                if self._account_filter is None:
                    self.accounts = fresh
                else:
                    fresh_by_name = {
                        str(item.get("name") or "").casefold(): item for item in fresh
                    }
                    self.accounts = [
                        fresh_by_name.get(str(item.get("name") or "").casefold(), item)
                        for item in self.accounts
                        if str(item.get("name") or "").casefold() in self._account_filter
                    ]
            selected = list(accounts) if accounts is not None else list(self.accounts)
            logger.info("=== STARTING CLIP FARMING CYCLE ===")
            total_uploaded = 0
            for account in selected:
                if self.stop_event.is_set():
                    break
                if not account.get("enabled", True):
                    logger.info(
                        "[%s] Account is disabled; skipping. Enable it in the panel to "
                        "include it in automatic cycles.",
                        account.get("name"),
                    )
                    continue
                try:
                    total_uploaded += self._run_cycle_for_account(
                        account,
                        preflight_oauth=preflight_oauth,
                    )
                except Exception as exc:
                    logger.exception(
                        "Cycle failed for account '%s': %s", account.get("name"), exc
                    )
            logger.info("=== CLIP CYCLE COMPLETE: %s real upload(s) ===", total_uploaded)
            return total_uploaded

    def _run_cycle_for_account(
        self,
        account: dict,
        preflight_oauth: bool = False,
    ) -> int:
        name = str(account.get("name") or "default").strip()
        window_error = validate_posting_window(account)
        if window_error:
            logger.error("[%s] Invalid posting window; account skipped: %s", name, window_error)
            return 0
        if not is_within_posting_window(account):
            logger.info(
                "[%s] Outside posting window (%s); no automatic upload this cycle.",
                name,
                posting_window_label(account),
            )
            return 0
        if posting_window_configured(account):
            logger.info("[%s] Posting window is open: %s.", name, posting_window_label(account))
        if "target_channels" in account:
            channels = [str(value).strip() for value in account.get("target_channels") or [] if str(value).strip()]
        else:
            channels = list(TARGET_CHANNELS) if name == "default" else []
        if not channels:
            logger.warning("[%s] No source channels configured; account skipped.", name)
            return 0

        max_daily = max(1, int(account.get("max_daily_uploads") or 10))
        aspect = account.get("aspect")
        fill = account.get("fill")
        shorts_per_video = min(20, max(1, int(account.get("shorts_per_video") or SHORTS_PER_VIDEO)))
        min_gap = max(0, int(account.get("min_minutes_between_uploads") or 0))
        spread = self._spread_scheduling_enabled(account)
        if spread:
            logger.info(
                "[%s] Scheduled publishing is on: uploads go up privately and "
                "YouTube publishes them at spread-out times inside %s.",
                name,
                posting_window_label(account),
            )
        max_candidate_attempts = max(
            1,
            int(
                account.get("candidate_attempts_per_channel")
                or CANDIDATE_ATTEMPTS_PER_CHANNEL
            ),
        )
        subtitles_enabled = bool(account.get("subtitles_enabled", True))
        expected_channel = str(
            account.get("expected_channel") or account.get("connected_channel") or ""
        ).strip()
        expected_channel_id = str(account.get("connected_channel_id") or "").strip()
        watermark_enabled = account.get("watermark_enabled")
        watermark_text = str(account.get("watermark") or "").strip()
        top_enabled = account.get("top_watermark_enabled")
        top_text = str(account.get("top_watermark") or TOP_WATERMARK_TEXT or "").strip()
        logo_position = (
            str(account.get("logo_position") or "").strip()
            if account.get("logo_remove")
            else "off"
        )

        fetcher = YouTubeFetcher(
            channels=channels,
            strategy=account.get("selection_strategy"),
            heatmap_weight=account.get("heatmap_weight"),
            audio_weight=account.get("audio_excitement_weight"),
        )
        client_secret, token = resolve_credentials(account)
        uploader = YouTubeUploader(
            client_secret_file=client_secret,
            token_file=token,
            state_db=self.state_db,
        )
        # Fail before downloading or rendering anything.  A forced refresh is
        # essential here: a one-hour access token can still look valid after a
        # Testing-mode refresh token has reached its seven-day expiry.
        check_connection = getattr(uploader, "check_connection", None)
        if (
            preflight_oauth
            and not getattr(uploader, "dry_run", False)
            and callable(check_connection)
        ):
            auth_ok, auth_detail = check_connection(
                expected_channel=expected_channel,
                expected_channel_id=expected_channel_id,
            )
            if not auth_ok:
                logger.error(
                    "[%s] OAuth preflight FAILED; automatic uploads are blocked: %s",
                    name,
                    auth_detail,
                )
                return 0
            logger.info("[%s] OAuth preflight passed: %s", name, auth_detail)
        uploaded_count = 0

        for channel_url in channels:
            if self.stop_event.is_set():
                break
            can_upload, remaining = self.state_db.can_upload_today(max_daily, name)
            if not can_upload:
                logger.warning(
                    "[%s] Rolling 24-hour upload cap reached (max %d/24h); account paused "
                    "until an older upload ages out. %s",
                    name,
                    max_daily,
                    channel_url,
                )
                break
            logger.info(
                "[%s] %d of %d upload slot(s) remain in the rolling 24-hour window.",
                name,
                remaining,
                max_daily,
            )
            order = str(account.get("selection_order") or SELECTION_ORDER).lower()
            if order not in ("newest", "oldest", "random"):
                order = "newest"
            try:
                videos = fetcher.fetch_channel_recent_videos(channel_url, order=order)
            except Exception as exc:
                logger.error("[%s] Could not scan %s: %s", name, channel_url, exc)
                continue

            scanned_total = len(videos)
            skipped_uploaded = 0
            skipped_claimed = 0
            attempts = 0
            for video in videos:
                if self.stop_event.is_set():
                    break
                video_id = video["video_id"]
                if self.state_db.is_video_processed(video_id, account=name):
                    skipped_uploaded += 1
                    if skipped_uploaded % 50 == 0:
                        logger.info(
                            "[%s] %d of %d candidate(s) already uploaded/processed - "
                            "still scanning the window for the next one...",
                            name,
                            skipped_uploaded,
                            scanned_total,
                        )
                    continue
                claim = self.state_db.claim_video(video_id, name)
                if not claim:
                    skipped_claimed += 1
                    continue
                attempts += 1
                logger.info(
                    "[%s] Picked candidate %s ('%s', %ss) - %d already uploaded, "
                    "%d held by an active claim, %d remaining in window. "
                    "(attempt %d of %d this cycle)",
                    name,
                    video_id,
                    video.get("title", ""),
                    video.get("duration", "?"),
                    skipped_uploaded,
                    skipped_claimed,
                    max(0, scanned_total - skipped_uploaded - skipped_claimed - 1),
                    attempts,
                    max_candidate_attempts,
                )
                handled = False
                try:
                    # When YouTube-side scheduling is on, the LOCAL gap/pacing
                    # is unnecessary: parts upload immediately as private and
                    # YouTube drips them public at the planned times.
                    if not spread and not self._wait_for_upload_gap(name, min_gap):
                        break
                    if shorts_per_video > 1:
                        ranked = fetcher.select_top_windows(
                            video["url"], count=shorts_per_video
                        )
                        windows = [
                            {"start": item["start"], "end": item["end"]}
                            for item in ranked
                        ]
                        # Get complete metadata once for every part.
                        info, _peak, _start, _end = fetcher.extract_heatmap_and_select_window(
                            video["url"]
                        )
                    else:
                        info, _peak, start, end = fetcher.extract_heatmap_and_select_window(
                            video["url"]
                        )
                        windows = [{"start": start, "end": end}]
                    uploaded_count += self._process_video_windows(
                        video_id,
                        video["url"],
                        video["title"],
                        channel_url,
                        windows,
                        account=name,
                        max_daily=max_daily,
                        uploader=uploader,
                        info=info,
                        aspect=aspect,
                        fill=fill,
                        logo_position=logo_position,
                        like_subscribe=None if watermark_enabled is None else bool(watermark_enabled),
                        like_subscribe_text=watermark_text,
                        top_watermark_enabled=None if top_enabled is None else bool(top_enabled),
                        top_watermark_text=top_text,
                        extra_hashtags=str(account.get("extra_hashtags") or "").strip(),
                        title_prefix=account.get("title_prefix"),
                        title_hashtags=str(account.get("title_hashtags") or "").strip(),
                        custom_description=str(account.get("custom_description") or "").strip(),
                        smart_titles=account.get("smart_titles"),
                        delete_after_upload=bool(
                            account.get("delete_after_upload", DELETE_AFTER_UPLOAD)
                        ),
                        delete_r2_after_upload=bool(
                            account.get("delete_r2_after_upload", DELETE_R2_AFTER_UPLOAD)
                        ),
                        subtitles_enabled=subtitles_enabled,
                        min_gap_minutes=0 if spread else min_gap,
                        publish_time_fn=(
                            (lambda: self._plan_publish_at(account, max_daily))
                            if spread
                            else None
                        ),
                        expected_channel=expected_channel,
                        expected_channel_id=expected_channel_id,
                    )
                    handled = True
                except Exception as exc:
                    if is_age_restricted_source(exc):
                        # Unlike removed/private videos, this can succeed after
                        # the user uploads fresh cookies from a verified 18+
                        # account. Never make an age gate terminal.
                        logger.warning(
                            "[%s] %s ('%s') needs age-verified YouTube cookies. "
                            "Keeping it retryable and moving to the next candidate.",
                            name,
                            video_id,
                            video.get("title", ""),
                        )
                        self.state_db.record_video_state(
                            video_id=video_id,
                            video_url=video["url"],
                            title=video["title"],
                            status="SOURCE_AUTH_REQUIRED",
                            error_msg=str(exc)[:500],
                            account=name,
                        )
                    elif is_permanent_source_failure(exc):
                        # Removed/private/region-blocked videos cannot succeed.
                        logger.warning(
                            "[%s] %s ('%s') can never be clipped: %s. Marking SKIPPED "
                            "and moving to the next candidate.",
                            name,
                            video_id,
                            video.get("title", ""),
                            exc,
                        )
                        self.state_db.record_video_state(
                            video_id=video_id,
                            video_url=video["url"],
                            title=video["title"],
                            status="SKIPPED",
                            error_msg=str(exc)[:500],
                            account=name,
                        )
                    else:
                        logger.exception("[%s] Failed to process %s: %s", name, video["url"], exc)
                        self.state_db.record_video_state(
                            video_id=video_id,
                            video_url=video["url"],
                            title=video["title"],
                            status="PROCESSING_FAILED",
                            error_msg=str(exc)[:500],
                            account=name,
                        )
                finally:
                    self.state_db.release_video_claim(video_id, name, claim)
                if handled:
                    # Natural pacing: one source video from each source channel/cycle.
                    break
                # The candidate failed: try the NEXT unprocessed one this cycle
                # instead of wasting the whole hour on one poisoned video.
                if attempts >= max_candidate_attempts:
                    logger.warning(
                        "[%s] %d candidate attempt(s) failed on %s this cycle; "
                        "pausing until the next cycle.",
                        name,
                        attempts,
                        channel_url,
                    )
                    break
            else:
                # The loop ended without picking a video: every candidate in the
                # scanned window is already handled. Say so instead of staying silent.
                if not self.stop_event.is_set():
                    if scanned_total == 0:
                        logger.warning(
                            "[%s] No candidates were returned for %s (channel "
                            "listing empty or all videos are already Shorts).",
                            name,
                            channel_url,
                        )
                    else:
                        logger.warning(
                            "[%s] All %d scanned candidate(s) on %s are already "
                            "uploaded/claimed (%d uploaded, %d held by an active claim). "
                            "Nothing left to do in this window - raise FETCH_SCAN_LIMIT "
                            "(default 300) to scan deeper.",
                            name,
                            scanned_total,
                            channel_url,
                            skipped_uploaded,
                            skipped_claimed,
                        )

        return uploaded_count

    def _minutes_since_last_upload(self, account: str) -> float:
        """Minutes since this account's last REAL upload; infinity if none."""
        last_upload = self.state_db.get_last_upload_time(account)
        if not last_upload:
            return float("inf")
        return (datetime.now(timezone.utc) - last_upload).total_seconds() / 60.0

    @staticmethod
    def _spread_scheduling_enabled(account: dict) -> bool:
        """Has this account opted into YouTube-side scheduled publishing?"""
        raw = account.get("spread_uploads_across_window")
        if isinstance(raw, bool):
            on = raw
        else:
            on = str(raw or "").strip().lower() in ("true", "on", "1", "yes")
        if not on:
            return False
        # Spreading only makes sense with a real (valid, non-24h) window.
        start = str(account.get("posting_start_time") or "").strip()
        end = str(account.get("posting_end_time") or "").strip()
        return (
            posting_window_configured(account)
            and validate_posting_window(account) is None
            and start != end
        )

    def _plan_publish_at(
        self,
        account: dict,
        max_daily: int,
        now_utc: Optional[datetime] = None,
    ) -> Optional[datetime]:
        """Plan the YouTube ``publishAt`` instant for the NEXT upload.

        Today's uploads are spaced evenly from the first upload of the day
        (or ~15 minutes from now) until the posting window ends - so a bot
        started at 08:00 with a 06:00-17:00 window spreads 08:00 -> 17:00
        instead of anchoring to 06:00 or skipping uploads. The local upload
        gap is skipped in this mode: everything is uploaded now, privately,
        and YouTube itself publishes each part at its planned time.

        Returns None when no sensible plan exists (window over soon, 24h
        window, setting off) - callers then upload publicly right away.
        """
        if not self._spread_scheduling_enabled(account):
            return None
        name = str(account.get("name") or "default").strip()
        now = now_utc or datetime.now(timezone.utc)
        zone = ZoneInfo(str(account["posting_timezone"]))
        local_now = now.astimezone(zone)
        start_clock = dt_time.fromisoformat(
            str(account["posting_start_time"]).strip()
        )
        end_clock = dt_time.fromisoformat(str(account["posting_end_time"]).strip())
        end_local = datetime.combine(local_now.date(), end_clock, tzinfo=zone)
        if start_clock > end_clock and local_now.time() > end_clock:
            # Overnight window, currently before midnight: the end is tomorrow.
            end_local += timedelta(days=1)
        end_utc = end_local.astimezone(timezone.utc)

        used = self.state_db.get_uploads_in_last_24_hours(account=name)
        first = self.state_db.get_first_upload_time(name)
        anchor = now + timedelta(minutes=15)
        if first is not None and first > anchor:
            anchor = first
        span = (end_utc - anchor).total_seconds()
        if span < 10 * 60:
            return None
        step = span / max(1, int(max_daily))
        slot = min(used, max(0, int(max_daily) - 1))
        publish_at = anchor + timedelta(seconds=step * slot)
        if publish_at <= now + timedelta(minutes=5):
            publish_at = now + timedelta(minutes=15)
        if publish_at > end_utc:
            return None
        return publish_at

    def _wait_for_upload_gap(self, account: str, min_gap_minutes: int) -> bool:
        if min_gap_minutes <= 0:
            return not self.stop_event.is_set()
        last_upload = self.state_db.get_last_upload_time(account)
        if not last_upload:
            return not self.stop_event.is_set()
        elapsed = (datetime.now(timezone.utc) - last_upload).total_seconds() / 60.0
        if elapsed >= min_gap_minutes:
            return not self.stop_event.is_set()
        wait_seconds = max(1, int((min_gap_minutes - elapsed) * 60))
        logger.info(
            "[%s] min_minutes_between_uploads=%d: last upload was %.1f min ago. "
            "Waiting %dm %02ds before the next upload... (press Stop to skip)",
            account,
            min_gap_minutes,
            elapsed,
            wait_seconds // 60,
            wait_seconds % 60,
        )
        interrupted = self.stop_event.wait(wait_seconds)
        if interrupted:
            logger.info("[%s] Upload-gap wait interrupted by a stop request.", account)
        else:
            logger.info("[%s] Upload gap elapsed; continuing.", account)
        return not interrupted

    @staticmethod
    def _state_for_upload_result(result: Optional[str]) -> str:
        if is_real_upload_id(result):
            return "UPLOADED_YOUTUBE"
        return {
            UPLOAD_QUOTA_REACHED: "QUOTA_WAIT",
            UPLOAD_DRY_RUN: "DRY_RUN_READY",
            UPLOAD_AUTH_REQUIRED: "AUTH_REQUIRED",
            UPLOAD_CHANNEL_MISMATCH: "CHANNEL_MISMATCH",
        }.get(result, "UPLOAD_FAILED")

    def _process_video_windows(
        self,
        video_id: str,
        video_url: str,
        video_title: str,
        channel_url: str,
        windows: list[dict],
        account: str = "",
        max_daily: int = 10,
        uploader: Optional[YouTubeUploader] = None,
        info: Optional[dict] = None,
        aspect: Optional[str] = None,
        fill: Optional[str] = None,
        logo_position: Optional[str] = None,
        like_subscribe: Optional[bool] = None,
        like_subscribe_text: Optional[str] = None,
        top_watermark_enabled: Optional[bool] = None,
        top_watermark_text: Optional[str] = None,
        extra_hashtags: str = "",
        title_prefix: Optional[str] = None,
        title_hashtags: str = "",
        custom_description: str = "",
        smart_titles: Optional[bool] = None,
        delete_after_upload: bool = False,
        delete_r2_after_upload: bool = False,
        subtitles_enabled: Optional[bool] = None,
        min_gap_minutes: int = 0,
        publish_time_fn: Optional[Callable[[], Optional[datetime]]] = None,
        expected_channel: Optional[str] = None,
        expected_channel_id: Optional[str] = None,
    ) -> int:
        uploader = uploader or YouTubeUploader(state_db=self.state_db)
        info = info or {"title": video_title}
        total = len(windows)
        uploaded_count = 0
        part_ids: list[str] = []
        account_slug = safe_account_slug(account)
        smart_title_enabled = (
            ENABLE_SMART_TITLES if smart_titles is None else bool(smart_titles)
        )

        for index, window in enumerate(windows, start=1):
            if self.stop_event.is_set():
                break
            start, end = float(window["start"]), float(window["end"])
            part_id = f"{video_id}_part{index}" if total > 1 else video_id
            part_ids.append(part_id)
            if self.state_db.is_video_processed(part_id, account=account):
                continue
            # Burst guard: the min-upload gap must hold between EVERY upload,
            # not just between source videos. Without this, a multi-part video
            # uploaded all its parts back-to-back within minutes. Remaining
            # parts stay unprocessed (retryable) and resume in a later cycle.
            if (
                index > 1
                and min_gap_minutes > 0
                and self._minutes_since_last_upload(account) < min_gap_minutes
            ):
                logger.info(
                    "[%s] Deferring %d remaining part(s) of %s to a later cycle: "
                    "min_minutes_between_uploads=%d has not elapsed since the last "
                    "upload (%.1f min ago). Parts already uploaded stay done.",
                    account,
                    total - index + 1,
                    video_id,
                    min_gap_minutes,
                    self._minutes_since_last_upload(account),
                )
                break
            raw_path: Optional[Path] = None
            processed_path: Optional[Path] = None
            srt_path: Optional[Path] = None
            saved_copy: Optional[Path] = None
            uploaded_key: Optional[str] = None
            short_id: Optional[str] = None
            transcript_text = ""
            try:
                raw_path = self._download_window(video_url, start, end)
                srt_path = raw_path.with_suffix(".srt")

                # Smart titles need the words spoken inside THIS selected clip.
                # Previously transcription was only a side effect of burning
                # subtitles, so disabling captions silently made smart titles
                # copy the long source video's original title. Generate the SRT
                # independently whenever smart titles are enabled; the renderer
                # reuses it when captions are also enabled.
                transcribe = getattr(
                    self.processor, "transcribe_and_generate_srt", None
                )
                probe_audio = getattr(self.processor, "_probe_has_audio", None)
                if smart_title_enabled and callable(transcribe):
                    has_audio = (
                        bool(probe_audio(raw_path))
                        if callable(probe_audio)
                        else True
                    )
                    if has_audio:
                        try:
                            generated_srt = transcribe(raw_path, srt_path=srt_path)
                            if generated_srt:
                                srt_path = Path(generated_srt)
                        except Exception as exc:
                            if subtitles_enabled is not False:
                                raise
                            logger.warning(
                                "[%s] Smart-title transcription failed; using the "
                                "clean source title for this clip: %s",
                                account,
                                exc,
                            )
                    else:
                        logger.warning(
                            "[%s] Smart titles are enabled but this clip has no "
                            "audio; using the clean source title.",
                            account,
                        )

                processed_path = raw_path.parent / (
                    f"processed_{raw_path.stem}_{uuid4().hex[:10]}.mp4"
                )
                processed_path = self.processor.process_clip_to_short(
                    raw_path,
                    output_path=processed_path,
                    srt_path=srt_path,
                    aspect=aspect,
                    fill=fill,
                    logo_position=logo_position,
                    like_subscribe=like_subscribe,
                    like_subscribe_text=like_subscribe_text,
                    top_watermark_enabled=top_watermark_enabled,
                    top_watermark_text=top_watermark_text,
                    subtitles=subtitles_enabled,
                )
                if KEEP_LOCAL_SHORTS and processed_path.is_file():
                    saved_copy = KEEP_SHORTS_DIR / f"{account_slug}_short_{part_id}.mp4"
                    saved_copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(processed_path, saved_copy)

                r2_key = f"shorts/{account_slug}/{part_id}.mp4"
                try:
                    uploaded_key = self.storage.upload_file(
                        processed_path, r2_key=r2_key
                    )
                except Exception as exc:
                    logger.error("[%s] Optional R2 backup failed: %s", account, exc)

                transcript_text = srt_to_text(srt_path) if srt_path else ""
                if smart_title_enabled:
                    if transcript_text:
                        logger.info(
                            "[%s] Smart title will use %d characters transcribed "
                            "from the selected clip.",
                            account,
                            len(transcript_text),
                        )
                    else:
                        logger.warning(
                            "[%s] Smart titles are enabled but no spoken transcript "
                            "was produced; using the clean source title.",
                            account,
                        )
                self.state_db.record_video_state(
                    video_id=part_id,
                    video_url=video_url,
                    channel_id=channel_url,
                    title=video_title,
                    peak_time=(start + end) / 2.0,
                    clip_start=start,
                    clip_end=end,
                    r2_key=uploaded_key or "",
                    status="PENDING_UPLOAD",
                    account=account,
                )
                short_id = uploader.upload_short(
                    video_path=processed_path,
                    original_video_id=part_id,
                    original_title=video_title,
                    original_url=video_url,
                    channel_name=channel_url,
                    part_label=None,
                    account=account,
                    account_max_daily=max_daily,
                    info=info,
                    transcript_text=transcript_text,
                    extra_hashtags=extra_hashtags,
                    title_prefix=title_prefix,
                    title_hashtags=title_hashtags,
                    custom_description=custom_description,
                    smart_titles=smart_titles,
                    expected_channel=expected_channel,
                    expected_channel_id=expected_channel_id,
                    content_language=(
                        getattr(self.processor, "detected_language", "")
                        if getattr(self.processor, "detected_language_probability", 0.0) >= 0.5
                        else ""
                    ),
                    publish_at=publish_time_fn() if publish_time_fn else None,
                )
                self._last_upload_result = short_id
                state = self._state_for_upload_result(short_id)
                self.state_db.record_video_state(
                    video_id=part_id,
                    video_url=video_url,
                    channel_id=channel_url,
                    title=video_title,
                    peak_time=(start + end) / 2.0,
                    clip_start=start,
                    clip_end=end,
                    r2_key=uploaded_key or "",
                    youtube_short_id=short_id if is_real_upload_id(short_id) else "",
                    status=state,
                    error_msg="" if is_real_upload_id(short_id) else state,
                    account=account,
                )

                if uploader.last_metadata:
                    save_metadata_sidecar(
                        saved_copy if saved_copy and saved_copy.exists() else processed_path,
                        uploader.last_metadata,
                        source_url=video_url,
                        short_id=short_id if is_real_upload_id(short_id) else "",
                        account=account,
                    )

                if is_real_upload_id(short_id):
                    uploaded_count += 1
                    if delete_after_upload and saved_copy:
                        saved_copy.unlink(missing_ok=True)
                        saved_copy.with_suffix(".txt").unlink(missing_ok=True)
                    if delete_r2_after_upload and uploaded_key and self.storage.client:
                        try:
                            self.storage.client.delete_object(
                                Bucket=self.storage.bucket_name, Key=uploaded_key
                            )
                        except Exception as exc:
                            logger.warning("[%s] Could not delete R2 backup: %s", account, exc)
                else:
                    logger.warning(
                        "[%s] Part %s remains retryable with state %s.",
                        account,
                        index,
                        state,
                    )
            except Exception as exc:
                self.state_db.record_video_state(
                    video_id=part_id,
                    video_url=video_url,
                    title=video_title,
                    peak_time=(start + end) / 2.0,
                    clip_start=start,
                    clip_end=end,
                    status="PROCESSING_FAILED",
                    error_msg=str(exc),
                    account=account,
                )
                logger.exception("[%s] Part %s failed: %s", account, index, exc)
            finally:
                self.storage.cleanup_local_files(raw_path, processed_path, srt_path)

            if short_id == UPLOAD_QUOTA_REACHED:
                break

        if total > 1 and part_ids and all(
            self.state_db.is_video_processed(part_id, account=account)
            for part_id in part_ids
        ):
            self.state_db.record_video_state(
                video_id=video_id,
                video_url=video_url,
                channel_id=channel_url,
                title=video_title,
                clip_start=float(windows[0]["start"]),
                clip_end=float(windows[-1]["end"]),
                status="PROCESSED_MULTI",
                account=account,
            )
        return uploaded_count

    def _download_window(self, video_url: str, start: float, end: float) -> Path:
        return YouTubeFetcher().download_clip_section(video_url, start, end)

    def _keep_local_copy(self, processed_short_path, video_id: str, account: str = "") -> None:
        """Compatibility helper retained for external callers/tests."""
        if not KEEP_LOCAL_SHORTS or not processed_short_path:
            return
        source = Path(processed_short_path)
        if not source.is_file():
            return
        destination = KEEP_SHORTS_DIR / f"{safe_account_slug(account)}_short_{video_id}.mp4"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def _next_wait_seconds(self, interval_hours: int) -> float:
        base_wait = max(60.0, float(interval_hours) * 3600.0)
        opening_delays = [
            seconds_until_posting_window(account)
            for account in self.accounts
            if account.get("enabled", True)
            and posting_window_configured(account)
            and not is_within_posting_window(account)
            and validate_posting_window(account) is None
        ]
        positive = [delay for delay in opening_delays if delay > 0]
        return max(1.0, min([base_wait, *positive])) if positive else base_wait

    def start_24_7_loop(self) -> None:
        if self._running:
            return
        self._running = True
        self.stop_event.clear()
        logger.info("Starting interruptible clip scheduler.")
        try:
            while not self.stop_event.is_set():
                self.run_single_cycle(preflight_oauth=True)
                if self.stop_event.is_set():
                    break
                hours = self._current_interval_hours()
                wait_seconds = self._next_wait_seconds(hours)
                logger.info("Next clip cycle in %.1f minute(s).", wait_seconds / 60.0)
                self.stop_event.wait(wait_seconds)
        finally:
            self._running = False
            logger.info("Clip scheduler stopped.")

    def stop(self) -> None:
        self.stop_event.set()
        logger.info("Scheduler stop requested.")
