"""YouTube OAuth, destination safety checks, metadata, and real uploads."""
from __future__ import annotations

import re
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .config import (
    DRY_RUN,
    MAX_DAILY_UPLOADS,
    VIDEO_LANGUAGE,
    YOUTUBE_API_SERVICE_NAME,
    YOUTUBE_API_VERSION,
    YOUTUBE_CLIENT_SECRET_FILE,
    YOUTUBE_SCOPES,
    YOUTUBE_TOKEN_FILE,
    logger,
)
from .models import StateDB

UPLOAD_QUOTA_REACHED = "QUOTA_LIMIT_REACHED"
UPLOAD_DRY_RUN = "DRY_RUN"
UPLOAD_AUTH_REQUIRED = "AUTH_REQUIRED"
UPLOAD_CHANNEL_MISMATCH = "CHANNEL_MISMATCH"
NON_UPLOAD_RESULTS = frozenset(
    {
        UPLOAD_QUOTA_REACHED,
        UPLOAD_DRY_RUN,
        UPLOAD_AUTH_REQUIRED,
        UPLOAD_CHANNEL_MISMATCH,
    }
)


def is_real_upload_id(value: Optional[str]) -> bool:
    return bool(value and value not in NON_UPLOAD_RESULTS)


class YouTubeUploader:
    """Authenticate and upload to exactly the destination account requested."""

    def __init__(
        self,
        client_secret_file: Path = YOUTUBE_CLIENT_SECRET_FILE,
        token_file: Path = YOUTUBE_TOKEN_FILE,
        state_db: Optional[StateDB] = None,
        dry_run: Optional[bool] = None,
    ):
        self.client_secret_file = Path(client_secret_file)
        self.token_file = Path(token_file)
        self.state_db = state_db if state_db else StateDB()
        self.dry_run = DRY_RUN if dry_run is None else bool(dry_run)
        self.youtube_service: Optional[Resource] = None
        self.last_auth_error = ""
        self.last_metadata: Optional[dict[str, Any]] = None

    @staticmethod
    def _run_auth_flow(flow) -> Credentials:
        """
        Run a state-checked local OAuth callback with a real five-minute limit.

        oauthlib >= 3.2 rejects ANY non-HTTPS callback with
        'OAuth 2 MUST utilize https' - including the standard loopback redirect
        (http://localhost:PORT/) that Google's installed-app flow uses. Like
        Google's own run_local_server, we present the callback to oauthlib as
        https://localhost (nothing is fetched from it - only the ?code/state
        params are parsed, then the code is exchanged with Google over the real
        HTTPS token endpoint).
        """
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        flow.redirect_uri = f"http://localhost:{port}/"
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent select_account",
        )
        logger.info("Opening a browser for Google authorization...")
        # Keep the full one-time URL out of rotating log files. It may be copied
        # from the process console when automatic browser opening is unavailable.
        print("\nGoogle authorization URL (valid for this connection attempt):")
        print(auth_url)
        print()
        try:
            webbrowser.open(auth_url)
        except Exception as exc:
            logger.warning("Could not open the browser automatically: %s", exc)

        result: dict[str, Optional[str]] = {"authorization_response": None, "error": None}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                params = parse_qs(urlparse(self.path).query)
                result["error"] = params.get("error", [None])[0]
                # https here: oauthlib refuses to parse a plain-http callback
                # URI, even for loopback. Same approach as google_auth_oauthlib.
                result["authorization_response"] = f"https://localhost:{port}{self.path}"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h3>Authorization received. You may close this tab.</h3></body></html>"
                )

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", port), Handler)
        server.timeout = 1.0
        deadline = time.monotonic() + 300
        try:
            while not result["authorization_response"] and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()

        if result["error"]:
            raise RuntimeError(f"Google authorization was denied: {result['error']}")
        response_url = result["authorization_response"]
        if not response_url:
            raise RuntimeError("OAuth flow timed out after five minutes")
        # OAuthlib validates the returned state when given the complete response.
        flow.fetch_token(authorization_response=response_url)
        return flow.credentials

    def _get_authenticated_service(self, interactive: bool = True) -> Optional[Resource]:
        if self.youtube_service:
            return self.youtube_service
        self.last_auth_error = ""
        logger.info("OAuth token path for this account: %s", self.token_file)

        creds: Optional[Credentials] = None
        if self.token_file.is_file():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.token_file), YOUTUBE_SCOPES
                )
            except Exception as exc:
                self.last_auth_error = f"Could not read OAuth token: {exc}"
                logger.error("%s", self.last_auth_error)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    logger.info("Refreshing expired YouTube OAuth token...")
                    creds.refresh(Request())
                except Exception as exc:
                    self.last_auth_error = f"OAuth token refresh failed: {exc}"
                    logger.error("%s", self.last_auth_error)
                    creds = None

            if not creds:
                if not self.client_secret_file.is_file():
                    self.last_auth_error = (
                        f"OAuth client secret is missing for this account: "
                        f"{self.client_secret_file}"
                    )
                    logger.error("%s", self.last_auth_error)
                    return None
                if not interactive:
                    self.last_auth_error = "Interactive Google authorization is required"
                    return None
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(self.client_secret_file), YOUTUBE_SCOPES
                    )
                    creds = self._run_auth_flow(flow)
                except Exception as exc:
                    self.last_auth_error = f"OAuth authorization failed: {exc}"
                    logger.error("%s", self.last_auth_error)
                    return None

            try:
                self.token_file.parent.mkdir(parents=True, exist_ok=True)
                self.token_file.write_text(creds.to_json(), encoding="utf-8")
                logger.info("Saved refreshed OAuth token for this account.")
            except Exception as exc:
                logger.warning("Could not save OAuth token: %s", exc)

        try:
            self.youtube_service = build(
                YOUTUBE_API_SERVICE_NAME,
                YOUTUBE_API_VERSION,
                credentials=creds,
                cache_discovery=False,
            )
            logger.info("Connected to YouTube Data API v3.")
            return self.youtube_service
        except Exception as exc:
            self.last_auth_error = f"Could not build YouTube API client: {exc}"
            logger.error("%s", self.last_auth_error)
            return None

    @staticmethod
    def _normalize_channel_title(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    @classmethod
    def _verify_channel(
        cls,
        service,
        expected: str = "",
        expected_channel_id: str = "",
    ) -> tuple[bool, str]:
        """Fail closed unless the destination ID or exact title matches."""
        try:
            response = service.channels().list(part="id,snippet", mine=True).execute()
            items = response.get("items") or []
            actual_titles = [
                str(item.get("snippet", {}).get("title", "")) for item in items
            ]
            actual = ", ".join(title for title in actual_titles if title) or "(no channel)"
            expected_id = str(expected_channel_id or "").strip()
            if expected_id:
                return any(str(item.get("id") or "") == expected_id for item in items), actual
            normalized_expected = cls._normalize_channel_title(expected)
            if not normalized_expected:
                return False, actual
            return any(
                cls._normalize_channel_title(title) == normalized_expected
                for title in actual_titles
            ), actual
        except Exception as exc:
            logger.error("Destination channel verification failed; upload blocked: %s", exc)
            return False, "(verification unavailable)"

    @staticmethod
    def _clean_channel_tag(channel_name: str) -> str:
        """Extract a useful handle, never the generic /shorts or /videos suffix."""
        raw = str(channel_name or "").strip()
        if not raw:
            return ""
        if raw.lower().startswith(("http://", "https://")):
            parts = [part for part in urlparse(raw).path.split("/") if part]
            while parts and parts[-1].lower() in {"shorts", "videos", "featured", "streams"}:
                parts.pop()
            raw = parts[-1] if parts else ""
        raw = raw.lstrip("@").strip()
        if not raw or len(raw) > 40 or "/" in raw or raw.lower().startswith("http"):
            return ""
        return raw

    # Map common ISO 639-2/B (3-letter codes Whisper may return) to the 2-letter
    # codes the YouTube API accepts in snippet.defaultLanguage.
    _LANGUAGE_3_TO_2 = {
        "eng": "en", "vie": "vi", "urd": "ur", "hin": "hi", "spa": "es",
        "por": "pt", "fra": "fr", "deu": "de", "rus": "ru", "ara": "ar",
        "tam": "ta", "tel": "te", "ben": "bn", "mar": "mr", "pan": "pa",
        "jpn": "ja", "kor": "ko", "zho": "zh", "ita": "it", "tur": "tr",
        "ind": "id", "msa": "ms", "nld": "nl", "pol": "pl", "ukr": "uk",
        "pus": "ps", "snd": "sd", "nep": "ne", "fas": "fa", "swa": "sw",
    }

    @classmethod
    def _normalize_language_code(cls, value: str) -> str:
        """
        Return a safe 2-letter ISO 639-1 code ('en', 'vi', 'ur', ...) suitable
        for YouTube's defaultLanguage/defaultAudioLanguage, or '' if unknown.
        """
        code = re.sub(r"[^A-Za-z]", "", str(value or ""))[:3].lower()
        if re.fullmatch(r"[a-z]{2}", code):
            return code
        if re.fullmatch(r"[a-z]{3}", code):
            return cls._LANGUAGE_3_TO_2.get(code, "")
        return ""

    @classmethod
    def resolve_content_language(cls, detected: str) -> str:
        """
        Decide the language tag applied to the upload (never the audio itself):
          VIDEO_LANGUAGE=auto -> detected whisper language
          VIDEO_LANGUAGE=en/vi/... -> forced code
          VIDEO_LANGUAGE=off  -> no tag
        """
        mode = str(VIDEO_LANGUAGE or "auto").strip().lower()
        if mode in ("auto", "", "detect"):
            return cls._normalize_language_code(detected)
        if mode in ("off", "none", "false", "0"):
            return ""
        return cls._normalize_language_code(mode)

    @staticmethod
    def generate_short_metadata(
        original_title: str,
        original_url: str = "",
        channel_name: str = "",
        part_label: Optional[str] = None,
        info: Optional[dict[str, Any]] = None,
        transcript_text: str = "",
        extra_hashtags: str = "",
        title_prefix: Optional[str] = None,
        title_hashtags: str = "",
        smart_titles: Optional[bool] = None,
        content_language: str = "",
    ) -> dict[str, Any]:
        from .hashtags import build_hashtags, make_catchy_title, make_description

        info = info or {"title": original_title}
        title = make_catchy_title(
            info=info,
            transcript_text=transcript_text,
            extra_hashtags=extra_hashtags,
            part_label=part_label or "",
            title_prefix=title_prefix,
            title_hashtags=title_hashtags,
            smart_titles=smart_titles,
        )
        final_tags = build_hashtags(
            info=info,
            transcript_text=transcript_text,
            extra_hashtags=extra_hashtags,
            smart_titles=smart_titles,
            title_hashtags=title_hashtags,
        )
        tags = [tag.lstrip("#") for tag in final_tags]
        channel_tag = YouTubeUploader._clean_channel_tag(channel_name)
        if channel_tag:
            tags.append(channel_tag)

        seen: set[str] = set()
        capped: list[str] = []
        for tag in tags:
            clean = str(tag).strip()
            key = clean.casefold()
            if not clean or key in seen:
                continue
            proposed = capped + [clean]
            if len(",".join(proposed).encode("utf-8")) > 480:
                break
            seen.add(key)
            capped.append(clean)

        return {
            "title": title,
            "description": make_description(original_title, original_url, final_tags),
            "tags": capped,
            "categoryId": "24",
            "content_language": YouTubeUploader.resolve_content_language(content_language),
        }

    def upload_short(
        self,
        video_path: Path,
        original_video_id: str,
        original_title: str,
        original_url: str = "",
        channel_name: str = "",
        part_label: Optional[str] = None,
        account: str = "",
        account_max_daily: Optional[int] = None,
        info: Optional[dict[str, Any]] = None,
        transcript_text: str = "",
        extra_hashtags: str = "",
        title_prefix: Optional[str] = None,
        title_hashtags: str = "",
        smart_titles: Optional[bool] = None,
        expected_channel: Optional[str] = None,
        expected_channel_id: Optional[str] = None,
        content_language: str = "",
    ) -> Optional[str]:
        """Upload once. Non-upload sentinels never consume quota or mark success."""
        video_path = Path(video_path)
        self.last_metadata = self.generate_short_metadata(
            original_title=original_title,
            original_url=original_url,
            channel_name=channel_name,
            part_label=part_label,
            info=info,
            transcript_text=transcript_text,
            extra_hashtags=extra_hashtags,
            title_prefix=title_prefix,
            title_hashtags=title_hashtags,
            smart_titles=smart_titles,
            content_language=content_language,
        )

        if not video_path.is_file() or video_path.stat().st_size <= 0:
            logger.error("YouTube upload file is missing or empty: %s", video_path)
            return None
        if self.dry_run:
            logger.info(
                "[DRY-RUN] Prepared but did not upload '%s' (%s).",
                self.last_metadata["title"],
                video_path.name,
            )
            return UPLOAD_DRY_RUN

        service = self._get_authenticated_service()
        if not service:
            logger.error(
                "YouTube upload not attempted because authentication is unavailable. "
                "The file remains queued locally; no quota was consumed."
            )
            return UPLOAD_AUTH_REQUIRED

        # A safety lock is mandatory. Newly connected accounts get these fields
        # automatically; legacy accounts must connect once or configure one.
        if not (str(expected_channel or "").strip() or str(expected_channel_id or "").strip()):
            logger.error(
                "Destination channel safety lock is not configured for account '%s'; "
                "upload blocked. Connect/Test this account in the panel first.",
                account,
            )
            return UPLOAD_CHANNEL_MISMATCH
        channel_ok, actual = self._verify_channel(
            service,
            expected=str(expected_channel or ""),
            expected_channel_id=str(expected_channel_id or ""),
        )
        if not channel_ok:
            logger.error(
                "CHANNEL SAFETY LOCK: account '%s' is connected to '%s', expected '%s'. "
                "Upload blocked.",
                account,
                actual,
                expected_channel_id or expected_channel,
            )
            return UPLOAD_CHANNEL_MISMATCH

        max_daily = int(account_max_daily or MAX_DAILY_UPLOADS)
        reservation, remaining_after = self.state_db.reserve_upload_slot(
            max_daily_uploads=max_daily, account=account
        )
        if not reservation:
            logger.warning(
                "Upload cap (%s/24h) reached for '%s'. The Short remains queued.",
                max_daily,
                account,
            )
            return UPLOAD_QUOTA_REACHED
        logger.info(
            "Reserved an upload slot for '%s' (%s slots remain after this upload).",
            account,
            remaining_after,
        )

        body = {
            "snippet": {
                "title": self.last_metadata["title"],
                "description": self.last_metadata["description"],
                "tags": self.last_metadata["tags"],
                "categoryId": self.last_metadata["categoryId"],
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        content_language = str(self.last_metadata.get("content_language") or "")
        if content_language:
            # Tag the CONTENT LANGUAGE so YouTube labels it correctly. This does
            # not modify the audio - the source audio is uploaded untouched.
            body["snippet"]["defaultLanguage"] = content_language
            body["snippet"]["defaultAudioLanguage"] = content_language
            logger.info(
                "Upload content language: '%s' (auto-detected from source audio; "
                "audio was NOT dubbed or replaced).",
                content_language,
            )
        try:
            media = MediaFileUpload(
                str(video_path),
                mimetype="video/mp4",
                resumable=True,
                chunksize=5 * 1024 * 1024,
            )
            request = service.videos().insert(
                part="snippet,status", body=body, media_body=media
            )
            response = None
            while response is None:
                status, response = request.next_chunk(num_retries=3)
                if status:
                    logger.info("YouTube upload progress: %s%%", int(status.progress() * 100))
            short_id = str((response or {}).get("id") or "").strip()
            if not short_id:
                raise RuntimeError("YouTube returned no video ID after upload")
            self.state_db.record_upload(
                original_video_id,
                short_id,
                account=account,
                reservation_id=reservation,
            )
            logger.info("YouTube upload complete: https://www.youtube.com/shorts/%s", short_id)
            return short_id
        except HttpError as exc:
            self.state_db.release_upload_reservation(reservation)
            content = exc.content.decode("utf-8", errors="replace") if isinstance(exc.content, bytes) else str(exc.content)
            quota_reason = any(
                reason in content
                for reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded")
            )
            if exc.resp.status == 429 or quota_reason:
                logger.error("YouTube quota/rate limit reached; upload remains queued: %s", exc)
                return UPLOAD_QUOTA_REACHED
            logger.error("YouTube rejected the upload (HTTP %s): %s", exc.resp.status, exc)
            return None
        except Exception as exc:
            self.state_db.release_upload_reservation(reservation)
            logger.error("YouTube upload failed; file remains queued: %s", exc)
            return None


def resolve_credentials(acc):
    """Return portable, account-isolated client-secret and token paths."""
    from .config import BASE_DIR, YOUTUBE_CLIENT_SECRET_FILE, YOUTUBE_TOKEN_FILE
    from .pathutils import credential_path

    if not acc:
        return YOUTUBE_CLIENT_SECRET_FILE, YOUTUBE_TOKEN_FILE
    name = str(acc.get("name") or "").strip()
    if not name:
        return YOUTUBE_CLIENT_SECRET_FILE, YOUTUBE_TOKEN_FILE
    return (
        credential_path(
            BASE_DIR, name, acc.get("client_secret"), "client_secret.json"
        ),
        credential_path(BASE_DIR, name, acc.get("token"), "token.json"),
    )
