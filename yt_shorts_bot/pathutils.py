"""Portable and traversal-safe account/file path helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath

_SAFE_CHARS = re.compile(r"[^a-zA-Z0-9._ -]+")
_WINDOWS_ABSOLUTE = re.compile(r"^[a-zA-Z]:[\\/]")


def safe_account_slug(name: str) -> str:
    """Return a stable folder component that cannot escape ``accounts/``."""
    raw = str(name or "default").strip().lower()
    raw = raw.replace("/", " ").replace("\\", " ")
    raw = _SAFE_CHARS.sub("", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    return raw[:80] or "default"


def is_foreign_windows_absolute(value: str) -> bool:
    return os.name != "nt" and bool(_WINDOWS_ABSOLUTE.match(str(value or "")))


def credential_path(
    base_dir: Path,
    account_name: str,
    value: str | Path | None,
    filename: str,
) -> Path:
    """
    Resolve a credential path without turning ``F:\\...`` into a bogus relative
    POSIX path after moving the project from Windows to Linux.
    """
    base_dir = Path(base_dir).resolve()
    fallback = base_dir / "accounts" / safe_account_slug(account_name) / filename
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if is_foreign_windows_absolute(raw):
        # Preserve only the basename; the original drive cannot exist here.
        win_name = PureWindowsPath(raw).name
        return fallback.with_name(win_name or filename)
    path = Path(raw)
    return path if path.is_absolute() else base_dir / path


def relative_credential_value(account_name: str, filename: str) -> str:
    """Portable value to persist in accounts.json (always project-relative)."""
    return (Path("accounts") / safe_account_slug(account_name) / filename).as_posix()
