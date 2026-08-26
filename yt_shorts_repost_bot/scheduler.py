"""Concurrency-safe, dynamically reloadable 24/7 scheduler for the repost bot."""
from __future__ import annotations

import shutil
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from dotenv import dotenv_values

from .config import (
    ACCOUNTS,
    CYCLE_INTERVAL_HOURS,
    DELETE_AFTER_UPLOAD,
    DELETE_R2_AFTER_UPLOAD,
    KEEP_LOCAL_SHORTS,
    KEEP_SHORTS_DIR,
    MAX_SHORTS_PER_CHANNEL_CYCLE,
    SELECTION_ORDER,
    TARGET_CHANNELS,
    TOP_WATERMARK_TEXT,
    _ENV_FILE,
    logger,
)
from .fetcher import ShortsFetcher
from .models import StateDB
from .pathutils import safe_account_slug
from .reprocessor import ShortReprocessor
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


class ShortsRepostScheduler:
    """Scan, prepare and upload Shorts for all enabled destination accounts."""

    def __init__(
        self,
        channels: Optional[list[str]] = None,
        interval_hours: int = CYCLE_INTERVAL_HOURS,
        accounts: Optional[list[dict]] = None,
        state_db: Optional[StateDB] = None,
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

    def run_single_cycle(self, accounts: Optional[list[dict]] = None) -> int:
        with pipeline_guard(blocking=False) as acquired:
            if not acquired:
                logger.warning("Another repost pipeline is already active; cycle skipped.")
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
            logger.info("=== STARTING SHORTS REPOST CYCLE ===")
            uploaded_count = 0
            for account in selected:
                if self.stop_event.is_set():
                    break
                if not account.get("enabled", True):
                    logger.info("Skipping disabled account: %s", account.get("name"))
                    continue
                try:
                    uploaded_count += self._run_cycle_for_account(account)
                except Exception as exc:
                    logger.exception(
                        "Cycle failed for account '%s': %s", account.get("name"), exc
                    )
            logger.info("=== CYCLE COMPLETE: %s real upload(s) ===", uploaded_count)
            return uploaded_count

    def _run_cycle_for_account(self, account: dict) -> int:
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
        # An explicit empty list means disabled/no sources. It must never fall
        # back to packaged example channels.
        if "target_channels" in account:
            channels = [str(value).strip() for value in account.get("target_channels") or [] if str(value).strip()]
        else:
            channels = list(TARGET_CHANNELS) if name == "default" else []
        if not channels:
            logger.warning("[%s] No source channels configured; account skipped.", name)
            return 0

        max_daily = max(1, int(account.get("max_daily_uploads") or 10))
        process_mode = str(account.get("process_mode") or "copy").strip().lower()
        if process_mode not in {"copy", "render"}:
            process_mode = "copy"
        aspect = account.get("aspect") or "auto"
        fill = account.get("fill") or "crop"
        subtitles_enabled = bool(account.get("subtitles_enabled", False))
        expected_channel = str(
            account.get("expected_channel") or account.get("connected_channel") or ""
        ).strip()
        expected_channel_id = str(account.get("connected_channel_id") or "").strip()

        watermark_enabled = account.get("watermark_enabled")
        watermark_text = str(account.get("watermark") or "").strip()
        top_enabled = account.get("top_watermark_enabled")
        top_text = str(account.get("top_watermark") or TOP_WATERMARK_TEXT or "").strip()
        if process_mode != "render" and (
            (watermark_enabled is not False and watermark_text)
            or (top_enabled is not False and top_text)
        ):
            logger.warning(
                "[%s] Watermarks require render mode; copy mode preserves the source.",
                name,
            )

        fetcher = ShortsFetcher(channels=channels)
        reprocessor = ShortReprocessor()
        client_secret, token = resolve_credentials(account)
        uploader = YouTubeUploader(
            client_secret_file=client_secret,
            token_file=token,
            state_db=self.state_db,
        )

        uploaded_count = 0
        for channel_url in channels:
            if self.stop_event.is_set():
                break
            can_upload, remaining = self.state_db.can_upload_today(max_daily, name)
            if not can_upload:
                logger.warning("[%s] Rolling upload cap reached; account paused.", name)
                break
            logger.info("[%s] %s quota slot(s) available.", name, remaining)
            order = str(account.get("selection_order") or SELECTION_ORDER).lower()
            if order not in ("newest", "oldest", "random"):
                order = "newest"
            try:
                shorts = fetcher.fetch_channel_recent_shorts(channel_url, order=order)
            except Exception as exc:
                logger.error("[%s] Could not scan %s: %s", name, channel_url, exc)
                continue

            attempted = 0
            max_per_channel = max(
                1,
                int(
                    account.get("max_shorts_per_channel_cycle")
                    or MAX_SHORTS_PER_CHANNEL_CYCLE
                ),
            )
            min_gap = max(0, int(account.get("min_minutes_between_uploads") or 0))
            for short in shorts:
                if self.stop_event.is_set() or attempted >= max_per_channel:
                    break
                video_id = short["video_id"]
                if self.state_db.is_video_processed(video_id, account=name):
                    continue
                claim = self.state_db.claim_video(video_id, account=name)
                if not claim:
                    logger.info("[%s] %s is already claimed by another worker.", name, video_id)
                    continue
                try:
                    if not self._wait_for_upload_gap(name, min_gap):
                        break
                    attempted += 1
                    uploaded = self._process_one(
                        video_id,
                        short["url"],
                        short["title"],
                        channel_url,
                        account=name,
                        max_daily=max_daily,
                        fetcher=fetcher,
                        reprocessor=reprocessor,
                        uploader=uploader,
                        like_subscribe=None if watermark_enabled is None else bool(watermark_enabled),
                        like_subscribe_text=watermark_text,
                        top_watermark_enabled=None if top_enabled is None else bool(top_enabled),
                        top_watermark_text=top_text,
                        extra_hashtags=str(account.get("extra_hashtags") or "").strip(),
                        title_prefix=account.get("title_prefix"),
                        title_hashtags=str(account.get("title_hashtags") or "").strip(),
                        smart_titles=account.get("smart_titles"),
                        delete_after_upload=bool(
                            account.get("delete_after_upload", DELETE_AFTER_UPLOAD)
                        ),
                        delete_r2_after_upload=bool(
                            account.get("delete_r2_after_upload", DELETE_R2_AFTER_UPLOAD)
                        ),
                        process_mode=process_mode,
                        subtitles_enabled=subtitles_enabled,
                        expected_channel=expected_channel,
                        expected_channel_id=expected_channel_id,
                        aspect=aspect,
                        fill=fill,
                    )
                    uploaded_count += int(uploaded)
                    if self._last_upload_result == UPLOAD_QUOTA_REACHED:
                        break
                except Exception as exc:
                    logger.exception("[%s] Failed to repost %s: %s", name, short["url"], exc)
                    self.state_db.record_video_state(
                        video_id=video_id,
                        video_url=short["url"],
                        title=short["title"],
                        status="PROCESSING_FAILED",
                        error_msg=str(exc),
                        account=name,
                    )
                finally:
                    self.state_db.release_video_claim(video_id, name, claim)

        logger.info("[%s] Account cycle finished: %s upload(s).", name, uploaded_count)
        return uploaded_count

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
            "[%s] Waiting up to %ss for configured upload pacing.",
            account,
            wait_seconds,
        )
        return not self.stop_event.wait(wait_seconds)

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

    def _process_one(
        self,
        video_id,
        video_url,
        video_title,
        channel_url,
        account: str = "",
        max_daily: int = 10,
        fetcher=None,
        reprocessor=None,
        uploader=None,
        like_subscribe: Optional[bool] = None,
        like_subscribe_text: Optional[str] = None,
        top_watermark_enabled: Optional[bool] = None,
        top_watermark_text: Optional[str] = None,
        extra_hashtags: str = "",
        title_prefix: Optional[str] = None,
        title_hashtags: str = "",
        smart_titles: Optional[bool] = None,
        delete_after_upload: bool = False,
        delete_r2_after_upload: bool = False,
        process_mode: Optional[str] = None,
        subtitles_enabled: Optional[bool] = None,
        expected_channel: Optional[str] = None,
        expected_channel_id: Optional[str] = None,
        aspect: Optional[str] = None,
        fill: Optional[str] = None,
    ) -> bool:
        fetcher = fetcher or ShortsFetcher()
        reprocessor = reprocessor or ShortReprocessor()
        uploader = uploader or YouTubeUploader(state_db=self.state_db)
        raw_path: Optional[Path] = None
        final_path: Optional[Path] = None
        uploaded_key: Optional[str] = None
        short_id: Optional[str] = None
        local_copy: Optional[Path] = None
        account_slug = safe_account_slug(account)
        self._last_upload_result = None

        try:
            raw_path = fetcher.download_short(video_url)
            final_path = raw_path.parent / f"final_{raw_path.stem}_{uuid4().hex[:10]}.mp4"
            final_path = reprocessor.process_short(
                raw_path,
                output_path=final_path,
                like_subscribe=like_subscribe,
                like_subscribe_text=like_subscribe_text,
                top_watermark_enabled=top_watermark_enabled,
                top_watermark_text=top_watermark_text,
                mode=process_mode,
                subtitles=subtitles_enabled,
                aspect=aspect,
                fill=fill,
            )
            if KEEP_LOCAL_SHORTS and final_path.is_file():
                local_copy = KEEP_SHORTS_DIR / f"{account_slug}_repost_{video_id}.mp4"
                local_copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(final_path, local_copy)
                logger.info("[%s] Saved review copy: %s", account, local_copy)

            r2_key = f"reposts/{account_slug}/{video_id}.mp4"
            try:
                uploaded_key = self.storage.upload_file(final_path, r2_key=r2_key)
            except Exception as exc:
                logger.error("[%s] Optional R2 backup failed: %s", account, exc)

            info: dict = {"title": video_title}
            try:
                info = fetcher.get_short_info(video_url) or info
            except Exception as exc:
                logger.warning("[%s] Full metadata fetch failed: %s", account, exc)

            self.state_db.record_video_state(
                video_id=video_id,
                video_url=video_url,
                channel_id=channel_url,
                title=video_title,
                r2_key=uploaded_key or "",
                status="PENDING_UPLOAD",
                account=account,
            )
            short_id = uploader.upload_short(
                video_path=final_path,
                original_video_id=video_id,
                original_title=video_title,
                original_url=video_url,
                channel_name=channel_url,
                account=account,
                account_max_daily=max_daily,
                info=info,
                transcript_text="",
                extra_hashtags=extra_hashtags,
                title_prefix=title_prefix,
                title_hashtags=title_hashtags,
                smart_titles=smart_titles,
                expected_channel=expected_channel,
                expected_channel_id=expected_channel_id,
            )
            self._last_upload_result = short_id
            status = self._state_for_upload_result(short_id)
            self.state_db.record_video_state(
                video_id=video_id,
                video_url=video_url,
                channel_id=channel_url,
                title=video_title,
                r2_key=uploaded_key or "",
                youtube_short_id=short_id if is_real_upload_id(short_id) else "",
                status=status,
                error_msg="" if is_real_upload_id(short_id) else status,
                account=account,
            )

            metadata = uploader.last_metadata
            if metadata:
                from .hashtags import save_metadata_sidecar

                sidecar_target = local_copy if local_copy and local_copy.exists() else final_path
                save_metadata_sidecar(
                    sidecar_target,
                    metadata,
                    source_url=video_url,
                    short_id=short_id if is_real_upload_id(short_id) else "",
                    account=account,
                )

            if is_real_upload_id(short_id):
                if delete_after_upload and local_copy:
                    for path in (local_copy, local_copy.with_suffix(".txt")):
                        path.unlink(missing_ok=True)
                if delete_r2_after_upload and uploaded_key and self.storage.client:
                    try:
                        self.storage.client.delete_object(
                            Bucket=self.storage.bucket_name, Key=uploaded_key
                        )
                    except Exception as exc:
                        logger.warning("[%s] Could not delete R2 backup: %s", account, exc)
                logger.info("[%s] Uploaded '%s' -> %s", account, video_title, short_id)
                return True

            logger.warning(
                "[%s] Not uploaded (%s); review copy remains queued.",
                account,
                self._state_for_upload_result(short_id),
            )
            return False
        finally:
            self.storage.cleanup_local_files(raw_path, final_path)

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
            logger.warning("Scheduler is already running.")
            return
        self._running = True
        self.stop_event.clear()
        logger.info("Starting interruptible Shorts repost scheduler.")
        try:
            while not self.stop_event.is_set():
                self.run_single_cycle()
                if self.stop_event.is_set():
                    break
                hours = self._current_interval_hours()
                wait_seconds = self._next_wait_seconds(hours)
                logger.info("Next repost cycle in %.1f minute(s).", wait_seconds / 60.0)
                self.stop_event.wait(wait_seconds)
        finally:
            self._running = False
            logger.info("Shorts repost scheduler stopped.")

    def stop(self) -> None:
        self.stop_event.set()
        logger.info("Scheduler stop requested.")
