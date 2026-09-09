"""Instagram Reels publishing through the official Meta Graph API.

Flow (create container, wait for processing, publish):
  1. POST /{ig-user-id}/media with media_type=REELS, video_url, caption
     -> returns a media container id.
  2. Poll GET /{container-id}?fields=status_code until FINISHED.
  3. POST /{ig-user-id}/media_publish with creation_id -> media id.

Requires an Instagram Professional (Business or Creator) account linked to a
Facebook Page, plus an app token with the instagram_content_publish
permission. The video MUST be reachable at a public https URL - the bot uses
the R2 public URL (see R2_PUBLIC_BASE_URL). Setup: SETUP_INSTAGRAM_TIKTOK.md.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

import requests

from .config import DRY_RUN, logger

GRAPH_API_VERSION = os.getenv("INSTAGRAM_GRAPH_API_VERSION", "v25.0").strip() or "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Container processing usually takes 10-60s; Meta fetches the video first.
CONTAINER_TIMEOUT_SEC = 300
CONTAINER_POLL_INTERVAL_SEC = 5
REQUEST_TIMEOUT_SEC = 30


class InstagramAPIError(RuntimeError):
    """A failed Meta Graph API call with the parsed server message."""

    def __init__(self, message: str, code: Any = None, status: Any = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _mask_token(token: str) -> str:
    """Last 4 chars for logs; tokens must never appear in full in logs."""
    token = str(token or "")
    return f"…{token[-4:]}" if len(token) > 8 else "(unset)"


def _meta_error_message(payload: Any) -> tuple[str, Any]:
    try:
        error = (payload or {}).get("error") or {}
        message = str(error.get("message") or "Unknown Meta API error")
        return message, error.get("code")
    except Exception:
        return "Unknown Meta API error", None


class InstagramReelsUploader:
    """Publish Reels to exactly one Instagram Professional account."""

    def __init__(
        self,
        ig_user_id: str = "",
        access_token: str = "",
        app_id: str = "",
        app_secret: str = "",
        dry_run: Optional[bool] = None,
        api_base: Optional[str] = None,
        on_token_refreshed=None,
    ):
        self.ig_user_id = str(ig_user_id or "").strip()
        self.access_token = str(access_token or "").strip()
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.dry_run = DRY_RUN if dry_run is None else bool(dry_run)
        self.api_base = (api_base or GRAPH_API_BASE).rstrip("/")
        self.on_token_refreshed = on_token_refreshed
        self.last_error = ""

    # ------------------------------------------------------------------
    def _auth_params(self, extra: Optional[dict] = None) -> dict:
        params = dict(extra or {})
        params["access_token"] = self.access_token
        return params

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """One Graph call; raises InstagramAPIError with Meta's message."""
        url = f"{self.api_base}/{path.lstrip('/')}"
        # Never log the query string: it carries the access token.
        safe_path = path.split("?")[0]
        try:
            response = requests.request(
                method, url, timeout=REQUEST_TIMEOUT_SEC, **kwargs
            )
        except Exception as exc:
            raise InstagramAPIError(f"Network error calling {safe_path}: {exc}")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            message, code = _meta_error_message(payload)
            raise InstagramAPIError(
                f"Meta API error ({response.status_code}): {message}",
                code=code,
                status=response.status_code,
            )
        return payload if isinstance(payload, dict) else {}

    def _call_with_refresh(self, method: str, path: str, **kwargs) -> dict:
        """Run one call, refreshing an expired long-lived token once."""
        try:
            return self._request(method, path, **kwargs)
        except InstagramAPIError as exc:
            if not self._looks_like_expired_token(exc):
                raise
            if not (self.app_id and self.app_secret):
                raise InstagramAPIError(
                    f"{exc} (token expired; configure the Meta app id/secret "
                    "for auto-refresh or paste a fresh token)"
                )
            logger.info("Instagram token expired; attempting a refresh...")
            refreshed = self.refresh_long_lived_token()
            if not refreshed:
                raise
            logger.info("Instagram token refreshed; retrying the request.")
            return self._request(method, path, **kwargs)

    @staticmethod
    def _looks_like_expired_token(exc: InstagramAPIError) -> bool:
        text = str(exc).lower()
        return exc.code == 190 or exc.status in (401, 403) and any(
            marker in text
            for marker in ("expired", "invalid token", "invalid oauth", "session")
        )

    # ------------------------------------------------------------------
    def refresh_long_lived_token(self) -> Optional[str]:
        """Exchange the token for a fresh ~60-day one; None on failure."""
        if not (self.app_id and self.app_secret and self.access_token):
            return None
        try:
            payload = self._request(
                "GET",
                "oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "fb_exchange_token": self.access_token,
                },
            )
        except InstagramAPIError as exc:
            self.last_error = str(exc)
            logger.error("Instagram token refresh failed: %s", exc)
            return None
        new_token = str(payload.get("access_token") or "").strip()
        if not new_token:
            self.last_error = "Token refresh returned no access_token"
            logger.error("Instagram token refresh failed: empty response.")
            return None
        self.access_token = new_token
        if callable(self.on_token_refreshed):
            try:
                self.on_token_refreshed(new_token)
            except Exception as exc:
                logger.warning("Could not persist refreshed Instagram token: %s", exc)
        return new_token

    # ------------------------------------------------------------------
    def check_connection(self) -> tuple[bool, str]:
        """Read-only check: token works and points at the configured account."""
        if not self.ig_user_id:
            return False, "Instagram user ID is not configured for this account."
        if not self.access_token:
            return False, "Instagram access token is not configured for this account."
        try:
            payload = self._call_with_refresh(
                "GET",
                self.ig_user_id,
                params=self._auth_params({"fields": "id,username,name"}),
            )
        except InstagramAPIError as exc:
            self.last_error = str(exc)
            return False, str(exc)
        username = str(payload.get("username") or "").strip()
        who = f"@{username}" if username else f"id {payload.get('id')}"
        return True, f"Instagram check passed for {who} (token {_mask_token(self.access_token)})."

    def publish_reel(self, video_url: str, caption: str = "") -> str:
        """Publish one Reel; returns the Instagram media id.

        Raises InstagramAPIError on failure. In dry-run mode nothing is sent
        and the sentinel "DRY_RUN" is returned instead.
        """
        video_url = str(video_url or "").strip()
        caption = str(caption or "")
        if self.dry_run:
            logger.info("[DRY-RUN] Instagram Reel prepared but not published.")
            return "DRY_RUN"
        if not self.ig_user_id or not self.access_token:
            raise InstagramAPIError("Instagram user ID / access token is not configured.")
        if not video_url.startswith("https://"):
            raise InstagramAPIError(
                "Instagram needs a public https video URL (configure R2_PUBLIC_BASE_URL)."
            )

        container = self._call_with_refresh(
            "POST",
            f"{self.ig_user_id}/media",
            params=self._auth_params(
                {
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption[:2200],
                    "share_to_feed": "true",
                }
            ),
        )
        container_id = str(container.get("id") or "").strip()
        if not container_id:
            raise InstagramAPIError("Meta returned no media container id.")
        logger.info("Instagram container %s created; waiting for processing...", container_id)
        self._wait_for_container(container_id)

        published = self._call_with_refresh(
            "POST",
            f"{self.ig_user_id}/media_publish",
            params=self._auth_params({"creation_id": container_id}),
        )
        media_id = str(published.get("id") or "").strip()
        if not media_id:
            raise InstagramAPIError("Meta accepted the Reel but returned no media id.")
        logger.info("Instagram Reel published: media id %s", media_id)
        return media_id

    def _wait_for_container(self, container_id: str) -> None:
        deadline = time.monotonic() + CONTAINER_TIMEOUT_SEC
        attempts = 0
        while True:
            attempts += 1
            payload = self._call_with_refresh(
                "GET",
                container_id,
                params=self._auth_params({"fields": "status_code,status"}),
            )
            status = str(payload.get("status_code") or "").strip().upper()
            if status == "FINISHED":
                return
            if status == "ERROR":
                detail = str(payload.get("status") or "processing failed")
                raise InstagramAPIError(f"Meta could not process the video: {detail}")
            if time.monotonic() >= deadline:
                raise InstagramAPIError(
                    "Timed out waiting for Meta to process the video "
                    f"(last status: {status or 'unknown'})."
                )
            if attempts == 1:
                logger.info("Instagram is fetching/processing the video...")
            time.sleep(CONTAINER_POLL_INTERVAL_SEC)
