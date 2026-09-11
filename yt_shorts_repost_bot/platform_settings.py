"""Per-destination (YouTube / TikTok / Bilibili) account settings.

Every platform can override a handful of account-level settings so one channel
can post a 20-second cut to YouTube and a 60-second cut to TikTok, each with
its own title prefix, hashtags and description.

Storage layout inside an account entry in ``accounts.json``::

    {
      "name": "Channel 1",
      "title_prefix": "shared prefix",          # account-wide fallback
      "youtube_clip_seconds": 20,
      "tiktok_clip_seconds": 60,
      "tiktok_title_prefix": "TikTok only",     # per-platform override
      ...
    }

Resolution order is always: per-platform value -> account-wide value ->
built-in default. An empty string or ``None`` means "not set", so clearing a
field in the control panel falls back to the shared value rather than posting
an empty title.
"""
from __future__ import annotations

from typing import Any, Optional

try:  # The clip bot cuts moments out of long videos and has a clip length.
    from .config import CLIP_DURATION_SEC, clamp_clip_duration
except ImportError:  # The repost bot reposts whole Shorts: no window to size.
    CLIP_DURATION_SEC = 0.0

    def clamp_clip_duration(value, default: float = 0.0) -> float:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return float(default)

PLATFORM_YOUTUBE = "youtube"
PLATFORM_TIKTOK = "tiktok"
PLATFORM_BILIBILI = "bilibili"
PLATFORMS = (PLATFORM_YOUTUBE, PLATFORM_TIKTOK, PLATFORM_BILIBILI)

# Social destinations that the cross-poster handles (YouTube is the main
# upload path, not a cross-post destination).
SOCIAL_PLATFORMS = (PLATFORM_TIKTOK, PLATFORM_BILIBILI)

PLATFORM_LABELS = {
    PLATFORM_YOUTUBE: "YouTube",
    PLATFORM_TIKTOK: "TikTok",
    PLATFORM_BILIBILI: "Bilibili",
}

# Settings a platform may override. Keys are the account-wide field names.
OVERRIDABLE_TEXT_FIELDS = (
    "title_prefix",
    "title_hashtags",
    "extra_hashtags",
    "custom_description",
)

# Per-platform field name = "<platform>_<field>".
CLIP_SECONDS_FIELD = "clip_seconds"


def is_platform(value) -> bool:
    return str(value or "").strip().lower() in PLATFORMS


def platform_field(platform: str, field: str) -> str:
    """Account-entry key holding ``field`` for ``platform``."""
    return f"{str(platform).strip().lower()}_{field}"


def platform_enabled(account: Optional[dict], platform: str) -> bool:
    """Is this destination switched on for the account?

    YouTube is the primary upload target and is always considered on; the
    social destinations opt in with their own ``*_enabled`` flag.
    """
    platform = str(platform or "").strip().lower()
    if platform == PLATFORM_YOUTUBE:
        return True
    if not account:
        return False
    return bool(account.get(f"{platform}_enabled"))


def platform_setting(
    account: Optional[dict],
    platform: str,
    field: str,
    default: Any = "",
) -> Any:
    """Per-platform override, else the account-wide value, else ``default``."""
    account = account or {}
    specific = account.get(platform_field(platform, field))
    if specific is not None and str(specific).strip() != "":
        return specific
    shared = account.get(field)
    if shared is not None and str(shared).strip() != "":
        return shared
    return default


def clip_seconds_for(account: Optional[dict], platform: str) -> float:
    """Clip length for one destination, clamped to the supported range.

    Falls back to the account-wide ``clip_seconds`` and finally to the global
    ``CLIP_DURATION_SEC``, so an account that never touches these settings
    keeps its current behaviour exactly.
    """
    raw = platform_setting(account, platform, CLIP_SECONDS_FIELD, None)
    if raw is None:
        return float(CLIP_DURATION_SEC)
    return clamp_clip_duration(raw, default=CLIP_DURATION_SEC)


def platform_text_settings(account: Optional[dict], platform: str) -> dict:
    """Resolved title/hashtag/description settings for one destination."""
    return {
        field: str(platform_setting(account, platform, field, "") or "").strip()
        for field in OVERRIDABLE_TEXT_FIELDS
    }


def platform_metadata(
    account: Optional[dict],
    platform: str,
    metadata: Optional[dict],
) -> dict:
    """Apply a platform's title/hashtag overrides to rendered metadata.

    ``metadata`` is what the YouTube uploader produced (title + tags). When a
    platform defines its own prefix or hashtags, they replace the shared ones
    for that destination only; otherwise the metadata passes through unchanged.
    """
    base = dict(metadata or {})
    settings = platform_text_settings(account, platform)
    shared = {
        field: str((account or {}).get(field) or "").strip()
        for field in OVERRIDABLE_TEXT_FIELDS
    }

    title = str(base.get("title") or "").strip()
    prefix = settings["title_prefix"]
    shared_prefix = shared["title_prefix"]
    if prefix and prefix != shared_prefix:
        # Swap the shared prefix out for this platform's own.
        if shared_prefix and title.startswith(shared_prefix):
            title = title[len(shared_prefix):].strip()
        title = f"{prefix} {title}".strip()
    base["title"] = title

    tags = [str(tag).strip().lstrip("#") for tag in (base.get("tags") or []) if str(tag).strip()]
    own_tags: list[str] = []
    for field in ("title_hashtags", "extra_hashtags"):
        if settings[field] and settings[field] != shared[field]:
            own_tags.extend(
                part.strip().lstrip("#")
                for part in settings[field].replace(",", " ").split()
                if part.strip().lstrip("#")
            )
    if own_tags:
        shared_tags = set()
        for field in ("title_hashtags", "extra_hashtags"):
            shared_tags.update(
                part.strip().lstrip("#").casefold()
                for part in shared[field].replace(",", " ").split()
                if part.strip()
            )
        # Drop the account-wide hashtags this platform is replacing.
        tags = [tag for tag in tags if tag.casefold() not in shared_tags]
        for tag in own_tags:
            if tag.casefold() not in {t.casefold() for t in tags}:
                tags.append(tag)
    base["tags"] = tags

    description = settings["custom_description"]
    if description:
        base["custom_description"] = description
    return base


def render_groups(account: Optional[dict]) -> dict[float, list[str]]:
    """Group the enabled destinations by the clip length they need.

    Destinations sharing a length share one render pass; a destination with a
    different length gets its own pass (its own best moment at that length).
    """
    groups: dict[float, list[str]] = {}
    for platform in PLATFORMS:
        if not platform_enabled(account, platform):
            continue
        groups.setdefault(clip_seconds_for(account, platform), []).append(platform)
    return groups
