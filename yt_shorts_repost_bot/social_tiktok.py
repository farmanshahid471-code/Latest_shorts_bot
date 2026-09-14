"""TikTok posting through the official Content Posting API.

Direct-post flow:
  1. POST /v2/post/publish/video/init/ with FILE_UPLOAD (bot sends the bytes)
     or PULL_FROM_URL (TikTok fetches a public URL) -> upload_url + publish_id.
  2. FILE_UPLOAD only: PUT the video bytes to upload_url in Content-Range chunks.
  3. Poll POST /v2/post/publish/status/fetch/ until PUBLISH_COMPLETE.

Requires a TikTok developer app with the video.upload + video.publish scopes.
Direct Post shows publicly only after the app passes TikTok's audit; until
then posts land as private. Access tokens expire after ~24h - when the app's
client key/secret plus a refresh token are configured, the bot refreshes them
automatically and persists the new tokens. Setup: SETUP_TIKTOK.md.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from .config import DRY_RUN, logger

API_BASE = "https://open.tiktokapis.com"

PRIVACY_PUBLIC = "PUBLIC_TO_EVERYONE"
PRIVACY_FRIENDS = "MUTUAL_FOLLOW_FRIENDS"
PRIVACY_FOLLOWERS = "FOLLOWER_OF_CREATOR"
PRIVACY_SELF = "SELF_ONLY"
PRIVACY_LEVELS = (PRIVACY_PUBLIC, PRIVACY_FRIENDS, PRIVACY_FOLLOWERS, PRIVACY_SELF)

TITLE_MAX_LEN = 150
# 10 MB chunks; files smaller than one chunk upload in a single PUT.
CHUNK_SIZE = 10_000_000
STATUS_TIMEOUT_SEC = 600
STATUS_POLL_INTERVAL_SEC = 10
REQUEST_TIMEOUT_SEC = 60
UPLOAD_TIMEOUT_SEC = 300


class TikTokAPIError(RuntimeError):
    """A failed TikTok API call with the parsed server message."""

    def __init__(self, message: str, code: Any = None, status: Any = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _mask_token(token: str) -> str:
    token = str(token or "")
    return f"…{token[-4:]}" if len(token) > 8 else "(unset)"


class TikTokUploader:
    """Direct-post videos to exactly one TikTok account."""

    def __init__(
        self,
        open_id: str = "",
        access_token: str = "",
        refresh_token: str = "",
        client_key: str = "",
        client_secret: str = "",
        privacy_level: str = PRIVACY_PUBLIC,
        dry_run: Optional[bool] = None,
        api_base: Optional[str] = None,
        on_tokens_refreshed: Optional[Callable[[str, str, Any], None]] = None,
    ):
        self.open_id = str(open_id or "").strip()
        self.access_token = str(access_token or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        self.client_key = str(client_key or "").strip()
        self.client_secret = str(client_secret or "").strip()
        privacy = str(privacy_level or PRIVACY_PUBLIC).strip()
        self.privacy_level = privacy if privacy in PRIVACY_LEVELS else PRIVACY_PUBLIC
        self.dry_run = DRY_RUN if dry_run is None else bool(dry_run)
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.on_tokens_refreshed = on_tokens_refreshed
        self.last_error = ""

    # ------------------------------------------------------------------
    _AUTH_ERROR_CODES = frozenset(
        {
            "unauthorized",
            "invalid_token",
            "access_token_invalid",
            "token_expired",
        }
    )

    @staticmethod
    def _parse_api_response(path: str, response) -> dict:
        """Parse a standard TikTok {"error": ..., "data": ...} response."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = (body or {}).get("error") or {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
        if response.status_code == 401 or code in {
            "invalid_token",
            "access_token_invalid",
            "token_expired",
        }:
            raise TikTokAPIError(
                f"TikTok authorization failed ({code or response.status_code}): "
                f"{message or 'token expired or revoked'}",
                code=code or "unauthorized",
                status=response.status_code,
            )
        if code == "unaudited_client_can_only_post_to_private_accounts":
            # TikTok blocks the post outright when an unaudited app targets a
            # PUBLIC account. Spell out both ways forward, because the raw
            # message does not explain them.
            raise TikTokAPIError(
                "TikTok rejected the post: this app has not passed TikTok's "
                "audit, so it can only post to accounts that are set to "
                "PRIVATE. Either set the TikTok account to private in the "
                "TikTok app (Settings > Privacy > Private account) and post "
                "at SELF_ONLY, or submit the app for TikTok's audit to post "
                "publicly. See SETUP_TIKTOK.md.",
                code=code,
                status=response.status_code,
            )
        if response.status_code >= 400 or code.lower() != "ok":
            raise TikTokAPIError(
                f"TikTok API error ({code or response.status_code}): "
                f"{message or 'unknown error'}",
                code=code,
                status=response.status_code,
            )
        data = (body or {}).get("data") or {}
        return data if isinstance(data, dict) else {}

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.api_base}/{path.lstrip('/')}"
        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=REQUEST_TIMEOUT_SEC,
            )
        except Exception as exc:
            raise TikTokAPIError(f"Network error calling {path}: {exc}")
        return self._parse_api_response(path, response)

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.api_base}/{path.lstrip('/')}"
        try:
            response = requests.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=REQUEST_TIMEOUT_SEC,
            )
        except Exception as exc:
            raise TikTokAPIError(f"Network error calling {path}: {exc}")
        return self._parse_api_response(path, response)

    def _refresh_or_raise(self, exc: TikTokAPIError) -> None:
        """Refresh an expired token once, or re-raise with setup guidance."""
        logger.info("TikTok token rejected; attempting a refresh...")
        if not self.refresh_access_token():
            raise TikTokAPIError(
                f"{exc} (configure the app client key/secret + refresh "
                "token for auto-refresh, or paste a fresh access token)"
            )
        logger.info("TikTok token refreshed; retrying the request.")

    def _post_with_refresh(self, path: str, payload: dict) -> dict:
        try:
            return self._post(path, payload)
        except TikTokAPIError as exc:
            if exc.code not in self._AUTH_ERROR_CODES:
                raise
            self._refresh_or_raise(exc)
            return self._post(path, payload)

    def _get_with_refresh(self, path: str, params: dict) -> dict:
        try:
            return self._get(path, params)
        except TikTokAPIError as exc:
            if exc.code not in self._AUTH_ERROR_CODES:
                raise
            self._refresh_or_raise(exc)
            return self._get(path, params)

    # ------------------------------------------------------------------
    def refresh_access_token(self) -> bool:
        """Refresh the ~24h access token; persists via callback. False on failure."""
        if not (self.client_key and self.client_secret and self.refresh_token):
            self.last_error = "TikTok refresh needs client key/secret + refresh token."
            return False
        try:
            response = requests.post(
                f"{self.api_base}/v2/oauth/token/",
                data={
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cache-Control": "no-cache",
                },
                timeout=REQUEST_TIMEOUT_SEC,
            )
            body = response.json()
        except Exception as exc:
            self.last_error = f"TikTok token refresh failed: {exc}"
            logger.error("%s", self.last_error)
            return False
        if not isinstance(body, dict):
            body = {}
        # Success is a FLAT object (access_token at top level); failures
        # carry error/error_description/message instead. A nested data.*
        # shape is accepted too in case TikTok ever wraps it.
        nested = body.get("data")
        data = nested if isinstance(nested, dict) else body
        new_access = str(data.get("access_token") or "").strip()
        # TikTok may rotate the refresh token: always keep the returned one.
        new_refresh = str(data.get("refresh_token") or "").strip() or self.refresh_token
        if response.status_code >= 400 or not new_access:
            detail = (
                data.get("error_description")
                or data.get("message")
                or data.get("error")
                or body.get("error_description")
                or body.get("message")
                or body.get("error")
                or f"HTTP {response.status_code}"
            )
            self.last_error = f"TikTok token refresh rejected: {detail}"
            logger.error("%s", self.last_error)
            return False
        self.access_token = new_access
        self.refresh_token = new_refresh
        if callable(self.on_tokens_refreshed):
            try:
                self.on_tokens_refreshed(new_access, new_refresh, data.get("expires_in"))
            except Exception as exc:
                logger.warning("Could not persist refreshed TikTok tokens: %s", exc)
        return True

    # ------------------------------------------------------------------
    def check_connection(self) -> tuple[bool, str]:
        """Read-only check: token works and belongs to the configured open_id."""
        if not self.open_id:
            return False, "TikTok open_id is not configured for this account."
        if not self.access_token:
            return False, "TikTok access token is not configured for this account."
        try:
            # User info is a GET endpoint with query params (not a POST).
            data = self._get_with_refresh(
                "/v2/user/info/",
                {"fields": "open_id,display_name"},
            )
        except TikTokAPIError as exc:
            self.last_error = str(exc)
            return False, str(exc)
        user = data.get("user") or data
        actual = str(user.get("open_id") or "").strip()
        if actual and actual != self.open_id:
            return False, (
                "Token belongs to a different TikTok user "
                f"(expected {self.open_id[:8]}…, got {actual[:8]}…). Reconnect it."
            )
        name = str(user.get("display_name") or "").strip()
        who = f"@{name}" if name else f"open_id {self.open_id[:8]}…"
        token_note = _mask_token(self.access_token)
        if not actual:
            return True, (
                f"TikTok token is valid ({who}, token {token_note}); "
                "open_id could not be verified from this response."
            )
        return True, f"TikTok check passed for {who} (token {token_note})."

    # ------------------------------------------------------------------
    def _effective_privacy(self) -> str:
        """Best-effort: fall back to an allowed privacy level for this creator."""
        try:
            data = self._post_with_refresh("/v2/post/publish/creator_info/query/", {})
            options = data.get("privacy_level_options") or []
            options = [str(item).strip() for item in options if str(item).strip()]
        except TikTokAPIError as exc:
            logger.warning("TikTok creator-info query failed; using %s: %s", self.privacy_level, exc)
            return self.privacy_level
        if options and self.privacy_level not in options:
            logger.warning(
                "TikTok privacy %s is not available to this creator; using %s instead.",
                self.privacy_level,
                options[0],
            )
            return options[0]
        if self.privacy_level != PRIVACY_SELF and options == [PRIVACY_SELF]:
            # Only SELF_ONLY on offer means the app has not been audited yet.
            logger.warning(
                "TikTok offers only SELF_ONLY for this creator: the app is "
                "unaudited, so posts will be PRIVATE (visible to the account "
                "owner only) until it passes TikTok's audit."
            )
        return self.privacy_level

    def upload_video(
        self,
        video_path: Path,
        title: str = "",
        video_url: str = "",
    ) -> str:
        """Direct-post one video; returns the TikTok publish_id.

        Uses PULL_FROM_URL when a public video_url is available, otherwise
        uploads the file bytes directly. Raises TikTokAPIError on failure.
        In dry-run mode nothing is sent and "DRY_RUN" is returned instead.
        """
        video_path = Path(video_path)
        title = " ".join(str(title or "").split())[:TITLE_MAX_LEN]
        video_url = str(video_url or "").strip()
        if self.dry_run:
            logger.info("[DRY-RUN] TikTok video prepared but not published.")
            return "DRY_RUN"
        if not self.open_id or not self.access_token:
            raise TikTokAPIError("TikTok open_id / access token is not configured.")

        file_size = 0
        if not video_url:
            if not video_path.is_file() or video_path.stat().st_size <= 0:
                raise TikTokAPIError(f"TikTok upload file is missing or empty: {video_path}")
            file_size = video_path.stat().st_size

        privacy = self._effective_privacy()
        post_info: dict[str, Any] = {
            "title": title,
            "privacy_level": privacy,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        }
        if video_url:
            source_info: dict[str, Any] = {
                "source": "PULL_FROM_URL",
                "video_url": video_url,
            }
            logger.info("TikTok: initializing a URL-pull post...")
        else:
            total_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))
            source_info = {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": min(file_size, CHUNK_SIZE),
                "total_chunk_count": total_chunks,
            }
            logger.info(
                "TikTok: initializing a file upload (%d bytes in %d chunk(s))...",
                file_size,
                total_chunks,
            )
        init = self._post_with_refresh(
            "/v2/post/publish/video/init/",
            {"post_info": post_info, "source_info": source_info},
        )
        publish_id = str(init.get("publish_id") or "").strip()
        if not publish_id:
            raise TikTokAPIError("TikTok returned no publish_id.")
        if not video_url:
            upload_url = str(init.get("upload_url") or "").strip()
            if not upload_url:
                raise TikTokAPIError("TikTok returned no upload_url.")
            self._upload_chunks(upload_url, video_path, file_size)
        self._wait_for_publish(publish_id)
        logger.info("TikTok video published: publish_id %s", publish_id)
        return publish_id

    def _upload_chunks(self, upload_url: str, video_path: Path, file_size: int) -> None:
        # The upload_url is pre-signed: no Authorization header is sent here.
        with open(video_path, "rb") as handle:
            offset = 0
            index = 0
            while offset < file_size:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                start = offset
                end = offset + len(chunk) - 1
                try:
                    response = requests.put(
                        upload_url,
                        data=chunk,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{file_size}",
                        },
                        timeout=UPLOAD_TIMEOUT_SEC,
                    )
                except Exception as exc:
                    raise TikTokAPIError(f"TikTok chunk upload failed: {exc}")
                if response.status_code >= 400:
                    raise TikTokAPIError(
                        f"TikTok chunk upload rejected (HTTP {response.status_code})."
                    )
                offset += len(chunk)
                index += 1
                logger.info("TikTok upload progress: chunk %d (%d%%).", index, int(offset * 100 / file_size))

    def _wait_for_publish(self, publish_id: str) -> None:
        deadline = time.monotonic() + STATUS_TIMEOUT_SEC
        while True:
            data = self._post_with_refresh(
                "/v2/post/publish/status/fetch/",
                {"publish_id": publish_id},
            )
            status = str(data.get("status") or "").strip().upper()
            if status == "PUBLISH_COMPLETE":
                return
            if status == "SEND_TO_USER_INBOX":
                # Draft/inbox mode (not direct post): the upload reached TikTok.
                logger.info("TikTok delivered the video to the creator inbox.")
                return
            if status == "FAILED":
                raise TikTokAPIError(
                    f"TikTok could not publish the video: {data.get('fail_reason') or 'unknown reason'}"
                )
            if time.monotonic() >= deadline:
                raise TikTokAPIError(
                    f"Timed out waiting for TikTok to publish (last status: {status or 'unknown'})."
                )
            if status:
                logger.info("TikTok publish status: %s; waiting...", status)
            time.sleep(STATUS_POLL_INTERVAL_SEC)
