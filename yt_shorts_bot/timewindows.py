"""Per-account posting windows with DST-aware US time zones."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Labels represent every US federal time-zone region, including territories,
# plus Arizona's non-DST Mountain time.
US_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("Eastern Time (ET)", "America/New_York"),
    ("Central Time (CT)", "America/Chicago"),
    ("Mountain Time (MT)", "America/Denver"),
    ("Arizona Time (MST, no DST)", "America/Phoenix"),
    ("Pacific Time (PT)", "America/Los_Angeles"),
    ("Alaska Time (AKT)", "America/Anchorage"),
    ("Aleutian Time (HAT)", "America/Adak"),
    ("Hawaii Time (HST, no DST)", "Pacific/Honolulu"),
    ("Atlantic Time (Puerto Rico/USVI)", "America/Puerto_Rico"),
    ("Samoa Time (American Samoa)", "Pacific/Pago_Pago"),
    ("Chamorro Time (Guam/N. Mariana Islands)", "Pacific/Guam"),
)
US_TIMEZONE_KEYS = frozenset(key for _label, key in US_TIMEZONES)


def _parse_clock(value: str) -> time:
    raw = str(value or "").strip()
    try:
        parsed = time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid posting time '{raw}'; use HH:MM") from exc
    return parsed.replace(second=0, microsecond=0)


def validate_posting_window(account: dict) -> Optional[str]:
    """Return a user-facing validation error, or None when valid/disabled."""
    zone_name = str(account.get("posting_timezone") or "").strip()
    start = str(account.get("posting_start_time") or "").strip()
    end = str(account.get("posting_end_time") or "").strip()
    if not zone_name and not start and not end:
        return None
    if not zone_name:
        return "Choose a US time zone or clear both posting times."
    if zone_name not in US_TIMEZONE_KEYS:
        return "Choose one of the supported US time zones."
    if not start or not end:
        return "Both posting start and end times are required."
    try:
        _parse_clock(start)
        _parse_clock(end)
        ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        return str(exc)
    return None


def posting_window_configured(account: dict) -> bool:
    return bool(
        str(account.get("posting_timezone") or "").strip()
        and str(account.get("posting_start_time") or "").strip()
        and str(account.get("posting_end_time") or "").strip()
    )


def is_within_posting_window(
    account: dict, now_utc: Optional[datetime] = None
) -> bool:
    """
    Return whether this account may auto-post now. Start is inclusive and end is
    exclusive. Equal start/end means a 24-hour window. Overnight windows work.
    Invalid configured windows fail closed.
    """
    if not posting_window_configured(account):
        return validate_posting_window(account) is None
    if validate_posting_window(account):
        return False
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    zone = ZoneInfo(str(account["posting_timezone"]))
    local_now = now.astimezone(zone)
    current = local_now.hour * 60 + local_now.minute
    start_clock = _parse_clock(str(account["posting_start_time"]))
    end_clock = _parse_clock(str(account["posting_end_time"]))
    start = start_clock.hour * 60 + start_clock.minute
    end = end_clock.hour * 60 + end_clock.minute
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def seconds_until_posting_window(
    account: dict, now_utc: Optional[datetime] = None
) -> float:
    """Seconds until the next opening; 0 when active/disabled, 3600 if invalid."""
    error = validate_posting_window(account)
    if error:
        return 3600.0
    if not posting_window_configured(account) or is_within_posting_window(account, now_utc):
        return 0.0
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    zone = ZoneInfo(str(account["posting_timezone"]))
    local_now = now.astimezone(zone)
    start = _parse_clock(str(account["posting_start_time"]))
    candidate = datetime.combine(local_now.date(), start, tzinfo=zone)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), start, tzinfo=zone)
    return max(0.0, (candidate.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds())


def posting_window_label(account: dict) -> str:
    if not posting_window_configured(account):
        return "24/7"
    zone_name = str(account.get("posting_timezone") or "")
    label = next((label for label, key in US_TIMEZONES if key == zone_name), zone_name)
    return (
        f"{account.get('posting_start_time')}–{account.get('posting_end_time')} "
        f"in {label}"
    )
