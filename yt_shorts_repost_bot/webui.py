"""
Regenerates yt_shorts_repost_bot/webui.py with a TAB-based multi-account UI:
- A tab strip at the top (like Chrome): one tab per account + a "+" tab to add.
- Each account tab contains ITS OWN: credentials (upload client_secret.json,
  connect/test, connected channel), account settings (prefix, hashtags,
  watermarks, quota, pacing, delete-after-upload), and source channels.
- Per-account quota + scheduling is already supported; each tab saves to its
  own account in accounts.json.
- All buttons are plain HTML forms (no-JS friendly). JS only for live refresh.
"""
import datetime
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode, urlparse

from flask import (
    Flask,
    Response,
    has_request_context,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
)

from .config import (
    logger, LOG_FILE, BGM_DIR, ACCOUNTS, ACCOUNTS_FILE, DRY_RUN,
    MAX_DAILY_UPLOADS, CYCLE_INTERVAL_HOURS, FILL_MODE,
    YOUTUBE_TOKEN_FILE, FFMPEG_PATH, KEEP_SHORTS_DIR, WEBUI_HOST, WEBUI_PORT,
    WEBUI_USERNAME, WEBUI_PASSWORD, WEBUI_SECRET_KEY, WEBUI_COOKIE_SECURE,
)
from .models import StateDB
from .storage import CloudStorageManager
from .scheduler import ShortsRepostScheduler
from .main import repost_one_url
from .pathutils import credential_path, relative_credential_value, safe_account_slug
from .runtime import PIPELINE_LOCK
from .timewindows import US_TIMEZONES, validate_posting_window
from .social_bilibili import DEFAULT_TID as BILIBILI_DEFAULT_TID


DEFAULT_CLIP_SECONDS = 0.0


def clamp_clip_duration(value, default: float = 0.0) -> float:
    """The repost bot reposts whole Shorts, so there is no clip length to
    clamp; kept so both panels share one code path."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(default)
from .social_tiktok import PRIVACY_LEVELS as TIKTOK_PRIVACY_LEVELS

_jobs: dict = {}
_scheduler_thread = None
_scheduler_instance = None
ALLOWED_BGM_EXT = {".mp3", ".wav", ".m4a", ".aac"}
MAX_BGM_BYTES = 25 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 2 * 1024 * 1024
MAX_COOKIE_BYTES = 5 * 1024 * 1024
_config_lock = threading.RLock()


def _write_accounts(accounts: list) -> None:
    """Atomically replace accounts.json so simultaneous tab saves cannot corrupt it."""
    payload = json.dumps({"accounts": _dedupe_accounts(accounts)}, indent=2)
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ACCOUNTS_FILE.with_name(
        f".{ACCOUNTS_FILE.name}.{secrets.token_hex(6)}.tmp"
    )
    with _config_lock:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, ACCOUNTS_FILE)


def _valid_account_name(name: str) -> bool:
    value = str(name or "").strip()
    return bool(value and len(value) <= 80 and value not in {".", ".."} and not any(c in value for c in "/\\\x00"))


def _spawn_job(name: str, fn) -> bool:
    with _config_lock:
        if name in _active_jobs():
            logger.warning("[webui] Job '%s' is already running.", name)
            return False

        def _run():
            try:
                fn()
            except Exception as exc:
                logger.exception("[webui] Job '%s' failed: %s", name, exc)

        thread = threading.Thread(target=_run, daemon=True, name=f"webui-{name}")
        _jobs[name] = thread
        thread.start()
        return True


def _active_jobs() -> list:
    return [n for n, t in _jobs.items() if t.is_alive()]


def _tail_log(lines: int = 120) -> list:
    if not LOG_FILE.exists():
        return ["(log file not created yet - run an action to see output)"]
    try:
        size = LOG_FILE.stat().st_size
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, size - 96 * 1024))
            return f.read().decode("utf-8", errors="replace").splitlines()[-lines:]
    except Exception:
        return ["(could not read log file)"]


def _is_placeholder(value) -> bool:
    v = str(value or "").strip().lower()
    if not v:
        return True
    return any(m in v for m in ("your_", "changeme", "replace_me", "your-cloudflare"))


def _shared_cookie_path() -> Path:
    """One ignored viewer-cookie file shared by both bot variants."""
    return Path(__file__).resolve().parent.parent / "cookies.txt"


def _valid_youtube_cookie_export(data: bytes) -> bool:
    """Accept Netscape exports that contain at least one YouTube auth cookie."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if "# Netscape HTTP Cookie File" not in text or ".youtube.com" not in text:
        return False
    auth_names = {"SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO"}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if (
            len(fields) >= 7
            and ".youtube.com" in fields[0]
            and (
                fields[5] in auth_names
                or (fields[5].startswith("__Secure-") and "SID" in fields[5])
            )
        ):
            return True
    return False


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _get_env_setting(key: str, default: str = "") -> str:
    try:
        m = re.search(rf"^{re.escape(key)}=(.*)$", (Path(__file__).resolve().parent / ".env").read_text(encoding="utf-8"), re.M)
        return m.group(1).strip().strip('"') if m else default
    except Exception:
        return default


def _write_env_changes(changes: dict) -> str:
    env_path = Path(__file__).resolve().parent / ".env"
    with _config_lock:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        for key, value in changes.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            value = str(value).replace("\r", "").replace("\n", "")
            found = False
            for index, line in enumerate(lines):
                if line.strip().startswith(key + "="):
                    lines[index] = f"{key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}")
        temporary = env_path.with_name(
            f".{env_path.name}.{secrets.token_hex(6)}.tmp"
        )
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, env_path)
    return str(env_path)


def _dedupe_accounts(accounts) -> list:
    """Drop entries whose (case-insensitive) name is already in the list.
    Two entries with the same name are THE SAME account - keeping both makes
    one tab's save appear to change both tabs."""
    seen, out = set(), []
    for a in accounts:
        k = str(a.get("name") or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


def _accounts_from_disk() -> list:
    try:
        if ACCOUNTS_FILE.exists():
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            accs = data.get("accounts", []) if isinstance(data, dict) else []
            if accs:
                uniq = _dedupe_accounts(accs)
                if len(uniq) != len(accs):
                    logger.warning("[webui] accounts.json contained duplicate names - keeping the first of each.")
                return uniq
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[webui] Could not read accounts.json: %s", exc)
    return ACCOUNTS


def _set_account_field(name: str, key: str, value) -> bool:
    """Update one field atomically without allowing path-like account names."""
    if not _valid_account_name(name):
        return False
    with _config_lock:
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) if ACCOUNTS_FILE.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        low = str(name).strip().casefold()
        account = next(
            (item for item in accounts if str(item.get("name") or "").strip().casefold() == low),
            None,
        )
        if account is None:
            account = {
                "name": str(name).strip(),
                "target_channels": [],
                "max_daily_uploads": 10,
                "enabled": True,
            }
            accounts.append(account)
        account[key] = value
        _write_accounts(accounts)
    return True


def _find_account(name=None):
    accounts = _accounts_from_disk()
    if name and str(name).strip():
        low = str(name).strip().lower()
        for a in accounts:
            if str(a.get("name") or "").strip().lower() == low:
                return a
        return None
    for a in accounts:
        if a.get("enabled", True):
            return a
    return accounts[0] if accounts else None


def _preflight_enabled_accounts(accounts: Optional[list[dict]] = None) -> tuple[bool, str]:
    """Force-refresh every runnable account before the scheduler can start."""
    if DRY_RUN:
        return True, "OAuth preflight skipped because explicit dry-run mode is active."

    from .uploader import YouTubeUploader, resolve_credentials

    selected = accounts if accounts is not None else _accounts_from_disk()
    runnable = [
        account
        for account in selected
        if account.get("enabled", True)
        and bool(_clean_channels(account.get("target_channels")))
    ]
    if not runnable:
        return True, "No enabled account with source channels needs an OAuth check yet."

    passed: list[str] = []
    failures: list[str] = []
    for account in runnable:
        name = str(account.get("name") or "default").strip()
        client_secret, token = resolve_credentials(account)
        uploader = YouTubeUploader(
            client_secret_file=client_secret,
            token_file=token,
            state_db=StateDB(),
        )
        ok, detail = uploader.check_connection(
            expected_channel=str(
                account.get("expected_channel")
                or account.get("connected_channel")
                or ""
            ).strip(),
            expected_channel_id=str(account.get("connected_channel_id") or "").strip(),
        )
        if ok:
            passed.append(name)
            logger.info("[webui] OAuth preflight passed for '%s': %s", name, detail)
        else:
            failures.append(f"{name}: {detail}")
            logger.error("[webui] OAuth preflight failed for '%s': %s", name, detail)

    if failures:
        return False, "OAuth check failed — scheduler not started. " + " | ".join(failures)
    return True, "OAuth check passed for: " + ", ".join(passed)


def _account_state(a: dict, db: StateDB) -> dict:
    name = a.get("name", "default")
    tk = credential_path(
        Path(__file__).resolve().parent,
        name,
        a.get("token"),
        "token.json",
    )
    return {
        "name": name,
        "enabled": bool(a.get("enabled", True)),
        "channels": a.get("target_channels") or [],
        "max_daily": int(a.get("max_daily_uploads") or 10),
        "uploads": db.get_uploads_in_last_24_hours(account=name),
        "process_mode": a.get("process_mode", ""),
        "selection_order": a.get("selection_order", ""),
        "connected_channel": a.get("connected_channel", ""),
        "connected": tk.is_file(),
        "title_prefix": a.get("title_prefix", ""),
        "title_hashtags": a.get("title_hashtags", ""),
        "custom_description": a.get("custom_description", ""),
        "smart_titles": a.get("smart_titles", None),
        "top_watermark": a.get("top_watermark", ""),
        "top_watermark_enabled": a.get("top_watermark_enabled", None),
        "watermark": a.get("watermark", ""),
        "subtitles_enabled": a.get("subtitles_enabled", None),
        "expected_channel": a.get("expected_channel", ""),
        "watermark_enabled": a.get("watermark_enabled", None),
        "aspect": a.get("aspect", ""),
        "fill": a.get("fill", ""),
        "max_shorts_per_channel_cycle": a.get("max_shorts_per_channel_cycle", ""),
        "min_minutes_between_uploads": a.get("min_minutes_between_uploads", ""),
        "spread_uploads_across_window": a.get("spread_uploads_across_window", ""),
        "posting_timezone": a.get("posting_timezone", ""),
        "posting_start_time": a.get("posting_start_time", ""),
        "posting_end_time": a.get("posting_end_time", ""),
        "delete_after_upload": a.get("delete_after_upload", None),
        "delete_r2_after_upload": a.get("delete_r2_after_upload", None),
        "tiktok_enabled": a.get("tiktok_enabled", None),
        "tiktok_open_id": a.get("tiktok_open_id", ""),
        "tiktok_has_token": bool(str(a.get("tiktok_access_token") or "").strip()),
        "tiktok_privacy_level": a.get("tiktok_privacy_level", ""),
        "bilibili_enabled": a.get("bilibili_enabled", None),
        "bilibili_client_id": a.get("bilibili_client_id", ""),
        "bilibili_has_token": bool(str(a.get("bilibili_access_token") or "").strip()),
    }


def _bgm_tracks() -> list:
    if not BGM_DIR.exists():
        return []
    return [{"name": p.name, "size_mb": round(p.stat().st_size / (1024 * 1024), 2)}
            for p in sorted(BGM_DIR.iterdir()) if p.is_file() and p.suffix.lower() in ALLOWED_BGM_EXT]


def _finished_files() -> list:
    files = []
    if Path(KEEP_SHORTS_DIR).exists():
        try:
            entries = sorted((p for p in Path(KEEP_SHORTS_DIR).iterdir()
                              if p.is_file() and p.suffix.lower() == ".mp4"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            entries = []
        for p in entries:
            try:
                files.append({"name": p.name, "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
                              "modified": datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
            except Exception:
                continue
    return files


# Platform tabs: the top-level tab strip. Each one owns the connection UI for
# that destination; the account sub-tabs below it stay the same everywhere.
PLATFORMS = ("youtube", "tiktok", "bilibili")
# Settings each platform can override; blank falls back to the account value.
PLATFORM_TEXT_FIELDS = (
    "title_prefix",
    "title_hashtags",
    "extra_hashtags",
    "custom_description",
)
PLATFORM_LABELS = {
    "youtube": "▶️ YouTube",
    "tiktok": "🎵 TikTok",
    "bilibili": "📺 Bilibili",
}


# Every "<platform>_<field>" key, so account saves never drop them.
_PLATFORM_OVERRIDE_KEYS = [
    f"{_p}_{_f2}"
    for _p in PLATFORMS
    for _f2 in PLATFORM_TEXT_FIELDS + ("clip_seconds",)
]


def _clean_platform(value) -> str:
    """Normalise the ?platform= query value to a known platform tab."""
    platform = str(value or "").strip().lower()
    return platform if platform in PLATFORMS else "youtube"


def _redirect_msg(msg: str, ok: bool = True, account: str = None,
                  platform: str = None):
    """Redirect back to the SAME tab (account + platform) the user was on - so
    a save in one tab never 'jumps' to another tab and looks like it saved
    everywhere."""
    params = {"msg": msg, "type": "ok" if ok else "err"}
    if account:
        params["account"] = account
    if platform:
        params["platform"] = _clean_platform(platform)
    return redirect("/?" + urlencode(params))


def _mode_info() -> dict:
    base = Path(__file__).resolve().parent
    accounts = _accounts_from_disk()
    configured = False
    connected = False
    live = False
    for account in accounts:
        name = str(account.get("name") or "default")
        secret_path = credential_path(base, name, account.get("client_secret"), "client_secret.json")
        token_path = credential_path(base, name, account.get("token"), "token.json")
        has_secret = secret_path.is_file()
        has_token = token_path.is_file()
        configured = configured or has_secret
        connected = connected or has_token
        live = live or (has_secret and has_token)
    root_secret = base / "client_secret.json"
    root_token = Path(YOUTUBE_TOKEN_FILE)
    configured = configured or root_secret.is_file()
    connected = connected or root_token.is_file()
    live = live or (root_secret.is_file() and root_token.is_file())
    r2_dry = _is_placeholder(os.getenv("R2_ACCESS_KEY_ID", "")) or _is_placeholder(
        os.getenv("R2_SECRET_ACCESS_KEY", "")
    )
    return {
        "live": live and not DRY_RUN,
        "dry_run": DRY_RUN,
        "yt_secret": configured,
        "yt_token": connected,
        "r2_dry": r2_dry,
        "yt_secret_path": str(root_secret),
        "yt_token_path": str(root_token),
    }


def _clean_channels(value):
    if isinstance(value, list):
        return [str(c).strip() for c in value if str(c).strip()]
    if isinstance(value, str):
        return [c.strip() for c in value.replace("\n", ",").split(",") if c.strip()]
    return []


def _clean_account(acc: dict) -> dict:
    name = str(acc.get("name") or "").strip()
    if not _valid_account_name(name):
        raise ValueError("Account names cannot contain path separators and must be 1-80 characters")
    entry = {
        "name": name,
        "client_secret": relative_credential_value(name, "client_secret.json"),
        "token": relative_credential_value(name, "token.json"),
        "target_channels": _clean_channels(acc.get("target_channels")),
        "max_daily_uploads": min(30, max(1, int(acc.get("max_daily_uploads") or 10))),
        "enabled": bool(acc.get("enabled", True)),
    }
    for opt in ["aspect", "fill", "shorts_per_video", "process_mode", "selection_order",
                "min_minutes_between_uploads", "posting_timezone", "posting_start_time",
                "posting_end_time", "delete_after_upload", "delete_r2_after_upload",
                "watermark", "watermark_enabled", "top_watermark", "top_watermark_enabled",
                "extra_hashtags", "title_prefix", "title_hashtags", "smart_titles", "custom_description",
                "spread_uploads_across_window",
                "max_shorts_per_channel_cycle", "connected_channel", "connected_channel_id",
                "subtitles_enabled", "expected_channel",
                "tiktok_enabled", "tiktok_open_id", "tiktok_access_token",
                "tiktok_refresh_token", "tiktok_client_key", "tiktok_client_secret",
                "tiktok_privacy_level",
                "bilibili_enabled", "bilibili_client_id", "bilibili_client_secret",
                "bilibili_access_token", "bilibili_refresh_token", "bilibili_tid",
                "bilibili_copyright", "bilibili_source",
                *_PLATFORM_OVERRIDE_KEYS]:
        if opt not in acc:
            continue
        val = acc[opt]
        if opt in ("watermark_enabled", "top_watermark_enabled", "smart_titles",
                   "delete_after_upload", "delete_r2_after_upload",
                   "subtitles_enabled", "spread_uploads_across_window",
                   "tiktok_enabled", "bilibili_enabled"):
            # accept real bools AND the strings "true"/"false"/"0"/"1"
            if isinstance(val, bool):
                entry[opt] = val
            else:
                entry[opt] = str(val).strip().lower() in ("true", "on", "1", "yes")
        elif val is not None:
            # store empty strings too (so e.g. resetting order back to "" works)
            entry[opt] = val
    if "tiktok_privacy_level" in entry:
        level = str(entry["tiktok_privacy_level"] or "").strip()
        entry["tiktok_privacy_level"] = (
            level if level in TIKTOK_PRIVACY_LEVELS else "PUBLIC_TO_EVERYONE"
        )
    window_error = validate_posting_window(entry)
    if window_error:
        raise ValueError(window_error)
    return entry


def create_app(testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config.update(
        TESTING=bool(testing),
        SECRET_KEY=WEBUI_SECRET_KEY or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=WEBUI_COOKIE_SECURE,
    )

    @app.before_request
    def _protect_control_panel():
        if app.config["TESTING"]:
            return None
        if WEBUI_PASSWORD:
            auth = request.authorization
            valid = bool(
                auth
                and hmac.compare_digest(auth.username or "", WEBUI_USERNAME)
                and hmac.compare_digest(auth.password or "", WEBUI_PASSWORD)
            )
            if not valid:
                return Response(
                    "Authentication required",
                    401,
                    {"WWW-Authenticate": 'Basic realm="Shorts Bot"'},
                )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
            expected = session.get("csrf_token")
            if not supplied or not expected or not hmac.compare_digest(supplied, expected):
                return jsonify({"ok": False, "message": "Invalid or missing CSRF token"}), 403
        return None

    def _f(request, key, default=None):
        if request.is_json:
            val = (request.json or {}).get(key)
            return default if val is None else val
        val = request.form.get(key)
        if val is None:
            return default
        if isinstance(default, bool):
            return str(val).strip().lower() in ("true", "on", "1", "yes")
        if isinstance(default, int):
            try:
                return int(val)
            except (TypeError, ValueError):
                return default
        return val.strip() if isinstance(val, str) else val

    def _page(msg: str = "", msg_type: str = "ok", loaded_account: str = None,
              platform: str = "youtube"):
        return Response(
            _render_page(msg, msg_type, loaded_account, platform),
            mimetype="text/html",
        )

    @app.get("/")
    def index():
        return _page(request.args.get("msg", ""), request.args.get("type", "ok"),
                     request.args.get("account") or None,
                     _clean_platform(request.args.get("platform")))

    @app.get("/api/health")
    def api_health():
        return jsonify({"ok": True})

    # ---------------- ACTIONS ----------------
    @app.post("/api/run-once")
    def api_run_once():
        if PIPELINE_LOCK.locked():
            return _redirect_msg("Another video pipeline is currently active.", ok=False)
        if "run-once" in _active_jobs():
            return _redirect_msg("A cycle is already running - wait for it to finish.", ok=False)
        _spawn_job(
            "run-once",
            lambda: ShortsRepostScheduler().run_single_cycle(preflight_oauth=True),
        )
        logger.info("[webui] User clicked 'Run One Cycle'.")
        return _redirect_msg("Cycle started - watch the logs.")

    @app.post("/api/process-url")
    def api_process_url():
        url = _f(request, "url", "")
        if not url:
            return _redirect_msg("Please paste a YouTube URL first.", ok=False)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"
        }:
            return _redirect_msg("Only a valid YouTube URL is accepted.", ok=False)
        acc_name = (_f(request, "account") or "").strip()
        if not acc_name:
            return _redirect_msg("Choose the destination account explicitly.", ok=False)
        acc = _find_account(acc_name)
        if not acc:
            return _redirect_msg("The selected destination account no longer exists.", ok=False)
        acc_name = acc.get("name")
        if PIPELINE_LOCK.locked():
            return _redirect_msg("Another video pipeline is currently active.", ok=False)
        started = _spawn_job("process-url", lambda: repost_one_url(url, account=acc))
        if not started:
            return _redirect_msg("A video is already being processed - wait for it to finish.", ok=False)
        logger.info(f"[webui] Process URL: {url} (acc={acc_name})")
        return _redirect_msg(f"Processing {url} - watch the logs.")

    @app.post("/api/scheduler/start")
    def api_scheduler_start():
        global _scheduler_thread
        if PIPELINE_LOCK.locked():
            return _redirect_msg("Wait for the active video pipeline to finish.", ok=False)
        if _scheduler_thread and _scheduler_thread.is_alive():
            return _redirect_msg("The 24/7 scheduler is already running.", ok=False)
        auth_ok, auth_message = _preflight_enabled_accounts()
        if not auth_ok:
            return _redirect_msg(auth_message, ok=False)
        _scheduler_thread = threading.Thread(target=_scheduler_worker, daemon=True, name="webui-scheduler")
        _scheduler_thread.start()
        logger.info("[webui] 24/7 scheduler STARTED. %s", auth_message)
        return _redirect_msg(
            f"24/7 scheduler started (initial cycle runs now). {auth_message}"
        )

    @app.post("/api/scheduler/stop")
    def api_scheduler_stop():
        global _scheduler_instance
        if _scheduler_instance:
            _scheduler_instance.stop()
            return _redirect_msg("Stopping scheduler...")
        return _redirect_msg("The scheduler is not running.", ok=False)

    @app.post("/api/bgm/upload")
    def api_bgm_upload():
        file = request.files.get("file")
        if not file or not file.filename:
            return _redirect_msg("No music file selected.", ok=False)
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_BGM_EXT:
            return _redirect_msg(f"Only {', '.join(sorted(ALLOWED_BGM_EXT))} files are allowed.", ok=False)
        data = file.read()
        if len(data) > MAX_BGM_BYTES:
            return _redirect_msg("File is bigger than 25 MB - try a shorter track.", ok=False)
        BGM_DIR.mkdir(parents=True, exist_ok=True)
        (BGM_DIR / Path(file.filename).name).write_bytes(data)
        logger.info(f"[webui] Uploaded background music: {file.filename}")
        return _redirect_msg(f"Saved {file.filename} - the bot can now use it.")

    @app.post("/api/youtube-cookies")
    def api_youtube_cookies_upload():
        """Install viewer cookies used by yt-dlp for eligible 18+ sources."""
        file = request.files.get("file")
        if not file or not file.filename:
            return _redirect_msg("No cookies.txt file selected.", ok=False)
        data = file.read(MAX_COOKIE_BYTES + 1)
        if len(data) > MAX_COOKIE_BYTES:
            return _redirect_msg("cookies.txt is unexpectedly larger than 5 MB.", ok=False)
        if not _valid_youtube_cookie_export(data):
            return _redirect_msg(
                "Invalid cookies file. Export Netscape cookies.txt while signed in "
                "to YouTube with an age-verified 18+ Google account.",
                ok=False,
            )

        destination = _shared_cookie_path()
        temporary = destination.with_name(
            f".{destination.name}.{secrets.token_hex(6)}.tmp"
        )
        with _config_lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(data)
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, destination)
        # StateDB startup migration makes age gates skipped by older versions
        # retryable again. Constructing it here also applies that migration
        # immediately if the panel has stayed open during an upgrade.
        StateDB()
        logger.info(
            "[webui] Installed authenticated YouTube viewer cookies (%d bytes) at %s.",
            len(data),
            destination,
        )
        return _redirect_msg(
            "YouTube viewer cookies saved. Age-restricted sources are now retryable; "
            "refresh this file when YouTube expires the browser session."
        )

    @app.post("/api/youtube-cookies/delete")
    def api_youtube_cookies_delete():
        destination = _shared_cookie_path()
        destination.unlink(missing_ok=True)
        logger.info("[webui] Removed shared YouTube viewer cookies.")
        return _redirect_msg("YouTube viewer cookies removed.")

    @app.post("/api/client-secret")
    def api_client_secret_upload():
        file = request.files.get("file")
        if not file or not file.filename:
            return _redirect_msg("No file selected.", ok=False)
        data = file.read(MAX_CREDENTIAL_BYTES + 1)
        if len(data) > MAX_CREDENTIAL_BYTES:
            return _redirect_msg("Credential file is unexpectedly large.", ok=False)
        try:
            parsed_secret = json.loads(data.decode("utf-8"))
            oauth = parsed_secret.get("installed") or parsed_secret.get("web")
            if not isinstance(oauth, dict) or not oauth.get("client_id") or not oauth.get("client_secret"):
                raise ValueError("missing OAuth client fields")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _redirect_msg("That is not a valid Google OAuth client JSON file.", ok=False)
        acc_name = (_f(request, "account") or "").strip()
        if not _valid_account_name(acc_name):
            return _redirect_msg("Choose a valid account before uploading credentials.", ok=False)
        base = Path(__file__).resolve().parent
        relative_secret = relative_credential_value(acc_name, "client_secret.json")
        relative_token = relative_credential_value(acc_name, "token.json")
        dest = base / relative_secret
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not _set_account_field(acc_name, "client_secret", relative_secret):
            return _redirect_msg("Could not update this account.", ok=False)
        _set_account_field(acc_name, "token", relative_token)
        # A new OAuth client must not silently reuse a token minted for an old
        # client configuration.
        (base / relative_token).unlink(missing_ok=True)
        dest.write_bytes(data)
        logger.info(f"[webui] Uploaded client_secret.json ({len(data)} bytes) -> {dest}")
        return _redirect_msg(
            f"client_secret.json saved for this tab ({dest.name}). Now press Connect / Test YouTube "
            f"and sign in with THIS channel's Google account.",
            account=acc_name or None)

    @app.post("/api/test-youtube")
    def api_test_youtube():
        acc_name = (_f(request, "account") or "").strip() or None
        acc = None
        if acc_name:
            try:
                data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) if ACCOUNTS_FILE.exists() else {}
            except Exception:
                data = {}
            accounts = data.get("accounts", []) if isinstance(data, dict) else []
            low = acc_name.lower()
            acc = next((a for a in accounts if str(a.get("name") or "").strip().lower() == low), None)
            if acc is None:
                if not _valid_account_name(acc_name):
                    return _redirect_msg("Invalid account name.", ok=False)
                acc = {
                    "name": acc_name,
                    "target_channels": [],
                    "max_daily_uploads": 10,
                    "enabled": True,
                    "client_secret": relative_credential_value(acc_name, "client_secret.json"),
                    "token": relative_credential_value(acc_name, "token.json"),
                }
                accounts.append(acc)
                _write_accounts(accounts)
                logger.info(f"[webui] Created new account '{acc_name}'")
        else:
            acc = _find_account(None)

        def _do():
            try:
                from .uploader import YouTubeUploader, resolve_credentials
                if acc is not None:
                    cs, tk = resolve_credentials(acc)
                    logger.info(f"[webui] Connect '{acc.get('name')}': secret={cs} token={tk}")
                    up = YouTubeUploader(client_secret_file=cs, token_file=tk, state_db=StateDB())
                else:
                    up = YouTubeUploader()
                # Force a refresh even when the short-lived access token still
                # works, so Connect/Test detects a dead seven-day refresh token
                # and immediately opens the consent flow to replace it.
                svc = up._get_authenticated_service(force_refresh=True)
                if svc:
                    logger.info(f"[webui] ✅ YouTube auth OK for '{acc_name or 'default'}'")
                    try:
                        ch = svc.channels().list(part="id,snippet", mine=True).execute()
                        items = ch.get("items") or []
                        if items:
                            cname = items[0]["snippet"]["title"]
                            channel_id = str(items[0].get("id") or "")
                            logger.info(
                                "[webui] Account '%s' connected to channel '%s'.",
                                acc_name or "default",
                                cname,
                            )
                            exp = (acc or {}).get("expected_channel") or ""
                            if exp and cname and exp.strip().lower() not in cname.strip().lower() and cname.strip().lower() not in exp.strip().lower():
                                logger.warning(
                                    f"⚠️ CHANNEL MISMATCH: tab '{acc_name}' expects '{exp}' "
                                    f"but this Google login belongs to '{cname}'. "
                                    f"Uploads to this tab will be BLOCKED until you connect "
                                    f"with the Google account that owns '{exp}'."
                                )
                            try:
                                import json as _json
                                _data = _json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) if ACCOUNTS_FILE.exists() else {}
                                _accs = _data.get("accounts", []) if isinstance(_data, dict) else []
                                for _a in _accs:
                                    if str(_a.get("name") or "").strip().lower() == str(acc_name or "").lower():
                                        _a["connected_channel"] = cname
                                        _a["connected_channel_id"] = channel_id
                                        if not str(_a.get("expected_channel") or "").strip():
                                            _a["expected_channel"] = cname
                                        break
                                _write_accounts(_accs)
                            except (OSError, json.JSONDecodeError) as exc:
                                logger.error("[webui] Could not persist connected channel: %s", exc)
                        else:
                            logger.info("[webui] ⚠️ Connected, but no channel found on this Google account.")
                    except Exception as e:
                        logger.info(f"[webui] ✅ YouTube auth OK (could not fetch channel name: {e})")
                else:
                    logger.warning(f"[webui] ⚠️ YouTube auth failed for '{acc_name or 'default'}'")
            except Exception as e:
                logger.error(f"[webui] YouTube auth error for '{acc_name or 'default'}': {e}")

        redir_name = acc.get("name") if acc is not None else acc_name
        _spawn_job("test-youtube", _do)
        return _redirect_msg("Auth started - a browser tab may open for login. Watch the logs.", account=redir_name)

    # ------------- CROSS-POSTING (TikTok + Bilibili) -------------
    @app.post("/api/platform-settings/save")
    def api_platform_settings_save():
        """Per-destination overrides: clip length, title prefix, hashtags,
        description. Blank means 'use the account-wide value'."""
        name = (_f(request, "account") or "").strip()
        if not _valid_account_name(name):
            return _redirect_msg("Invalid account name.", ok=False)
        platform = _clean_platform(_f(request, "platform"))
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) if ACCOUNTS_FILE.exists() else {}
        except Exception:
            data = {}
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        low = name.lower()
        acc = next((a for a in accounts if str(a.get("name") or "").strip().lower() == low), None)
        if acc is None:
            acc = {"name": name, "target_channels": [], "max_daily_uploads": 10, "enabled": True}
            accounts.append(acc)
        for field in PLATFORM_TEXT_FIELDS:
            key = f"{platform}_{field}"
            if key in request.form or (request.is_json and key in (request.json or {})):
                acc[key] = str(_f(request, key) or "").strip()
        clip_key = f"{platform}_clip_seconds"
        if clip_key in request.form or (request.is_json and clip_key in (request.json or {})):
            raw = str(_f(request, clip_key) or "").strip()
            if not raw:
                # Empty = inherit the account-wide / global clip length.
                acc.pop(clip_key, None)
            else:
                try:
                    acc[clip_key] = float(clamp_clip_duration(raw))
                except Exception:
                    return _redirect_msg(
                        "Clip length must be a number of seconds.",
                        ok=False, account=name, platform=platform,
                    )
        _write_accounts(accounts)
        logger.info("[webui] Saved %s settings for account '%s'.", platform, name)
        return _redirect_msg(
            f"{PLATFORM_LABELS.get(platform, platform)} settings saved for '{name}'.",
            account=name, platform=platform,
        )

    @app.post("/api/social/save")
    def api_social_save():
        name = (_f(request, "account") or "").strip()
        if not _valid_account_name(name):
            return _redirect_msg("Invalid account name.", ok=False)
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) if ACCOUNTS_FILE.exists() else {}
        except Exception:
            data = {}
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        low = name.lower()
        acc = next((a for a in accounts if str(a.get("name") or "").strip().lower() == low), None)
        if acc is None:
            acc = {"name": name, "target_channels": [], "max_daily_uploads": 10, "enabled": True}
            accounts.append(acc)
        payload = request.json if request.is_json else None

        def _present(key: str) -> bool:
            return key in request.form or (payload is not None and key in (payload or {}))

        # Enable toggles are presence-based, so saving any other settings form
        # can never silently switch cross-posting off.
        for field in ("tiktok_enabled", "bilibili_enabled"):
            if _present(field):
                raw = _f(request, field)
                if isinstance(raw, bool):
                    acc[field] = raw
                else:
                    acc[field] = str(raw or "").strip().lower() in ("true", "on", "1", "yes")
        # Plain IDs: an empty value clears the field.
        for field in ("tiktok_open_id", "tiktok_client_key",
                      "bilibili_client_id", "bilibili_tid",
                      "bilibili_copyright", "bilibili_source"):
            if _present(field):
                acc[field] = str(_f(request, field) or "").strip()
        # Secrets: only overwrite when a non-empty value is submitted, so the
        # panel never needs to echo them back into the page to keep them.
        for field in ("tiktok_access_token", "tiktok_refresh_token",
                      "tiktok_client_secret", "bilibili_access_token",
                      "bilibili_refresh_token", "bilibili_client_secret"):
            if _present(field):
                value = str(_f(request, field) or "").strip()
                if value:
                    acc[field] = value
        if _present("tiktok_privacy_level"):
            level = str(_f(request, "tiktok_privacy_level") or "").strip()
            if level in TIKTOK_PRIVACY_LEVELS:
                acc["tiktok_privacy_level"] = level
        _write_accounts(accounts)
        logger.info("[webui] Saved cross-posting settings for account '%s' (secrets not logged).", name)
        return _redirect_msg(
            f"Cross-posting settings saved for account '{name}'.",
            account=name,
            platform=_clean_platform(_f(request, "platform")),
        )

    @app.post("/api/test-tiktok")
    def api_test_tiktok():
        acc_name = (_f(request, "account") or "").strip()
        acc = _find_account(acc_name) if acc_name else None
        if acc is None:
            return _redirect_msg("Choose the account tab to test first.", ok=False)
        target = dict(acc)

        def _do():
            try:
                from .social_tiktok import TikTokUploader

                ok, detail = TikTokUploader(
                    open_id=str(target.get("tiktok_open_id") or ""),
                    access_token=str(target.get("tiktok_access_token") or ""),
                    refresh_token=str(target.get("tiktok_refresh_token") or ""),
                    client_key=str(target.get("tiktok_client_key") or ""),
                    client_secret=str(target.get("tiktok_client_secret") or ""),
                ).check_connection()
                if ok:
                    logger.info("[webui] ✅ TikTok check for '%s': %s", target.get("name"), detail)
                else:
                    logger.warning("[webui] ⚠️ TikTok check for '%s' failed: %s", target.get("name"), detail)
            except Exception as exc:
                logger.error("[webui] TikTok check error for '%s': %s", target.get("name"), exc)

        _spawn_job("test-tiktok", _do)
        return _redirect_msg("Testing TikTok - watch the logs.",
                             account=target.get("name"), platform="tiktok")

    # ---------------- ACCOUNT SETTINGS (per-account) ----------------
    @app.post("/api/test-bilibili")
    def api_test_bilibili():
        acc_name = (_f(request, "account") or "").strip()
        acc = _find_account(acc_name) if acc_name else None
        if acc is None:
            return _redirect_msg("Choose the account tab to test first.", ok=False)
        target = dict(acc)

        def _do():
            try:
                from .social_bilibili import BilibiliUploader

                ok, detail = BilibiliUploader(
                    client_id=str(target.get("bilibili_client_id") or ""),
                    client_secret=str(target.get("bilibili_client_secret") or ""),
                    access_token=str(target.get("bilibili_access_token") or ""),
                    refresh_token=str(target.get("bilibili_refresh_token") or ""),
                ).check_connection()
                if ok:
                    logger.info("[webui] ✅ Bilibili check for '%s': %s", target.get("name"), detail)
                else:
                    logger.warning("[webui] ⚠️ Bilibili check for '%s' failed: %s", target.get("name"), detail)
            except Exception as exc:
                logger.error("[webui] Bilibili check error for '%s': %s", target.get("name"), exc)

        _spawn_job("test-bilibili", _do)
        return _redirect_msg("Testing Bilibili - watch the logs.",
                             account=target.get("name"), platform="bilibili")

    @app.post("/api/account-settings/save")
    def api_account_settings_save():
        name = (_f(request, "account") or "default").strip()
        if not _valid_account_name(name):
            return _redirect_msg("Invalid account name.", ok=False)
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) if ACCOUNTS_FILE.exists() else {}
        except Exception:
            data = {}
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        low = name.lower()
        acc = next((a for a in accounts if str(a.get("name") or "").strip().lower() == low), None)
        if acc is None:
            acc = {"name": name, "target_channels": [], "max_daily_uploads": 10, "enabled": True}
            accounts.append(acc)
        for field in [
            "title_prefix", "title_hashtags", "top_watermark", "watermark",
            "aspect", "fill", "expected_channel", "posting_timezone",
            "posting_start_time", "posting_end_time", "custom_description"
        ]:
            if field in request.form or (request.is_json and field in (request.json or {})):
                acc[field] = str(_f(request, field) or "").strip()
        for field in ["max_daily_uploads", "max_shorts_per_channel_cycle", "min_minutes_between_uploads"]:
            if field in request.form or (request.is_json and field in (request.json or {})):
                try:
                    value = int(float(_f(request, field)))
                    if field == "min_minutes_between_uploads":
                        acc[field] = min(1440, max(0, value))
                    elif field == "max_daily_uploads":
                        acc[field] = min(30, max(1, value))
                    else:
                        acc[field] = min(20, max(1, value))
                except (TypeError, ValueError):
                    pass
        for field in ["smart_titles", "top_watermark_enabled", "watermark_enabled",
                      "delete_after_upload", "delete_r2_after_upload", "spread_uploads_across_window"]:
            if field in request.form or (request.is_json and field in (request.json or {})):
                raw = _f(request, field)
                if isinstance(raw, bool):
                    acc[field] = raw
                else:
                    acc[field] = str(raw or "").strip().lower() in ("true", "on", "1", "yes")
            elif not request.is_json:
                # Unchecked HTML checkboxes are absent from form submissions.
                acc[field] = False
        # subtitles_enabled: checkbox + hidden "false" twin -> value always sent
        if "subtitles_enabled" in request.form or (request.is_json and "subtitles_enabled" in (request.json or {})):
            vals = request.form.getlist("subtitles_enabled") if request.form else [str((request.json or {}).get("subtitles_enabled", ""))]
            acc["subtitles_enabled"] = ("true" in [v.strip().lower() for v in vals]
                                        or "on" in [v.strip().lower() for v in vals]
                                        or "1" in [v.strip().lower() for v in vals])
        window_error = validate_posting_window(acc)
        if window_error:
            return _redirect_msg(window_error, ok=False, account=name)
        if "cycle_interval_hours" in request.form or (request.is_json and "cycle_interval_hours" in (request.json or {})):
            try:
                _write_env_changes({"CYCLE_INTERVAL_HOURS": int(float(_f(request, "cycle_interval_hours")))})
            except (TypeError, ValueError):
                pass
        _write_accounts(accounts)
        logger.info(f"[webui] Saved settings for account '{name}'")
        return _redirect_msg(f"Settings saved for account '{name}'.", account=name)

    # ---------------- SOURCES (per-account, indexed) ----------------
    @app.post("/api/accounts/save")
    def api_accounts_save():
        if request.is_json:
            raw = (request.json or {}).get("accounts") or []
            try:
                accounts = [
                    _clean_account(account)
                    for account in raw
                    if isinstance(account, dict)
                    and str(account.get("name") or "").strip()
                ]
            except (TypeError, ValueError) as exc:
                return _redirect_msg(f"Invalid account data: {exc}", ok=False)
            if not accounts:
                return _redirect_msg("No valid accounts in the list.", ok=False)
            _write_accounts(accounts)
            return _redirect_msg(f"Saved {len(accounts)} account(s).")

        form = request.form
        existing = list(_accounts_from_disk())
        indexed = {}
        for key in form.keys():
            m = re.match(r"^acc_(name|channels|maxdaily|enabled|processmode|order)_(\d+)$", key)
            if not m:
                continue
            field, idx = m.group(1), int(m.group(2))
            indexed.setdefault(idx, {})[field] = form.get(key)
        accounts = []
        for idx in sorted(indexed):
            entry = indexed[idx]
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            # merge with the existing account of the SAME name (case-insensitive)
            # so editing one tab never touches the other tabs
            old = next((a for a in existing if str(a.get("name") or "").strip().lower() == name.lower()), None)
            acc = _clean_account({
                "name": name,
                "target_channels": _clean_channels(entry.get("channels") or ""),
                "max_daily_uploads": int(entry.get("maxdaily") or 10),
                "enabled": str(entry.get("enabled") or "").lower() in ("true", "on", "1", "yes"),
                "process_mode": entry.get("processmode", ""),
                "selection_order": entry.get("order", ""),
                "client_secret": old.get("client_secret") if old else None,
                "token": old.get("token") if old else None,
            })
            if old is not None:
                for keep in ["title_prefix", "title_hashtags", "smart_titles", "top_watermark",
                             "top_watermark_enabled", "watermark", "watermark_enabled", "aspect",
                             "fill", "max_shorts_per_channel_cycle", "shorts_per_video",
                             "min_minutes_between_uploads", "posting_timezone",
                             "posting_start_time", "posting_end_time", "delete_after_upload",
                             "delete_r2_after_upload", "connected_channel", "connected_channel_id",
                             "subtitles_enabled", "expected_channel", "custom_description",
                             "spread_uploads_across_window",
                             "tiktok_enabled",
                             "tiktok_open_id", "tiktok_access_token",
                             "tiktok_refresh_token", "tiktok_client_key",
                             "tiktok_client_secret", "tiktok_privacy_level",
                             "bilibili_enabled", "bilibili_client_id",
                             "bilibili_client_secret", "bilibili_access_token",
                             "bilibili_refresh_token", "bilibili_tid",
                             "bilibili_copyright", "bilibili_source",
                             *_PLATFORM_OVERRIDE_KEYS]:
                    if keep in old and keep not in acc:
                        acc[keep] = old[keep]
            accounts.append(acc)
        # PRESERVE accounts that were not part of this form (other tabs).
        # Without this, saving one tab's source channels would delete everyone else.
        names_in_form = {str(a.get("name") or "").strip().lower() for a in accounts}
        for a in existing:
            if str(a.get("name") or "").strip().lower() not in names_in_form:
                accounts.append(a)
        if not accounts:
            return _redirect_msg("No valid accounts in the list.", ok=False)
        _write_accounts(accounts)
        logger.info(f"[webui] Saved {len(accounts)} account(s) to accounts.json")
        # stay on the tab that was being edited
        first_row = indexed[sorted(indexed)[0]]
        tab_name = str(first_row.get("name") or "").strip() or None
        return _redirect_msg(f"Saved {len(accounts)} account(s).", account=tab_name)

    @app.post("/api/accounts/add")
    def api_accounts_add():
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) if ACCOUNTS_FILE.exists() else {}
        except Exception:
            data = {}
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        # Always pick a UNIQUE name: after deletions, len(accounts)+1 could
        # collide with an existing "New Channel N" and merge two tabs into
        # one account (settings would appear to 'save to both').
        used = {str(a.get("name") or "").strip().lower() for a in accounts}
        n = 1
        while f"new channel {n}" in used:
            n += 1
        name = f"New Channel {n}"
        accounts.append({
            "name": name,
            "target_channels": [],
            "max_daily_uploads": 10,
            "enabled": True,
            "client_secret": relative_credential_value(name, "client_secret.json"),
            "token": relative_credential_value(name, "token.json"),
        })
        _write_accounts(accounts)
        return _redirect_msg(
            f"Account '{name}' added - configure it in its tab.",
            account=name,
            platform=_clean_platform(_f(request, "platform")),
        )

    @app.post("/api/accounts/delete")
    def api_accounts_delete():
        name = (_f(request, "account") or "").strip()
        if not name:
            return _redirect_msg("No account name given to delete.", ok=False)
        if PIPELINE_LOCK.locked():
            return _redirect_msg("Stop or wait for the active pipeline before deleting an account.", ok=False, account=name)
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        low = name.lower()
        before = len(data.get("accounts", []))
        accounts = [a for a in data.get("accounts", []) if str(a.get("name") or "").strip().lower() != low]
        if len(accounts) == before:
            return _redirect_msg(f"Account '{name}' was not found - nothing deleted.", ok=False)
        if not accounts:
            # Never leave an empty account list (panel would show a phantom tab)
            accounts = [{"name": "New Channel 1", "target_channels": [], "max_daily_uploads": 10, "enabled": True}]
        _write_accounts(accounts)
        try:
            StateDB().delete_account_data(name)
        except Exception as exc:
            logger.error("[webui] Account tab deleted but DB cleanup failed for '%s': %s", name, exc)
        # stay on a REAL tab after deleting
        next_tab = accounts[0].get("name")
        logger.info(f"[webui] Deleted account '{name}'. Remaining: {[a.get('name') for a in accounts]}")
        return _redirect_msg(
            f"Deleted account '{name}'. Credential files remain in "
            f"accounts/{safe_account_slug(name)}/; delete that folder manually if desired.",
            account=next_tab)

    # ---------------- JSON (for JS refresh) ----------------
    @app.get("/api/status")
    def api_status():
        db = StateDB()
        total_bytes, objects = CloudStorageManager().get_bucket_usage()
        accounts_info = []
        for acc in _accounts_from_disk():
            a_name = acc.get("name", "default")
            accounts_info.append({"name": a_name,
                                  "enabled": bool(acc.get("enabled", True)),
                                  "uploads_24h": db.get_uploads_in_last_24_hours(account=a_name),
                                  "max_uploads_24h": int(acc.get("max_daily_uploads") or MAX_DAILY_UPLOADS),
                                  "channels": len(acc.get("target_channels") or [])})
        return jsonify({
            "uploads_24h": db.get_uploads_in_last_24_hours(),
            "max_uploads_24h": MAX_DAILY_UPLOADS,
            "accounts": accounts_info,
            "total_shorts": db.count_video_records(),
            "r2_gb": round(total_bytes / (1024 ** 3), 3),
            "r2_clips": len(objects),
            "scheduler_running": bool(_scheduler_thread and _scheduler_thread.is_alive()),
            "active_jobs": _active_jobs(),
            "ffmpeg_available": FFMPEG_PATH is not None,
            "mode": _mode_info(),
        })

    @app.get("/api/logs")
    def api_logs():
        try:
            lines = int(request.args.get("lines", 120))
        except (TypeError, ValueError):
            lines = 120
        lines = min(max(lines, 10), 1000)
        return jsonify({"lines": _tail_log(lines)})

    @app.get("/api/finished")
    def api_finished():
        return jsonify({"files": _finished_files(), "folder": str(Path(KEEP_SHORTS_DIR))})

    @app.get("/download-short/<path:name>")
    def api_download_short(name: str):
        return send_from_directory(str(Path(KEEP_SHORTS_DIR)), os.path.basename(name), as_attachment=False)

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"ok": False, "message": "Endpoint not found - restart the panel."}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"ok": False, "message": f"Server error: {e}"}), 500

    return app


# ---------------------------------------------------------------------------
def _render_page(msg: str = "", msg_type: str = "ok",
                 loaded_account: Optional[str] = None,
                 platform: str = "youtube") -> str:
    platform = _clean_platform(platform)
    db = StateDB()
    total_bytes, objects = CloudStorageManager().get_bucket_usage()
    mode = _mode_info()
    disk_accounts = _accounts_from_disk()
    uploads_24h = db.get_uploads_in_last_24_hours()
    total_shorts = db.count_video_records()
    sched_running = bool(_scheduler_thread and _scheduler_thread.is_alive())
    csrf_token = ""
    if has_request_context():
        csrf_token = session.get("csrf_token") or secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token
    csrf_input = (
        f'<input type="hidden" name="_csrf_token" value="{_esc(csrf_token)}">'
        if csrf_token
        else ""
    )

    # -------- resolve the active tab account --------
    if not loaded_account and disk_accounts:
        loaded_account = disk_accounts[0].get("name", "default")
    if not loaded_account:
        loaded_account = "default"
    loaded_acc = next((a for a in disk_accounts if str(a.get("name") or "").strip().lower() == str(loaded_account).lower()), None) or {"name": loaded_account}
    st = _account_state(loaded_acc, db)

    def _aset(key, env_default, is_bool=False):
        v = loaded_acc.get(key)
        if v is None:
            return env_default
        if is_bool:
            return bool(v)
        return v

    acc_settings = {
        "title_prefix": _aset("title_prefix", ""),
        "title_hashtags": _aset("title_hashtags", ""),
        "custom_description": _aset("custom_description", ""),
        "smart_titles": _aset("smart_titles", _get_env_setting("ENABLE_SMART_TITLES", "true").lower() == "true", is_bool=True),
        "max_daily_uploads": _aset("max_daily_uploads", _get_env_setting("MAX_DAILY_UPLOADS", str(MAX_DAILY_UPLOADS))),
        "max_shorts_per_channel_cycle": _aset("max_shorts_per_channel_cycle", "2"),
        "subtitles_enabled": _aset("subtitles_enabled", False, is_bool=True),
        "expected_channel": _aset("expected_channel", ""),
        "top_watermark": _aset("top_watermark", ""),
        "top_watermark_enabled": _aset("top_watermark_enabled", True, is_bool=True),
        "watermark": _aset("watermark", "LIKE & SUBSCRIBE"),
        "watermark_enabled": _aset("watermark_enabled", True, is_bool=True),
        "aspect": _aset("aspect", "auto"),
        "fill": _aset("fill", _get_env_setting("FILL_MODE", FILL_MODE)),
        "min_minutes_between_uploads": _aset("min_minutes_between_uploads", "0"),
        "spread_uploads_across_window": _aset("spread_uploads_across_window", False, is_bool=True),
        "posting_timezone": _aset("posting_timezone", ""),
        "posting_start_time": _aset("posting_start_time", ""),
        "posting_end_time": _aset("posting_end_time", ""),
        "delete_after_upload": _aset("delete_after_upload", False, is_bool=True),
        "delete_r2_after_upload": _aset("delete_r2_after_upload", False, is_bool=True),
        "cycle_interval_hours": _get_env_setting("CYCLE_INTERVAL_HOURS", str(CYCLE_INTERVAL_HOURS)),
    }

    chk = lambda v: " checked" if v else ""

    # -------- cross-posting (TikTok + Bilibili) for the active tab --------
    # Secrets are NEVER echoed back: the form only shows whether one is saved,
    # and submitting a blank secret field keeps the stored value.
    soc = {
        "tiktok_enabled": bool(loaded_acc.get("tiktok_enabled")),
        "tiktok_open_id": str(loaded_acc.get("tiktok_open_id") or ""),
        "tiktok_has_token": bool(str(loaded_acc.get("tiktok_access_token") or "").strip()),
        "tiktok_has_refresh": bool(str(loaded_acc.get("tiktok_refresh_token") or "").strip()),
        "tiktok_client_key": str(loaded_acc.get("tiktok_client_key") or ""),
        "tiktok_has_client_secret": bool(str(loaded_acc.get("tiktok_client_secret") or "").strip()),
        "tiktok_privacy_level": str(loaded_acc.get("tiktok_privacy_level") or "PUBLIC_TO_EVERYONE"),
        "bilibili_enabled": bool(loaded_acc.get("bilibili_enabled")),
        "bilibili_client_id": str(loaded_acc.get("bilibili_client_id") or ""),
        "bilibili_has_client_secret": bool(str(loaded_acc.get("bilibili_client_secret") or "").strip()),
        "bilibili_has_token": bool(str(loaded_acc.get("bilibili_access_token") or "").strip()),
        "bilibili_has_refresh": bool(str(loaded_acc.get("bilibili_refresh_token") or "").strip()),
        "bilibili_tid": str(loaded_acc.get("bilibili_tid") or BILIBILI_DEFAULT_TID),
        "bilibili_copyright": str(loaded_acc.get("bilibili_copyright") or "1"),
        "bilibili_source": str(loaded_acc.get("bilibili_source") or ""),
    }
    if soc["bilibili_enabled"] and soc["bilibili_client_id"] and soc["bilibili_has_token"]:
        bb_badge = '<span class="badge ok">Bilibili: on</span>'
    elif soc["bilibili_enabled"]:
        bb_badge = '<span class="badge warn">Bilibili: on but incomplete</span>'
    else:
        bb_badge = '<span class="badge">Bilibili: off</span>'
    if soc["tiktok_enabled"] and soc["tiktok_open_id"] and soc["tiktok_has_token"]:
        tt_badge = '<span class="badge ok">TikTok: on</span>'
    elif soc["tiktok_enabled"]:
        tt_badge = '<span class="badge warn">TikTok: on but incomplete</span>'
    else:
        tt_badge = '<span class="badge">TikTok: off</span>'
    privacy_options = "".join(
        f'<option value="{level}"{" selected" if soc["tiktok_privacy_level"] == level else ""}>'
        f"{_esc(level.replace('_', ' ').title())}</option>"
        for level in TIKTOK_PRIVACY_LEVELS
    )

    copyright_options = "".join(
        f'<option value="{value}"{" selected" if soc["bilibili_copyright"] == value else ""}>'
        f"{label}</option>"
        for value, label in (("1", "自制 / Original"), ("2", "转载 / Repost"))
    )

    def _secret_hint(has_value: bool, empty_hint: str) -> str:
        return "saved ✓ (leave blank to keep)" if has_value else empty_hint

    tiktok_card = f"""
    <div class="card" style="margin-top:16px;">
      <h2 style="font-size:14px;">🎵 Post to TikTok {tt_badge}</h2>
      <div class="hint">After each Short is rendered, the bot ALSO posts it here — even when the
        YouTube upload waits on quota or fails. Uses the official TikTok API
        (no password logins). Full setup steps: <b>SETUP_TIKTOK.md</b>.</div>
      <form action="/api/social/save" method="POST">
        <input type="hidden" name="account" value="{_esc(loaded_account)}">
        <input type="hidden" name="platform" value="tiktok">
        <table style="width:100%;font-size:13px;border-collapse:collapse;margin-top:6px;">
          <tr><td style="padding:4px 0;width:38%;">Post to TikTok</td>
              <td><input type="checkbox" name="tiktok_enabled" value="true"{chk(soc["tiktok_enabled"])} style="transform:scale(1.3);"></td></tr>
          <tr><td style="padding:4px 0;">TikTok open_id</td>
              <td><input type="text" name="tiktok_open_id" value="{_esc(soc["tiktok_open_id"])}" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">TikTok access token (expires ~24h)</td>
              <td><input type="password" name="tiktok_access_token" value="" placeholder="{_secret_hint(soc["tiktok_has_token"], "paste an access token")}" style="width:100%;" autocomplete="off"></td></tr>
          <tr><td style="padding:4px 0;">TikTok refresh token (enables auto-renewal)</td>
              <td><input type="password" name="tiktok_refresh_token" value="" placeholder="{_secret_hint(soc["tiktok_has_refresh"], "paste a refresh token")}" style="width:100%;" autocomplete="off"></td></tr>
          <tr><td style="padding:4px 0;">TikTok client key</td>
              <td><input type="text" name="tiktok_client_key" value="{_esc(soc["tiktok_client_key"])}" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">TikTok client secret</td>
              <td><input type="password" name="tiktok_client_secret" value="" placeholder="{_secret_hint(soc["tiktok_has_client_secret"], "needed for auto-renewal")}" style="width:100%;" autocomplete="off"></td></tr>
          <tr><td style="padding:4px 0;">TikTok privacy</td>
              <td><select name="tiktok_privacy_level" style="width:100%;">{privacy_options}</select></td></tr>
        </table>
        <div class="row" style="margin-top:10px;"><button type="submit">Save TikTok settings</button></div>
      </form>
      <div class="row" style="margin-top:10px;">
        <form action="/api/test-tiktok" method="POST" style="display:inline;">
          <input type="hidden" name="account" value="{_esc(loaded_account)}">
          <button class="green" type="submit">Test TikTok</button>
        </form>
      </div>
      <div class="hint" style="border:1px solid var(--border);border-radius:8px;padding:8px;margin-top:8px;">
        TikTok uploads the rendered file directly (a public R2 URL is used when available).
        The refresh token + client key/secret renew its ~24h access token automatically.
        Secrets live in the ignored accounts.json and are never shown back.
      </div>
    </div>
    """

    bilibili_card = f"""
    <div class="card" style="margin-top:16px;">
      <h2 style="font-size:14px;">📺 Post to Bilibili {bb_badge}</h2>
      <div class="hint">After each Short is rendered, the bot ALSO submits it to Bilibili — even
        when the YouTube upload waits on quota or fails. Uses the official Open Platform
        API (no password logins). Full setup steps: <b>SETUP_BILIBILI.md</b>.</div>
      <form action="/api/social/save" method="POST">
        <input type="hidden" name="account" value="{_esc(loaded_account)}">
        <input type="hidden" name="platform" value="bilibili">
        <table style="width:100%;font-size:13px;border-collapse:collapse;margin-top:6px;">
          <tr><td style="padding:4px 0;width:38%;">Post to Bilibili</td>
              <td><input type="checkbox" name="bilibili_enabled" value="true"{chk(soc["bilibili_enabled"])} style="transform:scale(1.3);"></td></tr>
          <tr><td style="padding:4px 0;">Bilibili client id</td>
              <td><input type="text" name="bilibili_client_id" value="{_esc(soc["bilibili_client_id"])}" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Bilibili client secret</td>
              <td><input type="password" name="bilibili_client_secret" value="" placeholder="{_secret_hint(soc["bilibili_has_client_secret"], "app secret from the open platform")}" style="width:100%;" autocomplete="off"></td></tr>
          <tr><td style="padding:4px 0;">Bilibili access token</td>
              <td><input type="password" name="bilibili_access_token" value="" placeholder="{_secret_hint(soc["bilibili_has_token"], "paste an access token")}" style="width:100%;" autocomplete="off"></td></tr>
          <tr><td style="padding:4px 0;">Bilibili refresh token (enables auto-renewal)</td>
              <td><input type="password" name="bilibili_refresh_token" value="" placeholder="{_secret_hint(soc["bilibili_has_refresh"], "paste a refresh token")}" style="width:100%;" autocomplete="off"></td></tr>
          <tr><td style="padding:4px 0;">Bilibili 分区 tid (category)</td>
              <td><input type="text" name="bilibili_tid" value="{_esc(soc["bilibili_tid"])}" placeholder="21 = 日常" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Bilibili copyright</td>
              <td><select name="bilibili_copyright" style="width:100%;">{copyright_options}</select></td></tr>
          <tr><td style="padding:4px 0;">Repost source (转载 only)</td>
              <td><input type="text" name="bilibili_source" value="{_esc(soc["bilibili_source"])}" placeholder="original video URL" style="width:100%;"></td></tr>
        </table>
        <div class="row" style="margin-top:10px;"><button type="submit">Save Bilibili settings</button></div>
      </form>
      <div class="row" style="margin-top:10px;">
        <form action="/api/test-bilibili" method="POST" style="display:inline;">
          <input type="hidden" name="account" value="{_esc(loaded_account)}">
          <button class="green" type="submit">Test Bilibili</button>
        </form>
      </div>
      <div class="hint" style="border:1px solid var(--border);border-radius:8px;padding:8px;margin-top:8px;">
        Bilibili always uploads the local file (there is no pull-from-URL flow), and every
        archive goes through 审核 review before it appears publicly. The refresh token +
        client id/secret renew the access token automatically. Secrets live in the ignored
        accounts.json and are never shown back.
      </div>
    </div>
    """
    if mode["live"]:
        mode_badge = '<span class="badge ok">Mode: LIVE</span>'
    elif mode["dry_run"]:
        mode_badge = '<span class="badge warn">Mode: EXPLICIT DRY-RUN</span>'
    else:
        mode_badge = '<span class="badge warn">Mode: NOT READY (connect OAuth)</span>'
    sched_badge = ('<span class="badge run">Scheduler: RUNNING</span>' if sched_running else '<span class="badge">Scheduler: stopped</span>')
    jobs_badge = f'<span class="badge">{len(_active_jobs())} job(s)</span>' if _active_jobs() else '<span class="badge">Jobs: none</span>'

    msg_html = ""
    if msg:
        color = "#30d158" if msg_type == "ok" else "#ff453a"
        msg_html = f'<div style="background:{color}22;border:1px solid {color};border-radius:10px;padding:12px;margin-top:16px;font-size:14px;">{_esc(msg)}</div>'

    # -------- PLATFORM TABS (top level): YouTube / TikTok / Bilibili --------
    # Each platform tab shows the same account sub-tabs underneath, so the
    # user picks "where to post" first and "which channel" second.
    platform_status = {
        "youtube": bool(st["connected"]),
        "tiktok": bool(soc["tiktok_enabled"] and soc["tiktok_open_id"]
                       and soc["tiktok_has_token"]),
        "bilibili": bool(soc["bilibili_enabled"] and soc["bilibili_client_id"]
                         and soc["bilibili_has_token"]),
    }
    platform_tabs = ""
    for key in PLATFORMS:
        active = "ptab-active" if key == platform else ""
        dot_color = "var(--green)" if platform_status.get(key) else "var(--muted)"
        dot = f'<span class="tab-dot" style="background:{dot_color};"></span>'
        platform_tabs += (
            f'<a class="ptab {active}" href="/?account={quote(str(loaded_account))}'
            f'&platform={key}">{dot}{PLATFORM_LABELS[key]}</a>'
        )

    # -------- ACCOUNT SUB-TABS (like Chrome): one per account + a "+" tab -----
    tabs = ""
    for a in disk_accounts:
        aname = str(a.get("name") or "default")
        active = "tab-active" if str(aname).strip().lower() == str(loaded_account).strip().lower() else ""
        badge = ""
        tk = credential_path(
            Path(__file__).resolve().parent,
            aname,
            a.get("token"),
            "token.json",
        )
        if tk.is_file():
            badge = '<span class="tab-dot" style="background:var(--green);"></span>'
        elif not a.get("enabled", True):
            badge = '<span class="tab-dot" style="background:var(--yellow);"></span>'
        else:
            badge = '<span class="tab-dot" style="background:var(--muted);"></span>'
        label = str(aname)
        if len(label) > 18:
            label = label[:17] + "…"
        tabs += (f'<a class="tab {active}" '
                 f'href="/?account={quote(str(aname))}&platform={platform}" '
                 f'style="display:inline-flex;align-items:center;gap:6px;">{badge}{_esc(label)}</a>')
    tabs += (
        '<form action="/api/accounts/add" method="POST" style="display:inline;">'
        + f'<input type="hidden" name="platform" value="{platform}">'
        + '<button class="tab tab-add" type="submit" title="Add account">+</button></form>'
    )

    # -------- per-account details (active tab) --------
    connected_html = ""
    if st["connected_channel"]:
        connected_html = f'<span class="badge ok">🔗 {_esc(st["connected_channel"])}</span>'
    elif st["connected"]:
        connected_html = '<span class="badge ok">🔗 connected</span>'
    else:
        connected_html = '<span class="badge">⬜ not connected</span>'

    base_dir = Path(__file__).resolve().parent
    acc_cs = str(
        credential_path(base_dir, loaded_account, loaded_acc.get("client_secret"), "client_secret.json")
    )
    acc_tk = str(
        credential_path(base_dir, loaded_account, loaded_acc.get("token"), "token.json")
    )
    tab_secret_present = Path(acc_cs).is_file()
    cookie_path = _shared_cookie_path()
    try:
        cookie_valid = (
            cookie_path.is_file()
            and cookie_path.stat().st_size <= MAX_COOKIE_BYTES
            and _valid_youtube_cookie_export(cookie_path.read_bytes())
        )
    except OSError:
        cookie_valid = False
    cookie_status = (
        f"✅ Authenticated viewer cookies ready: {_esc(cookie_path)}"
        if cookie_valid
        else "❌ No valid authenticated YouTube viewer cookies are installed"
    )
    yt_status = (f'<div style="white-space:pre-line;">'
                 f"{'✅' if tab_secret_present else '❌'} client_secret.json {'present' if tab_secret_present else 'MISSING - upload it below'}\n"
                 f"{'✅' if st['connected'] else '❌'} {'token.json present — live validity is checked at scheduler start' if st['connected'] else 'not connected yet - press Connect'}\n"
                 f"📁 this tab: {_esc(acc_cs)}\n"
                 f"🔑 this tab: {_esc(acc_tk)}</div>")

    # source channels textarea value
    chans_value = "\n".join(st["channels"])

    order_options = ""
    for val, lbl in [("", "order: global"), ("newest", "newest first"), ("oldest", "oldest first"), ("random", "random")]:
        sel = " selected" if str(st["selection_order"] or "") == val else ""
        order_options += f'<option value="{val}"{sel}>{lbl}</option>'

    timezone_options = '<option value="">24/7 (no posting window)</option>'
    for timezone_label, timezone_key in US_TIMEZONES:
        selected = " selected" if str(acc_settings["posting_timezone"]) == timezone_key else ""
        timezone_options += (
            f'<option value="{_esc(timezone_key)}"{selected}>{_esc(timezone_label)}</option>'
        )

    fin = _finished_files()
    fin_html = '<div class="empty">No finished Shorts yet.</div>'
    if fin:
        rows = "".join(f'<div class="track"><span>🎬 <a href="/download-short/{quote(f["name"])}" target="_blank" style="color:var(--cyan);text-decoration:none;">{_esc(f["name"])}</a></span>'
                       f'<span class="sz">{f["size_mb"]} MB · {_esc(f["modified"])}</span></div>' for f in fin)
        fin_html = f'<div class="hint">Each Short has a matching .txt with its generated title &amp; hashtags.</div>{rows}'

    log_lines = "".join(f"<div>{_esc(l)}</div>" for l in _tail_log(80))


    # -------- per-platform settings (clip length + titles) for this tab -----
    pset = {
        field: str(loaded_acc.get(f"{platform}_{field}") or "")
        for field in PLATFORM_TEXT_FIELDS
    }
    pset["clip_seconds"] = str(loaded_acc.get(f"{platform}_clip_seconds") or "")
    pset["clip_placeholder"] = f"inherits {DEFAULT_CLIP_SECONDS:g}s"
    clip_hint = (
        "How long THIS platform's clip should be. Each length gets its own "
        "moment selection and render, so YouTube can post 20s while TikTok "
        "posts its own best 60s cut. Leave blank to use the shared length. "
        "Over 60s is no longer a YouTube Short and uploads as a normal video."
    )
    platform_settings_card = f"""
    <div class="card" style="margin-top:16px;">
      <h2 style="font-size:14px;">🎛 {PLATFORM_LABELS[platform]} settings</h2>
      <div class="hint">These apply to <b>{PLATFORM_LABELS[platform]} only</b>. Leave a field empty
        to inherit the account-wide value from ⚙️ Settings below.</div>
      <form action="/api/platform-settings/save" method="POST">
        {csrf_input}
        <input type="hidden" name="account" value="{_esc(loaded_account)}">
        <input type="hidden" name="platform" value="{platform}">
        <table style="width:100%;font-size:13px;border-collapse:collapse;margin-top:6px;">
          <tr><td style="padding:4px 0;width:38%;">Title prefix</td>
              <td><input type="text" name="{platform}_title_prefix" value="{_esc(pset["title_prefix"])}" placeholder="inherits the shared prefix" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Title hashtags</td>
              <td><input type="text" name="{platform}_title_hashtags" value="{_esc(pset["title_hashtags"])}" placeholder="inherits the shared hashtags" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Extra hashtags</td>
              <td><input type="text" name="{platform}_extra_hashtags" value="{_esc(pset["extra_hashtags"])}" placeholder="inherits the shared hashtags" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Description</td>
              <td><input type="text" name="{platform}_custom_description" value="{_esc(pset["custom_description"])}" placeholder="inherits the shared description" style="width:100%;"></td></tr>
        </table>
        <div class="row" style="margin-top:10px;"><button type="submit">Save {PLATFORM_LABELS[platform]} settings</button></div>
      </form>
    </div>
    """

    youtube_card = f"""
    <div class="card" style="margin-top:16px;">
      <h2 style="font-size:14px;">🔑 Credentials</h2>
      <form action="/api/client-secret" method="POST" enctype="multipart/form-data">
        <input type="hidden" name="account" value="{_esc(loaded_account)}">
        <div class="row">
          <input type="file" name="file" accept=".json" style="flex:1;">
          <button class="cyan" type="submit">Upload client_secret.json</button>
        </div>
      </form>
      <div class="hint" style="border:1px solid var(--border);border-radius:8px;padding:8px;margin-top:8px;">
        Saves into <b>accounts/{_esc(loaded_account.lower())}/client_secret.json</b> — ONLY for
        this tab. The shared <b>client_secret.json</b> in the bot folder is only a fallback.
      </div>
      <form action="/api/test-youtube" method="POST" style="margin-top:8px;">
        <input type="hidden" name="account" value="{_esc(loaded_account)}">
        <button class="green" type="submit">Connect / Test YouTube</button>
      </form>
      <div class="hint" id="ytStatus">{yt_status}</div>
      <div class="hint" style="border:1px solid var(--border);border-radius:8px;padding:8px;margin-top:8px;">
        <b>📌 Each account connects with its own Google login</b> (its own channel).
        Sign in with the Google account of the channel this tab should upload to.
        Tip: for 10 uploads/day per channel, use a SEPARATE Google Cloud project
        + client_secret.json per channel. Keep every OAuth consent screen
        <b>Published / In production</b>, not Testing. Before the scheduler starts,
        the bot forces a token refresh and checks the destination channel. If the
        token must be replaced, startup is blocked and this panel tells you which
        account must use Connect / Test YouTube. Google does not expose an exact
        Testing-mode seven-day countdown to the bot.
      </div>

      <div style="border-top:1px solid var(--border);margin-top:16px;padding-top:14px;">
        <h2 style="font-size:14px;">🔞 Age-restricted source access</h2>
        <div class="hint" style="margin-bottom:8px;">{cookie_status}</div>
        <form action="/api/youtube-cookies" method="POST" enctype="multipart/form-data">
          <div class="row">
            <input type="file" name="file" accept=".txt,text/plain" style="flex:1;" required>
            <button class="cyan" type="submit">Upload cookies.txt</button>
          </div>
        </form>
        <form action="/api/youtube-cookies/delete" method="POST" style="margin-top:8px;">
          <button class="gray" type="submit">Remove viewer cookies</button>
        </form>
        <div class="hint" style="border:1px solid var(--border);border-radius:8px;padding:8px;margin-top:8px;">
          Export a <b>Netscape cookies.txt</b> while signed in at youtube.com with a
          Google account whose age is verified as 18+. This login is only for
          reading source videos; it is separate from upload OAuth and is shared by
          both bots. Cookies are private credentials and may expire, so refresh
          them when age-gated downloads fail again. This supports mature content
          that YouTube permits; it does not bypass Community Guidelines, copyright,
          private videos, or regional blocks.
        </div>
      </div>
    </div>
    """

    # Only the active platform tab's connection panel is rendered; the
    # account settings and source channels below stay shared.
    platform_panel = {
        "youtube": youtube_card,
        "tiktok": tiktok_card,
        "bilibili": bilibili_card,
    }[platform] + platform_settings_card

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🔁 Shorts Repost Bot</title>
<style>
  :root {{ --bg:#0d0d12; --card:#16161d; --card2:#1d1d27; --text:#eef0f5; --muted:#9aa0ae;
    --pink:#fe2c55; --cyan:#25f4ee; --yellow:#ffd60a; --green:#30d158; --red:#ff453a; --border:#26262f; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
    padding:24px 16px 60px; max-width:1100px; margin:0 auto; }}
  h1 {{ font-size:24px; }} .sub {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  .badges {{ margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  .badge {{ font-size:12px; font-weight:700; padding:4px 12px; border-radius:999px; background:var(--card2);
    border:1px solid var(--border); color:var(--muted); }}
  .badge.ok {{ color:var(--green); border-color:var(--green); }} .badge.warn {{ color:var(--yellow); border-color:var(--yellow); }}
  .badge.run {{ color:var(--cyan); border-color:var(--cyan); }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-top:20px; }}
  .stat {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:16px; }}
  .stat .num {{ font-size:26px; font-weight:800; margin-top:6px; }} .stat .lbl {{ font-size:12px; color:var(--muted); text-transform:uppercase; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:18px; margin-top:20px; }}
  .card h2 {{ font-size:16px; margin-bottom:12px; }}
  button {{ background:var(--pink); color:#fff; border:0; border-radius:10px; padding:10px 16px; font-size:14px; font-weight:700; cursor:pointer; }}
  button:hover {{ filter:brightness(1.12); }} button.cyan {{ background:var(--cyan); color:#04161a; }}
  button.gray {{ background:var(--card2); border:1px solid var(--border); color:var(--text); }} button.green {{ background:var(--green); color:#04120a; }}
  button.red {{ background:var(--red); }}
  .row {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }}
  input[type=text], input[type=password], input[type=time], input[type=number], textarea, select {{ background:var(--card2); border:1px solid var(--border); color:var(--text);
    border-radius:10px; padding:10px 12px; font-size:14px; }}
  input[type=text] {{ flex:1; min-width:200px; }} input[type=file] {{ color:var(--muted); font-size:13px; }}
  .hint {{ font-size:12px; color:var(--muted); margin-top:8px; line-height:1.5; }}
  pre.logs {{ background:#0a0a0f; border:1px solid var(--border); border-radius:10px; padding:12px; font-size:11.5px;
    line-height:1.55; font-family:Consolas,Menlo,monospace; white-space:pre-wrap; word-break:break-word;
    max-height:380px; overflow-y:auto; color:#c9cdd8; }}
  .track {{ display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid var(--border); font-size:13px; }}
  .track:last-child {{ border-bottom:0; }} .track .sz {{ color:var(--muted); }}
  .empty {{ color:var(--muted); font-size:13px; padding:8px 0; }}
  /* ---- TABS (Chrome-like) ---- */
  .tabbar {{ display:flex; gap:4px; margin-top:18px; overflow-x:auto; padding-bottom:0; border-bottom:2px solid var(--border); }}
  .tab {{ background:var(--card2); border:1px solid var(--border); border-bottom:none; border-radius:10px 10px 0 0;
    padding:10px 16px; font-size:14px; font-weight:600; color:var(--muted); text-decoration:none; white-space:nowrap; }}
  .tab.tab-active {{ background:var(--card); color:var(--cyan); border-color:var(--cyan); }}
  .tab.tab-add {{ background:transparent; border-style:dashed; color:var(--green); font-size:20px; padding:6px 14px; }}
  .tab-dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; }}
  /* ---- PLATFORM TABS (top level) ---- */
  .ptabbar {{ display:flex; gap:8px; margin-top:22px; flex-wrap:wrap; }}
  .ptab {{ display:inline-flex; align-items:center; gap:8px; background:var(--card2);
    border:1px solid var(--border); border-radius:12px; padding:11px 20px; font-size:15px;
    font-weight:700; color:var(--muted); text-decoration:none; }}
  .ptab:hover {{ color:var(--text); }}
  .ptab.ptab-active {{ background:var(--card); color:var(--pink); border-color:var(--pink); }}
  .subtab-label {{ font-size:11px; color:var(--muted); text-transform:uppercase;
    letter-spacing:0.06em; margin:18px 0 6px; }}
</style>
</head>
<body>
  <noscript><div style="background:#ff453a;color:#fff;padding:12px;border-radius:10px;margin-bottom:16px;text-align:center;font-weight:700;">
    ⚠️ JavaScript is DISABLED. The panel still works - all buttons are plain forms. Only live auto-refresh is off.
  </div></noscript>

  <h1>🔁 Shorts Repost Bot</h1>
  <div class="sub">Pick a platform on top, then the channel below it. Each channel is configured separately and they all run together.</div>
  <div class="badges">
    <span class="badge" style="border-color:var(--pink);color:var(--pink);">v7.3 (Sep 3, 2026)</span>
    {mode_badge} {sched_badge} {jobs_badge}
  </div>

  {msg_html}

  <div class="grid">
    <div class="stat"><div class="lbl">Uploads (24h)</div><div class="num" id="sUploads">{uploads_24h}</div></div>
    <div class="stat"><div class="lbl">Shorts made</div><div class="num" id="sShorts">{total_shorts}</div></div>
    <div class="stat"><div class="lbl">R2 storage</div><div class="num" id="sR2">{total_bytes / (1024**3):.2f} GB</div></div>
    <div class="stat"><div class="lbl">Accounts</div><div class="num" id="sAcc">{len(disk_accounts)}</div></div>
  </div>

  <!-- ================== PLATFORM TABS (top level) ================== -->
  <div class="ptabbar">
    {platform_tabs}
  </div>

  <!-- ============ ACCOUNT SUB-TABS (Chrome-like) for that platform ============ -->
  <div class="subtab-label">Channels</div>
  <div class="tabbar">
    {tabs}
  </div>

  <!-- ======================= ACTIVE ACCOUNT TAB ======================= -->
  <div class="card">
    <div class="row" style="justify-content:space-between; flex-wrap:wrap;">
      <h2 style="margin:0;">👤 {_esc(loaded_account)} {connected_html}
        <span style="font-size:12px;color:var(--muted);font-weight:400;"> · {st['uploads']}/{st['max_daily']} uploads today</span>
      </h2>
      <form action="/api/accounts/delete" method="POST" style="display:inline;">
        <input type="hidden" name="account" value="{_esc(loaded_account)}">
        <button class="red" type="submit" onclick="return confirm('Delete this account tab and its settings? OAuth files remain on disk until you remove the account folder manually.');">🗑 Delete this account</button>
      </form>
    </div>

    {platform_panel}

    <div class="card">
      <h2 style="font-size:14px;">⚙️ Settings for this account</h2>
      <form action="/api/account-settings/save" method="POST">
        <input type="hidden" name="account" value="{_esc(loaded_account)}">
        <table style="width:100%;font-size:13px;border-collapse:collapse;margin-top:6px;">
          <tr><td style="padding:4px 0;">Title prefix</td>
              <td><input type="text" name="title_prefix" value="{_esc(acc_settings['title_prefix'])}" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Title hashtags (all go in the title)</td>
              <td><input type="text" name="title_hashtags" value="{_esc(acc_settings['title_hashtags'])}" placeholder="simpsons, homer, bart" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Custom Description</td>
              <td><input type="text" name="custom_description" value="{_esc(acc_settings['custom_description'])}" placeholder="Link in bio!" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Smart Title</td>
              <td><input type="checkbox" name="smart_titles" value="true"{chk(acc_settings['smart_titles'])} style="transform:scale(1.3);"></td></tr>
          <tr><td style="padding:4px 0;">Max uploads / day</td>
              <td><input type="number" name="max_daily_uploads" value="{_esc(acc_settings['max_daily_uploads'])}" min="1" max="30" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Max shorts per channel / cycle</td>
              <td><input type="number" name="max_shorts_per_channel_cycle" value="{_esc(acc_settings['max_shorts_per_channel_cycle'])}" min="1" max="20" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Min minutes between uploads (0 = as fast as possible)</td>
              <td><input type="number" name="min_minutes_between_uploads" value="{_esc(acc_settings['min_minutes_between_uploads'])}" min="0" max="1440" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Automatic posting time zone</td>
              <td><select name="posting_timezone" style="width:100%;">{timezone_options}</select></td></tr>
          <tr><td style="padding:4px 0;">Posting window starts</td>
              <td><input type="time" name="posting_start_time" value="{_esc(acc_settings['posting_start_time'])}" step="60" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Posting window ends</td>
              <td><input type="time" name="posting_end_time" value="{_esc(acc_settings['posting_end_time'])}" step="60" style="width:100%;"><div class="hint">Uses the selected local time with daylight-saving changes. Overnight windows are supported. Choose 24/7 and clear both times to disable.</div></td></tr>
          <tr><td style="padding:4px 0;">Spread uploads across window (YouTube scheduled publishing)</td>
              <td><input type="checkbox" name="spread_uploads_across_window" value="true"{chk(acc_settings['spread_uploads_across_window'])} style="transform:scale(1.3);"><div class="hint">Shorts upload privately at once; YouTube itself publishes them at times spread evenly from now until the window ends - exact posting times even with the PC off. Local min-gap pacing is skipped while on.</div></td></tr>
          <tr><td style="padding:4px 0;">Top watermark (light text at top)</td>
              <td><input type="text" name="top_watermark" value="{_esc(acc_settings['top_watermark'])}" placeholder="e.g. Simpson Pimp" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Top watermark on</td>
              <td><input type="checkbox" name="top_watermark_enabled" value="true"{chk(acc_settings['top_watermark_enabled'])} style="transform:scale(1.3);"></td></tr>
          <tr><td style="padding:4px 0;">Bottom banner text</td>
              <td><input type="text" name="watermark" value="{_esc(acc_settings['watermark'])}" placeholder="Like &amp; Subscribe" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Bottom banner on</td>
              <td><input type="checkbox" name="watermark_enabled" value="true"{chk(acc_settings['watermark_enabled'])} style="transform:scale(1.3);"></td></tr>
          <tr><td style="padding:4px 0;">Aspect ratio</td>
              <td><select name="aspect" style="width:100%;">
                    <option value="auto"{' selected' if str(acc_settings['aspect']) == 'auto' else ''}>auto (like the source video)</option>
                    <option value="3:4"{' selected' if str(acc_settings['aspect']) == '3:4' else ''}>3:4 (reference style)</option>
                    <option value="9:16"{' selected' if str(acc_settings['aspect']) == '9:16' else ''}>9:16 (classic Shorts)</option></select></td></tr>
          <tr><td style="padding:4px 0;">Fill mode</td>
              <td><select name="fill" style="width:100%;">
                    <option value="blur"{' selected' if acc_settings['fill'] == 'blur' else ''}>Blur (nothing cut)</option>
                    <option value="crop"{' selected' if acc_settings['fill'] == 'crop' else ''}>Crop to fill</option></select></td></tr>
          <tr><td style="padding:4px 0;">Delete local copy after upload</td>
              <td><input type="checkbox" name="delete_after_upload" value="true"{chk(acc_settings['delete_after_upload'])} style="transform:scale(1.3);"></td></tr>
          <tr><td style="padding:4px 0;">Delete R2 backup after upload</td>
              <td><input type="checkbox" name="delete_r2_after_upload" value="true"{chk(acc_settings['delete_r2_after_upload'])} style="transform:scale(1.3);"></td></tr>
          <tr><td style="padding:4px 0;">Burn subtitles (render)</td>
              <td><input type="hidden" name="subtitles_enabled" value="false"><input type="checkbox" name="subtitles_enabled" value="true"{chk(acc_settings['subtitles_enabled'])} style="transform:scale(1.3);"></td></tr>
          <tr><td style="padding:4px 0;">Expected channel (safety lock - uploads blocked if the connected login's channel differs)</td>
              <td><input type="text" name="expected_channel" value="{_esc(acc_settings['expected_channel'])}" placeholder="e.g. PeterAKing" style="width:100%;"></td></tr>
          <tr><td style="padding:4px 0;">Bot cycle interval (hours, whole bot)</td>
              <td><input type="number" name="cycle_interval_hours" value="{_esc(acc_settings['cycle_interval_hours'])}" min="1" style="width:100%;"></td></tr>
        </table>
        <div class="row" style="margin-top:10px;"><button type="submit">Save Account Settings</button></div>
      </form>
    </div>

    <div class="card">
      <h2 style="font-size:14px;">📁 Source channels for this account</h2>
      <div class="hint">Channels this account downloads FROM (one per line). To this account's channel = where it uploads.</div>
      <form action="/api/accounts/save" method="POST">
        <input type="hidden" name="acc_name_0" value="{_esc(loaded_account)}">
        <textarea name="acc_channels_0" rows="3" style="width:100%;background:var(--card2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px;font-size:13px;">{_esc(chans_value)}</textarea>
        <div class="row" style="margin-top:10px;">
          <input type="number" name="acc_maxdaily_0" value="{st['max_daily']}" min="1" max="30" style="width:90px;" title="Max uploads per day">
          <label style="font-size:12px;color:var(--muted);">uploads/day</label>
          <select name="acc_processmode_0" style="background:var(--card2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px;">
            <option value="copy"{' selected' if st['process_mode'] == 'copy' else ''}>copy (keep original - NO watermark)</option>
            <option value="render"{' selected' if st['process_mode'] == 'render' else ''}>render (subtitles+watermark)</option>
          </select>
          <label style="font-size:12px;color:var(--muted);">mode</label>
          <select name="acc_order_0" style="background:var(--card2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px;">{order_options}</select>
          <label style="font-size:12px;color:var(--muted);">order</label>
          <label style="font-size:13px;color:var(--muted);"><input type="checkbox" name="acc_enabled_0" value="true"{' checked' if st['enabled'] else ''}> enabled</label>
          <button type="submit">Save Source</button>
        </div>
      </form>
    </div>
  </div>

  <div class="card">
    <h2>▶ Run the bot</h2>
    <div class="row">
      <form action="/api/run-once" method="POST" style="display:inline;"><button class="cyan" type="submit">Run One Cycle Now</button></form>
      <form action="/api/scheduler/start" method="POST" style="display:inline;"><button class="green" type="submit">Start 24/7 Scheduler</button></form>
      <form action="/api/scheduler/stop" method="POST" style="display:inline;"><button class="gray" type="submit">Stop Scheduler</button></form>
    </div>
    <div class="hint">Runs ALL enabled accounts (each with its own settings/quota). Start first performs a live OAuth refresh + channel check and refuses to run if any active account needs reconnection.</div>
  </div>

  <div class="card">
      <h2>🔗 Repost one specific Short</h2>
    <form action="/api/process-url" method="POST">
      <div class="row">
        <input type="text" name="url" placeholder="Paste a YouTube Short URL..." required>
        <select name="account" required>
          {''.join(f'<option value="{_esc(a["name"])}"{" selected" if str(a["name"]).casefold() == str(loaded_account).casefold() else ""}>{_esc(a["name"])}</option>' for a in disk_accounts)}
        </select>
        <button class="cyan" type="submit">Repost This Short</button>
      </div>
    </form>
  </div>



  <div class="card">
    <h2>📁 Finished Shorts</h2>
    <div>{fin_html}</div>
  </div>

  <div class="card">
    <h2>📜 Logs <span style="font-size:12px;color:var(--muted);">(auto-refresh: on if JS works)</span></h2>
    <pre class="logs" id="logBox">{log_lines}</pre>
  </div>

<script>
  var lastLogCount = -1;
  function esc(s) {{ var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }}
  function refreshStatus() {{
    if (typeof fetch === 'undefined') return;
    fetch('/api/status').then(function(r) {{ return r.json(); }}).then(function(s) {{
      var e1 = document.getElementById('sUploads'); if (e1) e1.textContent = s.uploads_24h;
      var e2 = document.getElementById('sShorts'); if (e2) e2.textContent = s.total_shorts;
      var e3 = document.getElementById('sR2'); if (e3) e3.textContent = s.r2_gb + ' GB';
      var e4 = document.getElementById('sAcc'); if (e4) e4.textContent = (s.accounts||[]).length;
    }}).catch(function() {{}});
  }}
  function refreshLogs() {{
    if (typeof fetch === 'undefined') return;
    fetch('/api/logs?lines=120').then(function(r) {{ return r.json(); }}).then(function(d) {{
      var box = document.getElementById('logBox');
      if (!box || !d.lines || d.lines.length === lastLogCount) return;
      lastLogCount = d.lines.length;
      var html = '';
      for (var i = 0; i < d.lines.length; i++) {{
        var l = d.lines[i]; var cls = '';
        if (l.toLowerCase().indexOf('[error]') !== -1) cls = 'color:var(--red);';
        else if (l.toLowerCase().indexOf('[warning]') !== -1) cls = 'color:var(--yellow);';
        html += '<div style="' + cls + '">' + esc(l) + '</div>';
      }}
      box.innerHTML = html;
      box.scrollTop = box.scrollHeight;
    }}).catch(function() {{}});
  }}
  refreshStatus(); refreshLogs();
  setInterval(refreshStatus, 5000);
  setInterval(refreshLogs, 3000);
</script>
</body>
</html>"""
    if csrf_input:
        html = re.sub(
            r'(<form\b[^>]*method="POST"[^>]*>)',
            lambda match: match.group(1) + csrf_input,
            html,
            flags=re.IGNORECASE,
        )
    return html


def _scheduler_worker() -> None:
    global _scheduler_thread, _scheduler_instance
    sched = ShortsRepostScheduler()
    _scheduler_instance = sched
    try:
        sched.start_24_7_loop()
    except Exception as e:
        logger.error(f"[webui] 24/7 scheduler thread ended with error: {e}")
    finally:
        _scheduler_instance = None
        _scheduler_thread = None


def run_webui(host: str = WEBUI_HOST, port: int = WEBUI_PORT) -> None:
    public_bind = str(host).strip() not in {"127.0.0.1", "localhost", "::1"}
    if public_bind and not WEBUI_PASSWORD:
        raise RuntimeError(
            "Refusing to expose the control panel without WEBUI_PASSWORD. "
            "Use host 127.0.0.1 for local access or configure a strong password."
        )
    logger.info("Web control panel starting on http://%s:%s", host, port)
    print(f"\n  Shorts Repost Bot Control Panel: http://{host}:{port}\n")
    app = create_app()
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
