"""User-controlled title, hashtag, description, transcript, and sidecar helpers.

Legacy keyword-extraction helpers remain for API compatibility, but publishing
uses only the account's explicit ``title_hashtags``/``extra_hashtags`` values.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from .config import TITLE_PREFIX, logger

STOPWORDS = set(
    """a about after all also am an and any are as at be because been before being
    between both but by can could did do does doing down during each few for from
    further had has have having he her here hers herself him himself his how i if
    in into is it its itself just me more most my myself no nor not now of off on
    once only or other our ours ourselves out over own same she should so some
    such than that the their theirs them themselves then there these they this
    those through to too under until up very was we were what when where which
    while who whom why will with you your yours yourself yourselves youtube video
    watch official channel shorts subscribe clip clips like share comment""".split()
)


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _is_stop(word: str) -> bool:
    return word in STOPWORDS or len(word) < 3


def _source_hashtags(*texts: str) -> list[str]:
    """Compatibility helper; source hashtags are not published automatically."""
    output: list[str] = []
    for text in texts:
        for tag in re.findall(r"#([A-Za-z0-9_]{2,})", text or ""):
            tag = tag.lower()
            if tag not in output:
                output.append(tag)
    return output


def extract_keywords(*texts: str, top_n: int = 14) -> list[str]:
    """Compatibility helper for callers that want local keyword analysis."""
    counts: Counter = Counter()
    capitals: Counter = Counter()
    for index, text in enumerate(texts):
        if not text:
            continue
        weight = 3 if index < 4 else 1
        for word in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text):
            normalized = _norm(word)
            if _is_stop(normalized):
                continue
            counts[normalized] += weight
            if word[0].isupper():
                capitals[normalized] += 1
    return sorted(
        counts,
        key=lambda word: counts[word] + (2.0 if capitals[word] else 0.0),
        reverse=True,
    )[:top_n]


def _split_tags(value: str) -> list[str]:
    value = (value or "").replace("#", " ").replace(",", " ")
    return [tag.strip() for tag in value.split() if tag.strip()]


def build_hashtags(
    info: Optional[dict[str, Any]] = None,
    transcript_text: str = "",
    extra_hashtags: str = "",
    top_n: int = 12,
    smart_titles: Optional[bool] = None,
    title_hashtags: str = "",
) -> list[str]:
    """Return only explicit user hashtags; content is never inferred."""
    del info, transcript_text, top_n, smart_titles
    user_tags: list[str] = []
    for tag in _split_tags(title_hashtags or extra_hashtags):
        normalized = _norm(tag)
        if normalized and normalized not in user_tags:
            user_tags.append(normalized)
    logger.debug("User-controlled hashtags: %s", user_tags)
    return user_tags


def make_catchy_title(
    info: Optional[dict[str, Any]] = None,
    transcript_text: str = "",
    extra_hashtags: str = "",
    part_label: str = "",
    max_len: int = 100,
    title_prefix: Optional[str] = None,
    title_hashtags: str = "",
    smart_titles: Optional[bool] = None,
) -> str:
    """Build ``prefix + clean source title + explicit hashtags`` within 100 chars."""
    del transcript_text, extra_hashtags, smart_titles
    info = info or {}
    original = str(info.get("title") or "")
    clean = re.sub(r"#\w+", "", original)
    clean = re.sub(r"[@\[\](){}]", "", clean).strip()
    if len(clean) > 60:
        clean = clean[:57].strip() + "..."

    raw_prefix = (
        title_prefix
        if title_prefix is not None
        else (info.get("title_prefix") or TITLE_PREFIX or "")
    )
    prefix = str(raw_prefix or "").strip()
    if prefix:
        prefix += " "
    if part_label:
        prefix = f"{prefix}{part_label} - "

    user_tags = [tag.lstrip("#") for tag in _split_tags(title_hashtags)]
    tags_text = " ".join("#" + tag for tag in user_tags)
    tags_part = (" " + tags_text) if tags_text else ""
    if len(prefix) + len(clean) + len(tags_part) > max_len:
        available = max_len - len(prefix) - len(tags_part) - 1
        if available >= 10:
            clean = clean[: max(0, available - 1)].rstrip() + "…"
        else:
            available = max_len - len(prefix) - 1
            clean = clean[: max(0, available - 1)].rstrip() + "…"
            tags_part = ""
    result = f"{prefix}{clean}{tags_part}".strip()
    return result if len(result) <= max_len else result[: max_len - 1].rstrip() + "…"


def make_description(
    original_title: str,
    original_url: str = "",
    hashtags: Optional[list[str]] = None,
) -> str:
    """Build a description without exposing the source URL or source hashtags."""
    del original_url
    tag_line = " ".join("#" + tag for tag in (hashtags or []))
    quoted = re.sub(r"#\w+", "", str(original_title or "")).strip()
    return (
        f"🎬 High-engagement highlight clip from: {quoted}\n"
        "💡 Subscribe for daily curated shorts & insights!\n\n"
        f"{tag_line}"
    )


def srt_to_text(srt_path) -> str:
    try:
        path = Path(srt_path)
        if not path.exists():
            return ""
        lines = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or "-->" in line or line.isdigit():
                continue
            lines.append(line)
        return " ".join(lines)
    except (OSError, TypeError):
        return ""


def save_metadata_sidecar(
    short_video_path,
    metadata: dict[str, Any],
    source_url: str = "",
    short_id: str = "",
    account: str = "",
) -> Optional[Path]:
    """Write the exact attempted upload metadata next to a finished video."""
    try:
        path = Path(short_video_path)
        if not path.exists():
            return None
        sidecar = path.with_suffix(".txt")
        lines = [
            "=== SHORT METADATA (exact upload attempt) ===",
            "",
            f"Account       : {account or 'default'}",
            f"Source URL    : {source_url}",
            f"YouTube Short : {short_id or '(not uploaded)'}",
            "",
            f"TITLE         : {metadata.get('title', '')}",
            "",
            "DESCRIPTION:",
            str(metadata.get("description", "")),
            "",
            "TAGS: " + ", ".join(metadata.get("tags", [])),
        ]
        sidecar.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Saved metadata sidecar: %s", sidecar)
        return sidecar
    except (OSError, TypeError) as exc:
        logger.warning("Could not write metadata sidecar: %s", exc)
        return None
