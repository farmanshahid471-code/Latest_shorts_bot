"""Save a finished clip to disk for MANUAL uploading instead of posting it.

Some destinations are not worth automating. Bilibili, for example, only hands
out upload API access to certified mainland-China enterprises, and the
unofficial cookie route risks the account. So the bot can simply *prepare* the
post: it copies the rendered video next to a plain-text file containing the
title, description, tags and every other field the upload form asks for, and
the human pastes them in.

Layout (one folder per account, one pair of files per clip)::

    bilibili_manual/
      My Channel/
        2026-09-13_My-clip-title_abc123.mp4
        2026-09-13_My-clip-title_abc123.txt

The ``.txt`` is deliberately Notepad-friendly: plain UTF-8, CRLF-free, no
Markdown, fields in the order the Bilibili submission page asks for them.
"""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Keep well under the 255-byte limit ext4/NTFS impose, leaving room for the
# date prefix, the video id suffix and the extension.
MAX_TITLE_CHARS = 60

# Windows forbids these outright; the control characters break shells.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Trailing dots/spaces are silently stripped by Windows, which breaks lookups.
_TRAILING_JUNK = re.compile(r"[. ]+$")
# Reserved DOS device names cannot be used as filenames, even with extensions.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(text: str, fallback: str = "clip") -> str:
    """Turn an arbitrary title into something every OS accepts as a filename.

    Unicode is preserved (Chinese titles stay readable) -- only characters that
    are genuinely illegal or hostile to shells are replaced.
    """
    value = unicodedata.normalize("NFC", str(text or "")).strip()
    value = _UNSAFE.sub(" ", value)
    # Collapse runs of whitespace into single dashes for tidy, shell-safe names.
    value = re.sub(r"\s+", "-", value).strip("-")
    value = _TRAILING_JUNK.sub("", value)
    if len(value) > MAX_TITLE_CHARS:
        value = value[:MAX_TITLE_CHARS].rstrip("-")
    if value.upper().split(".")[0] in _RESERVED:
        value = f"_{value}"
    return value or fallback


def _unique_path(path: Path) -> Path:
    """Never overwrite an export the user may not have uploaded yet."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for counter in range(2, 1000):
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem}_{datetime.now():%H%M%S}{suffix}"


def _tag_list(metadata: dict) -> list[str]:
    tags: list[str] = []
    for raw in metadata.get("tags") or []:
        tag = str(raw or "").strip().lstrip("#")
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_notes(
    metadata: Optional[dict],
    *,
    account_name: str = "",
    video_id: str = "",
    video_filename: str = "",
    source_url: str = "",
    platform_label: str = "Bilibili",
    extra_fields: Optional[dict] = None,
) -> str:
    """The human-readable sidecar: everything needed to fill the upload form."""
    meta = metadata or {}
    title = str(meta.get("title") or "").strip() or "New Short"
    description = str(meta.get("description") or "").strip()
    tags = _tag_list(meta)

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"{platform_label} upload — copy each field into the form")
    lines.append("=" * 60)
    lines.append("")
    lines.append("TITLE")
    lines.append(title)
    lines.append("")
    lines.append("DESCRIPTION")
    lines.append(description or title)
    lines.append("")
    lines.append("TAGS (comma separated)")
    lines.append(", ".join(tags) if tags else "(none)")
    lines.append("")
    if tags:
        lines.append("TAGS (hashtag style)")
        # Hashtags cannot contain whitespace ("daily life" -> "#dailylife").
        lines.append(" ".join("#" + re.sub(r"\s+", "", tag) for tag in tags))
        lines.append("")

    for label, value in (extra_fields or {}).items():
        text = str(value or "").strip()
        if text:
            lines.append(str(label).upper())
            lines.append(text)
            lines.append("")

    lines.append("-" * 60)
    lines.append("REFERENCE (not part of the upload form)")
    lines.append(f"Video file   : {video_filename}")
    if account_name:
        lines.append(f"Account      : {account_name}")
    if video_id:
        lines.append(f"Clip id      : {video_id}")
    if source_url:
        lines.append(f"Source video : {source_url}")
    thumbnail = str(meta.get("thumbnail_path") or "").strip()
    if thumbnail and Path(thumbnail).is_file():
        lines.append(f"Cover image  : {thumbnail}")
    lines.append(f"Prepared     : {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append("")
    return "\n".join(lines)


def export_for_manual_upload(
    video_path: Path,
    export_dir: Path,
    metadata: Optional[dict] = None,
    *,
    account_name: str = "",
    video_id: str = "",
    source_url: str = "",
    platform_label: str = "Bilibili",
    extra_fields: Optional[dict] = None,
    copy_cover: bool = True,
) -> tuple[Path, Path]:
    """Copy the clip + write its notes file. Returns (video, notes) paths.

    The video is *copied*, never moved: the pipeline still owns the original and
    deletes it during its normal cleanup.
    """
    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"Rendered video is missing: {source}")

    meta = metadata or {}
    folder = Path(export_dir) / safe_filename(account_name, "account") if account_name \
        else Path(export_dir)
    folder.mkdir(parents=True, exist_ok=True)

    title_part = safe_filename(meta.get("title") or "", "clip")
    id_part = safe_filename(video_id, "")
    stem = f"{datetime.now():%Y-%m-%d}_{title_part}"
    if id_part:
        stem = f"{stem}_{id_part}"

    video_target = _unique_path(folder / f"{stem}{source.suffix or '.mp4'}")
    shutil.copy2(source, video_target)

    # Match the notes name to the FINAL video name, so the pair always lines up
    # even when a duplicate forced a "_2" suffix.
    notes_target = video_target.with_suffix(".txt")
    notes_target.write_text(
        build_notes(
            meta,
            account_name=account_name,
            video_id=video_id,
            video_filename=video_target.name,
            source_url=source_url,
            platform_label=platform_label,
            extra_fields=extra_fields,
        ),
        encoding="utf-8",
    )

    if copy_cover:
        cover = str(meta.get("thumbnail_path") or "").strip()
        if cover and Path(cover).is_file():
            try:
                shutil.copy2(Path(cover), video_target.with_suffix(Path(cover).suffix))
            except OSError as exc:  # A missing cover must never fail the export.
                logger.debug("Could not copy cover image: %s", exc)

    return video_target, notes_target
