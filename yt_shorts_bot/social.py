"""Cross-post finished Shorts to TikTok.

Each destination account opts in independently via the control panel
(``tiktok_enabled`` plus that platform's credentials).
Cross-posting runs AFTER a Short is rendered and is independent of the YouTube
result: a YouTube quota wait, auth failure, or failed upload never blocks the
TikTok post, and a failed social post never blocks YouTube.

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
from .social_tiktok import TITLE_MAX_LEN as TIKTOK_TITLE_MAX_LEN
from .social_tiktok import TikTokAPIError, TikTokUploader
from .storage import CloudStorageManager

DEST_TIKTOK = "tiktok"

SOCIAL_POSTED = "POSTED"
SOCIAL_FAILED = "FAILED"
SOCIAL_SKIPPED = "SKIPPED"
SOCIAL_DRY_RUN = "DRY_RUN_READY"
SOCIAL_ALREADY_POSTED = "ALREADY_POSTED"

_TOKEN_KEYS = {
    DEST_TIKTOK: ("tiktok_access_token", "tiktok_refresh_token"),
}


def is_enabled(account: Optional[dict], destination: str) -> bool:
    """Has this destination account opted into cross-posting?"""
    if not account:
        return False
    if destination == DEST_TIKTOK:
        return bool(account.get("tiktok_enabled"))
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
    ) -> dict[str, str]:
        """Post to every enabled destination; returns {destination: status}."""
        results: dict[str, str] = {}
        if not account:
            return results
        name = str(account.get("name") or "").strip()
        if not name:
            return results
        for destination in (DEST_TIKTOK,):
            if not is_enabled(account, destination):
                continue
            try:
                results[destination] = self._crosspost_tiktok(
                    account, name, video_id, video_path, r2_key, metadata
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
        return bool(existing and existing.get("status") == SOCIAL_POSTED)

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


def crosspost_short(
    account: Optional[dict],
    video_id: str,
    video_path: Optional[Path],
    r2_key: Optional[str] = None,
    metadata: Optional[dict] = None,
    state_db: Optional[StateDB] = None,
    dry_run: Optional[bool] = None,
) -> dict[str, str]:
    """Convenience wrapper around SocialDestinations (never raises)."""
    try:
        poster = SocialDestinations(state_db=state_db, dry_run=dry_run)
        return poster.crosspost(account, video_id, video_path, r2_key, metadata)
    except Exception as exc:
        logger.warning("Social cross-post skipped: %s", exc)
        return {}
