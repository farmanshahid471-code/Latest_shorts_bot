"""Cross-post finished Shorts to TikTok and Bilibili.

Each destination account opts in independently via the control panel
(``tiktok_enabled`` / ``bilibili_enabled`` plus that platform's credentials).
Cross-posting runs AFTER a Short is rendered and is independent of the YouTube
result: a YouTube quota wait, auth failure, or failed upload never blocks the
social post, and a failed social post never blocks YouTube.

Idempotency: every attempt is recorded in the ``social_posts`` table, so a
retry never publishes the same Short twice to the same platform. DRY_RUN mode
prepares everything but sends nothing.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Optional

from .config import ACCOUNTS_FILE, DRY_RUN, logger
from .models import StateDB
from .platform_settings import platform_metadata
from .social_bilibili import DESC_MAX_LEN as BILIBILI_DESC_MAX_LEN
from .social_bilibili import BilibiliAPIError, BilibiliUploader
from .manual_export import export_for_manual_upload
from .social_tiktok import TITLE_MAX_LEN as TIKTOK_TITLE_MAX_LEN
from .social_tiktok import TikTokAPIError, TikTokUploader
from .storage import CloudStorageManager

DEST_TIKTOK = "tiktok"
DEST_BILIBILI = "bilibili"

SOCIAL_POSTED = "POSTED"
SOCIAL_FAILED = "FAILED"
SOCIAL_SKIPPED = "SKIPPED"
SOCIAL_DRY_RUN = "DRY_RUN_READY"
SOCIAL_ALREADY_POSTED = "ALREADY_POSTED"
SOCIAL_EXPORTED = "EXPORTED_FOR_MANUAL"

# Where manual-mode clips land when the account does not override it.
DEFAULT_BILIBILI_EXPORT_DIR = ACCOUNTS_FILE.parent / "bilibili_manual"


def bilibili_is_manual(account: Optional[dict]) -> bool:
    """True when Bilibili should export to disk instead of uploading."""
    return str((account or {}).get("bilibili_mode") or "").strip().lower() == "manual"


def bilibili_dub_enabled(account: Optional[dict]) -> bool:
    """True when clips bound for Bilibili should be dubbed into Chinese."""
    return bool((account or {}).get("bilibili_dub_enabled"))


def bilibili_export_dir(account: Optional[dict]) -> Path:
    """Folder for manual-mode exports (account override, else the default)."""
    custom = str((account or {}).get("bilibili_export_dir") or "").strip()
    if custom:
        path = Path(custom).expanduser()
        # Relative paths resolve next to accounts.json, matching the rest of
        # the bot's path handling.
        return path if path.is_absolute() else ACCOUNTS_FILE.parent / path
    return DEFAULT_BILIBILI_EXPORT_DIR

_TOKEN_KEYS = {
    DEST_TIKTOK: ("tiktok_access_token", "tiktok_refresh_token"),
    DEST_BILIBILI: ("bilibili_access_token", "bilibili_refresh_token"),
}


def is_enabled(account: Optional[dict], destination: str) -> bool:
    """Has this destination account opted into cross-posting?"""
    if not account:
        return False
    if destination == DEST_TIKTOK:
        return bool(account.get("tiktok_enabled"))
    if destination == DEST_BILIBILI:
        return bool(account.get("bilibili_enabled"))
    return False


def build_social_caption(metadata: Optional[dict], destination: str) -> str:
    """Caption from the YouTube title + hashtags (no source URL, no dupes)."""
    metadata = metadata or {}
    title = str(metadata.get("title") or "").strip() or "New Short 🎬"
    tags: list[str] = []
    for raw in metadata.get("tags") or []:
        # Hashtags cannot contain whitespace ("funny cats" -> "#funnycats").
        tag = re.sub(r"\s+", "", str(raw or "").strip().lstrip("#"))
        if tag and tag not in tags:
            tags.append(tag)
    lowered_title = title.lower()
    fresh = [tag for tag in tags if tag.lower() not in lowered_title]
    tag_line = " ".join(f"#{tag}" for tag in fresh)
    if destination == DEST_BILIBILI:
        # Bilibili keeps the title and hashtags apart: tags travel in their own
        # field, so the description only needs the readable title line.
        return " ".join(f"{title} {tag_line}".split())[:BILIBILI_DESC_MAX_LEN]
    # TikTok titles are a single short line.
    return " ".join(f"{title} {tag_line}".split())[:TIKTOK_TITLE_MAX_LEN]


def save_social_tokens(account_name: str, destination: str, values: dict) -> bool:
    """Atomically persist refreshed tokens into the existing account entry.

    Only the whitelisted token keys for ``destination`` are written; nothing
    is created when the account no longer exists. Returns True on success.
    """
    name = str(account_name or "").strip()
    allowed = set(_TOKEN_KEYS.get(destination, ()))
    updates = {
        str(key): str(value or "").strip()
        for key, value in (values or {}).items()
        if str(key) in allowed and str(value or "").strip()
    }
    if not name or not updates:
        return False
    try:
        if not ACCOUNTS_FILE.exists():
            return False
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read accounts.json to save %s tokens: %s", destination, exc)
        return False
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    target = next(
        (
            item
            for item in accounts
            if str(item.get("name") or "").strip().casefold() == name.casefold()
        ),
        None,
    )
    if target is None:
        return False
    target.update(updates)
    try:
        temporary = ACCOUNTS_FILE.with_name(
            f".{ACCOUNTS_FILE.name}.{secrets.token_hex(6)}.tmp"
        )
        temporary.write_text(
            json.dumps({"accounts": accounts}, indent=2), encoding="utf-8"
        )
        os.replace(temporary, ACCOUNTS_FILE)
    except OSError as exc:
        logger.warning("Could not save refreshed %s tokens: %s", destination, exc)
        return False
    logger.info("Saved refreshed %s token(s) for account '%s'.", destination, name)
    return True


class SocialDestinations:
    """Attempt each enabled destination; never raises into the pipeline."""

    def __init__(
        self,
        state_db: Optional[StateDB] = None,
        storage: Optional[CloudStorageManager] = None,
        dry_run: Optional[bool] = None,
    ):
        self.state_db = state_db if state_db else StateDB()
        self.storage = storage if storage else CloudStorageManager()
        self.dry_run = DRY_RUN if dry_run is None else bool(dry_run)

    # ------------------------------------------------------------------
    def crosspost(
        self,
        account: Optional[dict],
        video_id: str,
        video_path: Optional[Path],
        r2_key: Optional[str] = None,
        metadata: Optional[dict] = None,
        only_platforms: Optional[list[str]] = None,
        srt_path: Optional[Path] = None,
    ) -> dict[str, str]:
        """Post to every enabled destination; returns {destination: status}.

        ``only_platforms`` restricts the attempt to those destinations, which
        the scheduler uses so a clip rendered at one platform's clip length is
        never posted to a platform that asked for a different length.
        """
        results: dict[str, str] = {}
        if not account:
            return results
        name = str(account.get("name") or "").strip()
        if not name:
            return results
        allowed = (
            {str(item).strip().lower() for item in only_platforms}
            if only_platforms is not None
            else None
        )
        for destination in (DEST_TIKTOK, DEST_BILIBILI):
            if not is_enabled(account, destination):
                continue
            if allowed is not None and destination not in allowed:
                continue
            # Each destination may override the title/hashtags for itself.
            destination_metadata = platform_metadata(account, destination, metadata)
            try:
                if destination == DEST_TIKTOK:
                    results[destination] = self._crosspost_tiktok(
                        account, name, video_id, video_path, r2_key,
                        destination_metadata,
                    )
                else:
                    results[destination] = self._crosspost_bilibili(
                        account, name, video_id, video_path, destination_metadata,
                        srt_path=srt_path,
                    )
            except Exception as exc:
                # A social failure must never break the YouTube pipeline.
                logger.warning(
                    "[%s] %s cross-post crashed (%s); YouTube flow continues.",
                    name,
                    destination,
                    exc,
                )
                try:
                    self.state_db.record_social_post(
                        video_id=video_id,
                        account=name,
                        destination=destination,
                        status=SOCIAL_FAILED,
                        error_msg=str(exc)[:500],
                    )
                except Exception as record_exc:
                    logger.debug(
                        "[%s] Could not record %s failure: %s",
                        name,
                        destination,
                        record_exc,
                    )
                results[destination] = SOCIAL_FAILED
        return results

    # ------------------------------------------------------------------
    def _already_posted(self, video_id: str, name: str, destination: str) -> bool:
        try:
            existing = self.state_db.get_social_post(video_id, name, destination)
        except Exception:
            return False
        # A manual export counts as done too: re-running must not write the
        # same clip into the export folder twice.
        return bool(
            existing
            and existing.get("status") in (SOCIAL_POSTED, SOCIAL_EXPORTED)
        )

    def _record(
        self,
        video_id: str,
        name: str,
        destination: str,
        status: str,
        remote_id: str = "",
        error_msg: str = "",
    ) -> None:
        try:
            self.state_db.record_social_post(
                video_id=video_id,
                account=name,
                destination=destination,
                remote_id=remote_id,
                status=status,
                error_msg=error_msg[:500],
            )
        except Exception as exc:
            logger.warning("[%s] Could not record %s status: %s", name, destination, exc)

    def _public_video_url(self, r2_key: Optional[str]) -> str:
        try:
            return self.storage.public_url_for_key(r2_key) if r2_key else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    def _crosspost_tiktok(
        self,
        account: dict,
        name: str,
        video_id: str,
        video_path: Optional[Path],
        r2_key: Optional[str],
        metadata: Optional[dict],
    ) -> str:
        if self._already_posted(video_id, name, DEST_TIKTOK):
            logger.info("[%s] TikTok: already posted; skipping.", name)
            return SOCIAL_ALREADY_POSTED
        open_id = str(account.get("tiktok_open_id") or "").strip()
        token = str(account.get("tiktok_access_token") or "").strip()
        if not open_id or not token:
            reason = "TikTok is enabled but the open_id / access token is missing."
            logger.warning("[%s] %s", name, reason)
            self._record(video_id, name, DEST_TIKTOK, SOCIAL_SKIPPED, error_msg=reason)
            return SOCIAL_SKIPPED
        if self.dry_run:
            logger.info("[%s] [DRY-RUN] TikTok video prepared but not published.", name)
            self._record(video_id, name, DEST_TIKTOK, SOCIAL_DRY_RUN)
            return SOCIAL_DRY_RUN
        video_url = self._public_video_url(r2_key)
        source = Path(video_path) if video_path else None
        if not video_url and (not source or not source.is_file()):
            reason = "TikTok needs the rendered video file or a public R2 URL."
            logger.warning("[%s] %s", name, reason)
            self._record(video_id, name, DEST_TIKTOK, SOCIAL_SKIPPED, error_msg=reason)
            return SOCIAL_SKIPPED

        def _on_tokens(new_access: str, new_refresh: str, _expires: Any) -> None:
            save_social_tokens(
                name,
                DEST_TIKTOK,
                {"tiktok_access_token": new_access, "tiktok_refresh_token": new_refresh},
            )

        uploader = TikTokUploader(
            open_id=open_id,
            access_token=token,
            refresh_token=str(account.get("tiktok_refresh_token") or ""),
            client_key=str(account.get("tiktok_client_key") or ""),
            client_secret=str(account.get("tiktok_client_secret") or ""),
            privacy_level=str(account.get("tiktok_privacy_level") or "PUBLIC_TO_EVERYONE"),
            dry_run=False,
            on_tokens_refreshed=_on_tokens,
        )
        try:
            title = build_social_caption(metadata, DEST_TIKTOK)
            publish_id = uploader.upload_video(
                source if source else Path(""),
                title=title,
                video_url=video_url,
            )
        except TikTokAPIError as exc:
            logger.warning("[%s] TikTok post failed: %s", name, exc)
            self._record(video_id, name, DEST_TIKTOK, SOCIAL_FAILED, error_msg=str(exc))
            return SOCIAL_FAILED
        logger.info("[%s] TikTok video published (publish_id %s).", name, publish_id)
        self._record(video_id, name, DEST_TIKTOK, SOCIAL_POSTED, remote_id=publish_id)
        return SOCIAL_POSTED


    # ------------------------------------------------------------------
    def _crosspost_bilibili(
        self,
        account: dict,
        name: str,
        video_id: str,
        video_path: Optional[Path],
        metadata: Optional[dict],
        srt_path: Optional[Path] = None,
    ) -> str:
        if self._already_posted(video_id, name, DEST_BILIBILI):
            logger.info("[%s] Bilibili: already posted; skipping.", name)
            return SOCIAL_ALREADY_POSTED
        # Chinese dubbing happens before EITHER mode consumes the file, so the
        # manual export and the API upload both get the dubbed cut.
        video_path = self._maybe_dub(account, name, video_path, srt_path)
        if bilibili_is_manual(account):
            return self._export_bilibili(account, name, video_id, video_path, metadata)
        client_id = str(account.get("bilibili_client_id") or "").strip()
        client_secret = str(account.get("bilibili_client_secret") or "").strip()
        token = str(account.get("bilibili_access_token") or "").strip()
        if not client_id or not client_secret or not token:
            reason = ("Bilibili is enabled but the client id / secret / access "
                      "token is missing.")
            logger.warning("[%s] %s", name, reason)
            self._record(video_id, name, DEST_BILIBILI, SOCIAL_SKIPPED, error_msg=reason)
            return SOCIAL_SKIPPED
        if self.dry_run:
            logger.info("[%s] [DRY-RUN] Bilibili archive prepared but not submitted.", name)
            self._record(video_id, name, DEST_BILIBILI, SOCIAL_DRY_RUN)
            return SOCIAL_DRY_RUN
        source = Path(video_path) if video_path else None
        if not source or not source.is_file():
            # Bilibili has no pull-from-URL flow: the bytes must exist locally.
            reason = "Bilibili needs the rendered video file on disk."
            logger.warning("[%s] %s", name, reason)
            self._record(video_id, name, DEST_BILIBILI, SOCIAL_SKIPPED, error_msg=reason)
            return SOCIAL_SKIPPED

        def _on_tokens(new_access: str, new_refresh: str, _expires: Any) -> None:
            save_social_tokens(
                name,
                DEST_BILIBILI,
                {
                    "bilibili_access_token": new_access,
                    "bilibili_refresh_token": new_refresh,
                },
            )

        uploader = BilibiliUploader(
            client_id=client_id,
            client_secret=client_secret,
            access_token=token,
            refresh_token=str(account.get("bilibili_refresh_token") or ""),
            tid=account.get("bilibili_tid"),
            copyright_type=account.get("bilibili_copyright"),
            source=str(account.get("bilibili_source") or ""),
            dry_run=False,
            on_tokens_refreshed=_on_tokens,
        )
        meta = metadata or {}
        title = str(meta.get("title") or "").strip() or "New Short"
        try:
            resource_id = uploader.upload_video(
                source,
                title=title,
                description=build_social_caption(meta, DEST_BILIBILI),
                tags=meta.get("tags") or [],
                cover_path=self._cover_path(meta),
            )
        except BilibiliAPIError as exc:
            logger.warning("[%s] Bilibili post failed: %s", name, exc)
            self._record(video_id, name, DEST_BILIBILI, SOCIAL_FAILED, error_msg=str(exc))
            return SOCIAL_FAILED
        logger.info("[%s] Bilibili archive submitted for review (%s).", name, resource_id)
        self._record(video_id, name, DEST_BILIBILI, SOCIAL_POSTED, remote_id=resource_id)
        return SOCIAL_POSTED

    # ------------------------------------------------------------------
    def _maybe_dub(
        self,
        account: dict,
        name: str,
        video_path: Optional[Path],
        srt_path: Optional[Path],
    ) -> Optional[Path]:
        """Return a Chinese-dubbed copy of the clip when the account asks.

        Dubbing is best-effort: any failure logs a warning and falls back to
        the original audio, because a missing dub is far better than a missing
        post.
        """
        if not bilibili_dub_enabled(account):
            return video_path
        source = Path(video_path) if video_path else None
        if not source or not source.is_file():
            return video_path
        if not srt_path or not Path(srt_path).is_file():
            logger.warning(
                "[%s] Bilibili dubbing is on but this clip has no transcript; "
                "enable subtitles for the account so it gets transcribed. "
                "Posting the original audio.",
                name,
            )
            return video_path

        from .dubbing import DEFAULT_VOICE, DubbingError, dub_video

        voice = str(account.get("bilibili_dub_voice") or "").strip() or DEFAULT_VOICE
        try:
            original_volume = float(account.get("bilibili_dub_original_volume", 0.12))
        except (TypeError, ValueError):
            original_volume = 0.12
        target = str(account.get("bilibili_dub_language") or "zh-CN").strip() or "zh-CN"
        dubbed = source.with_name(f"{source.stem}_zh{source.suffix}")
        try:
            logger.info("[%s] Dubbing clip for Bilibili with voice %s...", name, voice)
            dub_video(
                source, Path(srt_path), dubbed,
                voice=voice,
                target_language=target,
                original_volume=max(0.0, min(original_volume, 1.0)),
            )
        except DubbingError as exc:
            logger.warning(
                "[%s] Bilibili dubbing failed (%s); posting the original audio.",
                name, exc,
            )
            return video_path
        except Exception as exc:
            logger.warning(
                "[%s] Bilibili dubbing crashed (%s); posting the original audio.",
                name, exc,
            )
            return video_path
        if not dubbed.is_file() or dubbed.stat().st_size == 0:
            logger.warning("[%s] Dub produced no file; posting the original audio.", name)
            return video_path
        logger.info("[%s] Bilibili clip dubbed: %s", name, dubbed.name)
        return dubbed

    # ------------------------------------------------------------------
    def _export_bilibili(
        self,
        account: dict,
        name: str,
        video_id: str,
        video_path: Optional[Path],
        metadata: Optional[dict],
    ) -> str:
        """Manual mode: save the clip + a notes file instead of uploading.

        Bilibili only grants upload API access to certified mainland-China
        enterprises, so many users upload by hand. The bot still does all the
        work up to the upload itself.
        """
        source = Path(video_path) if video_path else None
        if not source or not source.is_file():
            reason = "Bilibili manual export needs the rendered video file on disk."
            logger.warning("[%s] %s", name, reason)
            self._record(video_id, name, DEST_BILIBILI, SOCIAL_SKIPPED, error_msg=reason)
            return SOCIAL_SKIPPED

        meta = dict(metadata or {})
        # Bilibili keeps tags in their own field, so the description is the
        # readable caption the same way the API path builds it.
        meta.setdefault("description", build_social_caption(meta, DEST_BILIBILI))

        copyright_type = str(account.get("bilibili_copyright") or "").strip()
        extra = {
            "分区 / category (tid)": account.get("bilibili_tid") or "",
            "copyright": ("转载 / Repost" if copyright_type == "2" else "自制 / Original")
            if copyright_type else "",
            "repost source": account.get("bilibili_source") or "",
        }
        try:
            exported_video, notes = export_for_manual_upload(
                source,
                Path(bilibili_export_dir(account)),
                meta,
                account_name=name,
                video_id=video_id,
                source_url=str(meta.get("source_url") or ""),
                platform_label="Bilibili",
                extra_fields=extra,
            )
        except Exception as exc:
            logger.warning("[%s] Bilibili manual export failed: %s", name, exc)
            self._record(video_id, name, DEST_BILIBILI, SOCIAL_FAILED, error_msg=str(exc))
            return SOCIAL_FAILED

        logger.info(
            "[%s] Bilibili clip saved for manual upload: %s (details in %s)",
            name, exported_video, notes.name,
        )
        self._record(
            video_id, name, DEST_BILIBILI, SOCIAL_EXPORTED,
            remote_id=exported_video.name,
        )
        return SOCIAL_EXPORTED

    @staticmethod
    def _cover_path(metadata: Optional[dict]) -> Optional[Path]:
        """Thumbnail for the archive cover, when the pipeline produced one."""
        raw = str((metadata or {}).get("thumbnail_path") or "").strip()
        if not raw:
            return None
        candidate = Path(raw)
        return candidate if candidate.is_file() else None


def crosspost_short(
    account: Optional[dict],
    video_id: str,
    video_path: Optional[Path],
    r2_key: Optional[str] = None,
    metadata: Optional[dict] = None,
    state_db: Optional[StateDB] = None,
    dry_run: Optional[bool] = None,
    only_platforms: Optional[list[str]] = None,
) -> dict[str, str]:
    """Convenience wrapper around SocialDestinations (never raises)."""
    try:
        poster = SocialDestinations(state_db=state_db, dry_run=dry_run)
        return poster.crosspost(
            account, video_id, video_path, r2_key, metadata,
            only_platforms=only_platforms,
        )
    except Exception as exc:
        logger.warning("Social cross-post skipped: %s", exc)
        return {}
