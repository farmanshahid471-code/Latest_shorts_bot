"""Bilibili posting through the official Open Platform (开放平台) API.

Upload flow (server-side 视频稿件投递):
  1. POST /arcopen/fn/archive/video/init -> upload_token. ``utype`` is "1" for
     files <= 100 MB (single-shot upload) and "0" for bigger ones (chunked).
  2a. Small files: POST /video/v2/upload?upload_token=... with the raw bytes.
  2b. Big files:   POST /video/v2/part/upload?upload_token=...&part_number=N
      in 8 MB chunks, then POST /arcopen/fn/archive/video/complete to merge.
  3. Optional cover: POST /arcopen/fn/archive/cover/upload (multipart) -> URL.
  4. POST /arcopen/fn/archive/add-by-utoken -> resource_id (the BV number).

Every ``member.bilibili.com`` call carries the platform's signed headers: the
``x-bili-*`` set is sorted, joined with newlines and signed with HMAC-SHA256
using the app secret, and the result goes into ``Authorization``. Access
tokens expire (~30 days) - with the client id/secret plus a refresh token the
bot renews them automatically and persists the new pair.

Submitted archives go through Bilibili's review queue, so a successful post
means "accepted for review", not "already public". Setup: SETUP_BILIBILI.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from .config import DRY_RUN, logger

API_BASE = "https://member.bilibili.com"
UPLOAD_BASE = "https://openupos.bilivideo.com"
OAUTH_URL = "https://api.bilibili.com/x/account-oauth2/v1/token"

# Platform limits: title < 80 chars, description < 250, all tags < 200.
TITLE_MAX_LEN = 80
DESC_MAX_LEN = 250
TAG_MAX_LEN = 200

# Files at or below this size use the single-shot upload (no merge step).
SMALL_FILE_MAX_BYTES = 100 * 1024 * 1024
# Bilibili recommends a fixed 8 MB chunk size for the multipart flow.
CHUNK_SIZE = 8 * 1024 * 1024

COPYRIGHT_ORIGINAL = 1
COPYRIGHT_REPOST = 2

# 默认分区: 21 = 日常 (life/daily). Overridable per account in the panel.
DEFAULT_TID = 21

REQUEST_TIMEOUT_SEC = 60
UPLOAD_TIMEOUT_SEC = 300

# Bilibili error codes that mean "the access token is dead, refresh it".
_AUTH_ERROR_CODES = frozenset({127001, 127000, 127011, 122007})


class BilibiliAPIError(RuntimeError):
    """A failed Bilibili API call with the parsed server message."""

    def __init__(self, message: str, code: Any = None, status: Any = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _mask_token(token: str) -> str:
    token = str(token or "")
    return f"…{token[-4:]}" if len(token) > 8 else "(unset)"


def build_tag_string(tags) -> str:
    """Comma-separated tag string within Bilibili's 200-character budget."""
    out: list[str] = []
    total = 0
    for raw in tags or []:
        tag = str(raw or "").strip().lstrip("#").replace(",", " ").strip()
        if not tag or tag in out:
            continue
        extra = len(tag) + (1 if out else 0)
        if total + extra > TAG_MAX_LEN:
            break
        out.append(tag)
        total += extra
    return ",".join(out)


class BilibiliUploader:
    """Publish video archives to exactly one Bilibili account."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        access_token: str = "",
        refresh_token: str = "",
        tid: Any = DEFAULT_TID,
        copyright_type: Any = COPYRIGHT_ORIGINAL,
        source: str = "",
        dry_run: Optional[bool] = None,
        api_base: Optional[str] = None,
        upload_base: Optional[str] = None,
        on_tokens_refreshed: Optional[Callable[[str, str, Any], None]] = None,
    ):
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.access_token = str(access_token or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        try:
            self.tid = int(tid or DEFAULT_TID)
        except (TypeError, ValueError):
            self.tid = DEFAULT_TID
        try:
            copyright_value = int(copyright_type or COPYRIGHT_ORIGINAL)
        except (TypeError, ValueError):
            copyright_value = COPYRIGHT_ORIGINAL
        self.copyright_type = (
            copyright_value if copyright_value in (COPYRIGHT_ORIGINAL, COPYRIGHT_REPOST)
            else COPYRIGHT_ORIGINAL
        )
        self.source = str(source or "").strip()
        self.dry_run = DRY_RUN if dry_run is None else bool(dry_run)
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.upload_base = (upload_base or UPLOAD_BASE).rstrip("/")
        self.on_tokens_refreshed = on_tokens_refreshed
        self.last_error = ""

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------
    def _signed_headers(self, body: bytes, content_type: str) -> dict:
        """Build the x-bili-* header set plus its HMAC-SHA256 signature.

        The signed string is every ``x-bili-`` header sorted by name and
        joined as ``name:value`` lines. ``x-bili-content-md5`` is the MD5 of
        the JSON body (the empty string for GET / multipart / empty bodies).
        """
        signed_headers = {
            "x-bili-accesskeyid": self.client_id,
            "x-bili-content-md5": hashlib.md5(body or b"").hexdigest(),
            "x-bili-signature-method": "HMAC-SHA256",
            "x-bili-signature-nonce": str(uuid.uuid4()),
            "x-bili-signature-version": "2.0",
            "x-bili-timestamp": str(int(time.time())),
        }
        payload = "\n".join(f"{key}:{signed_headers[key]}" for key in sorted(signed_headers))
        signature = hmac.new(
            self.client_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers = dict(signed_headers)
        headers["Authorization"] = signature
        headers["Accept"] = "application/json"
        headers["access-token"] = self.access_token
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_api_response(path: str, response) -> dict:
        """Parse a standard Bilibili {"code", "message", "data"} response."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        body = body or {}
        try:
            code = int(body.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        message = str(body.get("message") or "")
        if response.status_code == 401 or code in _AUTH_ERROR_CODES:
            raise BilibiliAPIError(
                f"Bilibili authorization failed ({code or response.status_code}): "
                f"{message or 'token expired or revoked'}",
                code=code,
                status=response.status_code,
            )
        if response.status_code >= 400 or code != 0:
            raise BilibiliAPIError(
                f"Bilibili API error ({code or response.status_code}) on {path}: "
                f"{message or 'unknown error'}",
                code=code,
                status=response.status_code,
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        files: Optional[dict] = None,
        params: Optional[dict] = None,
        retry_on_auth: bool = True,
    ) -> dict:
        """Signed request against member.bilibili.com, refreshing on 401."""
        url = f"{self.api_base}{path}"
        if files is not None:
            # Multipart bodies are excluded from the MD5 per the spec, and
            # requests must set its own boundary-carrying Content-Type.
            body, content_type = b"", ""
        elif payload is not None:
            body, content_type = (
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json",
            )
        else:
            body, content_type = b"", "application/json"
        headers = self._signed_headers(body, content_type)
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                data=body if files is None and payload is not None else None,
                files=files,
                params=params,
                timeout=UPLOAD_TIMEOUT_SEC if files else REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            raise BilibiliAPIError(f"Bilibili request to {path} failed: {exc}") from exc
        try:
            return self._parse_api_response(path, response)
        except BilibiliAPIError as exc:
            if retry_on_auth and exc.code in _AUTH_ERROR_CODES and self._refresh_access_token():
                return self._request(
                    method, path, payload, files, params, retry_on_auth=False
                )
            raise

    # ------------------------------------------------------------------
    def _refresh_access_token(self) -> bool:
        """Swap the refresh token for a fresh access token; persist on success."""
        if not (self.refresh_token and self.client_id and self.client_secret):
            logger.warning(
                "Bilibili token expired and no refresh token / client credentials "
                "are configured - paste a fresh token in the panel."
            )
            return False
        try:
            response = requests.post(
                OAUTH_URL,
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
                timeout=REQUEST_TIMEOUT_SEC,
            )
            body = response.json() or {}
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Bilibili token refresh failed: %s", exc)
            return False
        data = body.get("data") or {}
        access = str(data.get("access_token") or "").strip()
        refresh = str(data.get("refresh_token") or "").strip()
        try:
            code = int(body.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        if code != 0 or not access:
            logger.warning(
                "Bilibili token refresh rejected: %s", body.get("message") or body
            )
            return False
        self.access_token = access
        if refresh:
            self.refresh_token = refresh
        logger.info("Bilibili access token refreshed (%s).", _mask_token(access))
        if self.on_tokens_refreshed:
            try:
                self.on_tokens_refreshed(access, refresh or self.refresh_token,
                                         data.get("expires_in"))
            except Exception as exc:
                logger.debug("Bilibili token persist hook failed: %s", exc)
        return True

    # ------------------------------------------------------------------
    def check_connection(self) -> tuple[bool, str]:
        """Verify the credentials by reading the authorized account info."""
        if not (self.client_id and self.client_secret and self.access_token):
            return False, ("Bilibili needs a client id, client secret and access "
                           "token before it can post.")
        try:
            data = self._request("GET", "/arcopen/fn/user/account/info")
        except BilibiliAPIError as exc:
            self.last_error = str(exc)
            return False, str(exc)
        name = str(data.get("name") or data.get("openid") or "unknown")
        return True, f"Connected to Bilibili account '{name}'."

    # ------------------------------------------------------------------
    def _init_upload(self, filename: str, small: bool) -> str:
        data = self._request(
            "POST",
            "/arcopen/fn/archive/video/init",
            payload={"name": filename, "utype": "1" if small else "0"},
        )
        upload_token = str(data.get("upload_token") or "").strip()
        if not upload_token:
            raise BilibiliAPIError("Bilibili did not return an upload_token.")
        return upload_token

    def _upload_bytes(self, url: str, params: dict, chunk: bytes) -> None:
        try:
            response = requests.post(
                url,
                params=params,
                data=chunk,
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            raise BilibiliAPIError(f"Bilibili chunk upload failed: {exc}") from exc
        self._parse_api_response(url, response)

    def _upload_video_file(self, video_path: Path, upload_token: str, small: bool) -> None:
        if small:
            self._upload_bytes(
                f"{self.upload_base}/video/v2/upload",
                {"upload_token": upload_token},
                video_path.read_bytes(),
            )
            return
        part_number = 0
        with video_path.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                part_number += 1
                self._upload_bytes(
                    f"{self.upload_base}/video/v2/part/upload",
                    {"upload_token": upload_token, "part_number": part_number},
                    chunk,
                )
        logger.info("Bilibili: uploaded %d chunk(s); merging.", part_number)
        self._request(
            "POST",
            "/arcopen/fn/archive/video/complete",
            params={"upload_token": upload_token},
        )

    def upload_cover(self, cover_path: Optional[Path]) -> str:
        """Upload a cover image and return its URL ("" when unavailable)."""
        if not cover_path:
            return ""
        path = Path(cover_path)
        if not path.is_file():
            return ""
        try:
            with path.open("rb") as handle:
                data = self._request(
                    "POST",
                    "/arcopen/fn/archive/cover/upload",
                    files={"file": (path.name, handle, "image/jpeg")},
                )
        except BilibiliAPIError as exc:
            # A missing cover must not sink the whole submission.
            logger.warning("Bilibili cover upload failed (posting without): %s", exc)
            return ""
        return str(data.get("url") or "")

    # ------------------------------------------------------------------
    def upload_video(
        self,
        video_path: Path,
        title: str,
        description: str = "",
        tags=None,
        cover_path: Optional[Path] = None,
    ) -> str:
        """Run the full submit flow and return the archive's resource_id."""
        if not (self.client_id and self.client_secret and self.access_token):
            raise BilibiliAPIError(
                "Bilibili is enabled but the client id / secret / access token "
                "is missing."
            )
        path = Path(video_path) if video_path else None
        if not path or not path.is_file():
            raise BilibiliAPIError("Bilibili needs the rendered video file on disk.")
        clean_title = " ".join(str(title or "").split())[:TITLE_MAX_LEN] or "New Short"
        clean_desc = str(description or "").strip()[:DESC_MAX_LEN]
        tag_string = build_tag_string(tags)
        if self.dry_run:
            logger.info("[DRY-RUN] Bilibili archive prepared but not submitted.")
            return "dry-run"

        size = path.stat().st_size
        small = size <= SMALL_FILE_MAX_BYTES
        upload_token = self._init_upload(path.name, small)
        self._upload_video_file(path, upload_token, small)
        cover_url = self.upload_cover(cover_path)

        payload = {
            "title": clean_title,
            "cover": cover_url,
            "tid": self.tid,
            "tag": tag_string,
            "desc": clean_desc,
            "copyright": self.copyright_type,
            "no_reprint": 0,
        }
        if self.copyright_type == COPYRIGHT_REPOST and self.source:
            payload["source"] = self.source
        data = self._request(
            "POST",
            "/arcopen/fn/archive/add-by-utoken",
            payload=payload,
            params={"upload_token": upload_token},
        )
        resource_id = str(data.get("resource_id") or "").strip()
        if not resource_id:
            raise BilibiliAPIError("Bilibili accepted the upload but returned no resource_id.")
        return resource_id
