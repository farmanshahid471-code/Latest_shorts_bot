"""
config.py - Configuration for the YouTube Shorts REPOST bot.
Scans channels, downloads their Shorts, optionally re-renders them,
and uploads them to YOUR channel 24/7.
"""
import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List
from dotenv import load_dotenv

from .pathutils import credential_path

# Load .env next to this file (works no matter which folder we are launched from)
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE, override=False)
else:
    load_dotenv()

# --- PROJECT DIRECTORIES ---
BASE_DIR = Path(__file__).resolve().parent


def _resolve_path(value) -> Path:
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


TEMP_DIR = _resolve_path(os.getenv("TEMP_DIR", BASE_DIR / "temp_clips"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = _resolve_path(os.getenv("DB_PATH", BASE_DIR / "bot_state.db"))
LOG_FILE = _resolve_path(os.getenv("LOG_FILE", BASE_DIR / "shorts_repost.log"))

# --- TARGET SELECTION (shorts-focused channels) ---
_channels_env = os.getenv("TARGET_CHANNELS", "")
TARGET_CHANNELS: List[str] = [c.strip() for c in _channels_env.split(",") if c.strip()]

# How many newest Shorts to inspect per channel per cycle
FETCH_LIMIT_PER_CHANNEL: int = int(os.getenv("FETCH_LIMIT_PER_CHANNEL", "20"))

# How deep the channel listing scan goes when selection order is "oldest" or
# "random". YouTube tabs arrive newest-first and no longer support server-side
# sorting, so "oldest" needs a deeper window before reversing - otherwise it
# would only ever see the newest N and never reach the real backlog.
FETCH_SCAN_LIMIT: int = max(10, min(1000, int(os.getenv("FETCH_SCAN_LIMIT", "300"))))

# Per-network-call timeout for yt-dlp (seconds). Without it a stalled connection
# can hang a cycle for minutes with NO log output (looks frozen).
YTDL_SOCKET_TIMEOUT_SEC: float = float(os.getenv("YTDL_SOCKET_TIMEOUT_SEC", "25"))

# Max number of Shorts to repost from ONE channel in a single cycle
# (spreads uploads across channels; total is still capped by MAX_DAILY_UPLOADS)
MAX_SHORTS_PER_CHANNEL_CYCLE: int = int(os.getenv("MAX_SHORTS_PER_CHANNEL_CYCLE", "2"))

# What counts as a Short (YouTube Shorts are <= 60 seconds)
MAX_SHORT_DURATION_SEC: int = int(os.getenv("MAX_SHORT_DURATION_SEC", "60"))

# --- PROCESSING MODE ---
# "copy"  = download the Short and re-encode it to a clean mp4 (h264+aac),
#           keeping the original look. Fast, recommended for reposting.
# "render" = like the other bot: transcribe + burn viral subtitles + add BGM,
#           fitted to SHORT_ASPECT canvas. Slower.
PROCESS_MODE: str = os.getenv("PROCESS_MODE", "copy").strip().lower()
if PROCESS_MODE not in ("copy", "render"):
    PROCESS_MODE = "copy"

# --- VIDEO / RENDER SETTINGS (used in "render" mode; "copy" mode only uses
# these for the re-encode quality) ---
SHORT_ASPECT: str = os.getenv("SHORT_ASPECT", "9:16").strip().lower()
FILL_MODE: str = os.getenv("FILL_MODE", "blur").strip().lower()

if SHORT_ASPECT == "3:4":
    VERTICAL_WIDTH: int = 1080
    VERTICAL_HEIGHT: int = 1440
else:
    SHORT_ASPECT = "9:16"
    VERTICAL_WIDTH: int = 1080
    VERTICAL_HEIGHT: int = 1920
ASPECT_RATIO_EXPRESSION: str = SHORT_ASPECT

VIDEO_CRF: int = int(os.getenv("VIDEO_CRF", "17"))
VIDEO_PRESET: str = os.getenv("VIDEO_PRESET", "slow")
AUDIO_BITRATE: str = os.getenv("AUDIO_BITRATE", "192k")
FFMPEG_TIMEOUT_SEC: int = max(60, int(os.getenv("FFMPEG_TIMEOUT_SEC", "900")))

# Copy-mode quality: Shorts are already compressed, so a lighter CRF keeps
# files reasonable while staying visually identical (default 21).
VIDEO_CRF_COPY: int = int(os.getenv("VIDEO_CRF_COPY", "21"))

# --- SUBTITLES (render mode) ---
WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "base")
WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "auto").strip().lower()
WHISPER_DEVICE: str = "cpu"
WHISPER_COMPUTE_TYPE: str = "int8"
MAX_WORDS_PER_SUBTITLE_LINE: int = int(os.getenv("MAX_WORDS_PER_SUBTITLE_LINE", "4"))
SUBTITLE_MAX_DURATION_SEC: float = 2.0

SUBTITLE_STYLE_MODE: str = os.getenv("SUBTITLE_STYLE_MODE", "viral")
VIRAL_WORDS_PER_LINE: int = int(os.getenv("VIRAL_WORDS_PER_LINE", "2"))
SUBTITLE_UPPERCASE: bool = os.getenv("SUBTITLE_UPPERCASE", "true").lower() == "true"

_DEFAULT_FONT = "Arial" if sys.platform.startswith("win") else "DejaVu Sans"
SUBTITLE_FONT_NAME: str = os.getenv("SUBTITLE_FONT_NAME", "").strip() or _DEFAULT_FONT
if sys.platform.startswith("win") and SUBTITLE_FONT_NAME.strip().lower() == "dejavu sans":
    SUBTITLE_FONT_NAME = "Arial"
SUBTITLE_FONT_SIZE: int = int(os.getenv("SUBTITLE_FONT_SIZE", "28"))
SUBTITLE_FORCE_STYLE: str = (
    f"Fontname={SUBTITLE_FONT_NAME},Fontsize={SUBTITLE_FONT_SIZE},Bold=1,"
    "PrimaryColour=&H00FFFFFF,SecondaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=3,Shadow=2,Alignment=2,MarginV=85"
)

# --- BACKGROUND MUSIC (render mode) ---
BGM_DIR: Path = _resolve_path(os.getenv("BGM_DIR", BASE_DIR / "bgm"))
BGM_DIR.mkdir(parents=True, exist_ok=True)
BGM_ENABLED: bool = os.getenv("BGM_ENABLED", "true").lower() == "true"
BGM_VOLUME: float = float(os.getenv("BGM_VOLUME", "0.18"))
VOICE_VOLUME: float = float(os.getenv("VOICE_VOLUME", "1.00"))

# --- LOGO / WATERMARK REMOVAL (render mode) ---
LOGO_REMOVE_ENABLED: bool = os.getenv("LOGO_REMOVE_ENABLED", "false").lower() == "true"
LOGO_POSITION: str = os.getenv("LOGO_POSITION", "top-right").strip().lower()
LOGO_SIZE_PCT: float = float(os.getenv("LOGO_SIZE_PCT", "12"))
LOGO_POSITIONS: tuple = ("top-left", "top-right", "bottom-left", "bottom-right")

# --- "LIKE & SUBSCRIBE" BOTTOM BANNER ---
LIKE_AND_SUBSCRIBE_ENABLED: bool = os.getenv("LIKE_AND_SUBSCRIBE_ENABLED", "true").lower() == "true"
LIKE_AND_SUBSCRIBE_TEXT: str = os.getenv("LIKE_AND_SUBSCRIBE_TEXT", "LIKE & SUBSCRIBE")

# --- TOP CHANNEL WATERMARK (light, in the upper blur band) ---
# A subtle semi-transparent channel name at the top of the video, so it does
# not disturb the viewing experience. Set TOP_WATERMARK_TEXT to your channel
# name (empty = no top watermark).
TOP_WATERMARK_ENABLED: bool = os.getenv("TOP_WATERMARK_ENABLED", "true").lower() == "true"
TOP_WATERMARK_TEXT: str = os.getenv("TOP_WATERMARK_TEXT", "")

# --- KEEP LOCAL COPIES OF EVERY REPOSTED SHORT ---
KEEP_LOCAL_SHORTS: bool = os.getenv("KEEP_LOCAL_SHORTS", "true").lower() == "true"
KEEP_SHORTS_DIR: Path = _resolve_path(os.getenv("KEEP_SHORTS_DIR", BASE_DIR / "finished_shorts"))
KEEP_SHORTS_DIR.mkdir(parents=True, exist_ok=True)

# --- YOUTUBE COOKIES (fixes "Sign in to confirm you're not a bot") ---
YT_COOKIES_FILE: str = os.getenv("YT_COOKIES_FILE", "")
YT_COOKIES_FROM_BROWSER: str = os.getenv("YT_COOKIES_FROM_BROWSER", "")

# --- CLOUD STORAGE (Cloudflare R2, OPTIONAL - leave placeholders for dry-run) ---
R2_ACCOUNT_ID: str = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID: str = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME: str = os.getenv("R2_BUCKET_NAME", "youtube-shorts-reposts")
R2_ENDPOINT_URL: str = os.getenv(
    "R2_ENDPOINT_URL",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
)
R2_MAX_BUCKET_BYTES: int = int(os.getenv("R2_MAX_BUCKET_BYTES", str(8 * 1024 * 1024 * 1024)))

# --- YOUTUBE DATA API V3 SETTINGS ---
YOUTUBE_CLIENT_SECRET_FILE: Path = _resolve_path(os.getenv("YOUTUBE_CLIENT_SECRET_FILE", BASE_DIR / "client_secret.json"))
YOUTUBE_TOKEN_FILE: Path = _resolve_path(os.getenv("YOUTUBE_TOKEN_FILE", BASE_DIR / "token.json"))
YOUTUBE_API_SERVICE_NAME: str = "youtube"
YOUTUBE_API_VERSION: str = "v3"
YOUTUBE_SCOPES: List[str] = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly"
]

# Local rolling upload cap; Google API project quota is enforced separately by YouTube
MAX_DAILY_UPLOADS: int = int(os.getenv("MAX_DAILY_UPLOADS", "10"))
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() == "true"

# --- SCHEDULER SETTINGS ---
CYCLE_INTERVAL_HOURS: int = int(os.getenv("CYCLE_INTERVAL_HOURS", "3"))

# --- VIDEO SELECTION ORDER (which videos to pick from a source channel) ---
# "newest" = most recent first (default) | "oldest" = oldest first | "random" = random order.
# The bot NEVER posts the same video twice to the same account (tracked in bot_state.db).
SELECTION_ORDER: str = os.getenv("SELECTION_ORDER", "newest").strip().lower()
if SELECTION_ORDER not in ("newest", "oldest", "random"):
    SELECTION_ORDER = "newest"

# --- DELETE AFTER UPLOAD ---
# DELETE_AFTER_UPLOAD = "true" -> delete the local finished_shorts copy (+ .txt
# sidecar) as soon as the video is successfully uploaded to YouTube.
# DELETE_R2_AFTER_UPLOAD = "true" -> also delete the R2 backup after upload.
# Both can be overridden per-account in the Settings panel.
DELETE_AFTER_UPLOAD: bool = os.getenv("DELETE_AFTER_UPLOAD", "false").lower() == "true"
DELETE_R2_AFTER_UPLOAD: bool = os.getenv("DELETE_R2_AFTER_UPLOAD", "false").lower() == "true"

# --- WATERMARK STYLE ---
# Top watermark: PLAIN TEXT only - no background band, no border, no shadow.
#   color "white" (or any ffmpeg color), opacity 0.5 = 50% visible.
TOP_WATERMARK_COLOR: str = os.getenv("TOP_WATERMARK_COLOR", "white")
TOP_WATERMARK_OPACITY: float = float(os.getenv("TOP_WATERMARK_OPACITY", "0.5"))
TOP_WATERMARK_FONT_SIZE: int = int(os.getenv("TOP_WATERMARK_FONT_SIZE", "56"))
TOP_WATERMARK_ITALIC: bool = os.getenv("TOP_WATERMARK_ITALIC", "true").lower() == "true"
# Vertical position of the top watermark as % of screen height.
# The top blur band is roughly the top ~10-30% of the screen, so ~12 = nicely
# inside the top blur area, horizontally centered (x is always centered).
TOP_WATERMARK_Y_PCT: float = float(os.getenv("TOP_WATERMARK_Y_PCT", "12"))
# Bottom banner: PLAIN TEXT only, 100% visible.
BOTTOM_BANNER_FONT_SIZE: int = int(os.getenv("BOTTOM_BANNER_FONT_SIZE", "56"))
BOTTOM_BANNER_OPACITY: float = float(os.getenv("BOTTOM_BANNER_OPACITY", "1.0"))
BOTTOM_BANNER_ITALIC: bool = os.getenv("BOTTOM_BANNER_ITALIC", "true").lower() == "true"
# Vertical position of the bottom text as % of screen height (90 = near bottom)
BOTTOM_BANNER_Y_PCT: float = float(os.getenv("BOTTOM_BANNER_Y_PCT", "90"))
TOP_WATERMARK_BAND: bool = os.getenv("TOP_WATERMARK_BAND", "false").lower() == "true"

# --- USER-CONTROLLED TITLES & HASHTAGS (legacy setting names retained) ---
ENABLE_SMART_TITLES: bool = os.getenv("ENABLE_SMART_TITLES", "true").lower() == "true"
MAX_TITLE_HASHTAGS: int = int(os.getenv("MAX_TITLE_HASHTAGS", "4"))
REACH_HASHTAGS: str = os.getenv("REACH_HASHTAGS", "shorts,viral,fyp,trending")
EXTRA_HASHTAGS: str = os.getenv("EXTRA_HASHTAGS", "")
TITLE_PREFIX: str = os.getenv("TITLE_PREFIX", "")

# --- WEB CONTROL PANEL ---
WEBUI_HOST: str = os.getenv("WEBUI_HOST", "127.0.0.1")
WEBUI_PORT: int = int(os.getenv("WEBUI_PORT", "5100"))
WEBUI_USERNAME: str = os.getenv("WEBUI_USERNAME", "admin")
WEBUI_PASSWORD: str = os.getenv("WEBUI_PASSWORD", "")
WEBUI_SECRET_KEY: str = os.getenv("WEBUI_SECRET_KEY", "")
WEBUI_COOKIE_SECURE: bool = os.getenv("WEBUI_COOKIE_SECURE", "false").lower() == "true"

# --- FFMPEG RESOLUTION (system PATH or bundled yt_shorts_repost_bot/ffmpeg/bin) ---
def _find_binary(name: str):
    env_key = "FFMPEG_PATH" if name == "ffmpeg" else "FFPROBE_PATH"
    env_val = os.getenv(env_key)
    if env_val:
        p = _resolve_path(env_val)
        if p.exists():
            return str(p)
    local = BASE_DIR / "ffmpeg" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    if local.exists():
        return str(local)
    import shutil
    return shutil.which(name)


FFMPEG_PATH = _find_binary("ffmpeg")
FFPROBE_PATH = _find_binary("ffprobe")

if not FFMPEG_PATH:
    logging.getLogger("YTShortsRepost").warning(
        "FFmpeg was not found. Run setup.bat / setup.sh, or put ffmpeg.exe in "
        "yt_shorts_repost_bot/ffmpeg/bin/."
    )


def setup_logging(log_level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("YTShortsRepost")
    logger.setLevel(log_level)
    if logger.handlers:
        return logger
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(module)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        logger.warning(f"Could not initialize rotating file logger: {e}")
    return logger


logger = setup_logging()


# --- MULTI-ACCOUNT SUPPORT ---
# accounts.json (in this folder) lets you run several YouTube channels with
# different source channels, credentials, and quotas:
# {
#   "accounts": [
#     {
#       "name": "Gaming",
#       "client_secret": "accounts/gaming/client_secret.json",
#       "token": "accounts/gaming/token.json",
#       "target_channels": ["https://www.youtube.com/@SomeGamingShorts"],
#       "max_daily_uploads": 10,
#       "process_mode": "copy",
#       "enabled": true
#     }
#   ]
# }
ACCOUNTS_FILE: Path = BASE_DIR / "accounts.json"


def _load_accounts() -> List[dict]:
    import json as _json
    _log = logging.getLogger("YTShortsRepost")

    defaults = {
        "name": "default",
        "client_secret": str(YOUTUBE_CLIENT_SECRET_FILE),
        "token": str(YOUTUBE_TOKEN_FILE),
        "target_channels": TARGET_CHANNELS,
        "max_daily_uploads": MAX_DAILY_UPLOADS,
        "process_mode": PROCESS_MODE,
        "enabled": True,
    }

    if ACCOUNTS_FILE.exists():
        try:
            data = _json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            raw = data.get("accounts", []) if isinstance(data, dict) else []
            accounts = []
            for acc in raw:
                if not isinstance(acc, dict) or not acc.get("name"):
                    continue
                merged = dict(defaults)
                merged.update(acc)
                merged["name"] = str(merged["name"]).strip()
                for key, filename, root_default in (
                    ("client_secret", "client_secret.json", YOUTUBE_CLIENT_SECRET_FILE),
                    ("token", "token.json", YOUTUBE_TOKEN_FILE),
                ):
                    raw_value = str(merged.get(key) or "").strip()
                    if raw_value == str(root_default):
                        raw_value = ""
                    path = credential_path(BASE_DIR, merged["name"], raw_value, filename)
                    if path.is_dir():
                        path = credential_path(BASE_DIR, merged["name"], None, filename)
                    merged[key] = str(path)
                if not str(merged.get("expected_channel") or "").strip() and merged.get("connected_channel"):
                    merged["expected_channel"] = str(merged["connected_channel"]).strip()
                ch = merged.get("target_channels")
                if isinstance(ch, str):
                    merged["target_channels"] = [c.strip() for c in ch.split(",") if c.strip()]
                elif isinstance(ch, list):
                    merged["target_channels"] = [str(c).strip() for c in ch if str(c).strip()]
                else:
                    merged["target_channels"] = defaults["target_channels"]
                merged["enabled"] = bool(merged.get("enabled", True))
                merged["max_daily_uploads"] = int(merged.get("max_daily_uploads") or MAX_DAILY_UPLOADS)
                pm = str(merged.get("process_mode") or PROCESS_MODE).strip().lower()
                merged["process_mode"] = pm if pm in ("copy", "render") else "copy"
                accounts.append(merged)
            # Dedupe by case-insensitive name: duplicate names would make the
            # scheduler run the same channel twice and the panel merge tabs.
            seen, unique_accounts = set(), []
            for a in accounts:
                k = str(a.get("name") or "").strip().lower()
                if not k or k in seen:
                    continue
                seen.add(k)
                unique_accounts.append(a)
            accounts = unique_accounts
            if accounts:
                _log.info(f"Loaded {len(accounts)} account(s) from accounts.json: "
                          + ", ".join(a["name"] for a in accounts))
                return accounts
        except Exception as e:
            _log.error(f"Failed to parse accounts.json ({e}). Using default single account.")

    return [defaults]


ACCOUNTS: List[dict] = _load_accounts()
