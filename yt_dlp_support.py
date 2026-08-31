"""Shared yt-dlp safeguards for authenticated YouTube extraction.

YouTube's August 2026 player rollout makes yt-dlp's logged-in default choose the
broken ``tv_downgraded`` client for some accounts.  Keep viewer cookies for
age-restricted sources, but explicitly use the upstream-recommended clients and
retry public sources once without cookies when that authenticated path fails.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

AUTHENTICATED_PLAYER_CLIENTS: tuple[str, ...] = ("default", "web_embedded")
_VIEWER_AUTH_KEYS: tuple[str, ...] = ("cookiefile", "cookiesfrombrowser")
_COOKIE_PLAYER_FAILURE_MARKERS: tuple[str, ...] = (
    "the page needs to be reloaded",
    "requested format is not available",
    "no video formats found",
    "playback on other websites has been disabled",
    "http error 403",
    "403 forbidden",
)

_Result = TypeVar("_Result")


def authenticated_youtube_options() -> dict[str, Any]:
    """Return the safe player-client policy required when cookies are active."""
    return {
        "extractor_args": {
            "youtube": {"player_client": list(AUTHENTICATED_PLAYER_CLIENTS)}
        }
    }


def has_viewer_auth(options: Mapping[str, Any]) -> bool:
    """Whether yt-dlp options contain a cookie source."""
    return any(options.get(key) for key in _VIEWER_AUTH_KEYS)


def without_viewer_auth(options: Mapping[str, Any]) -> dict[str, Any]:
    """Copy options and restore yt-dlp's normal anonymous client selection."""
    anonymous = dict(options)
    for key in _VIEWER_AUTH_KEYS:
        anonymous.pop(key, None)

    # Remove only the workaround injected for authenticated requests. Preserve
    # any unrelated extractor arguments if more are added in the future.
    extractor_args = dict(anonymous.get("extractor_args") or {})
    youtube_args = dict(extractor_args.get("youtube") or {})
    clients = tuple(youtube_args.get("player_client") or ())
    if clients == AUTHENTICATED_PLAYER_CLIENTS:
        youtube_args.pop("player_client", None)
        if youtube_args:
            extractor_args["youtube"] = youtube_args
        else:
            extractor_args.pop("youtube", None)
        if extractor_args:
            anonymous["extractor_args"] = extractor_args
        else:
            anonymous.pop("extractor_args", None)
    return anonymous


def is_cookie_player_failure(error: object) -> bool:
    """True for failures known to be caused by the authenticated client path."""
    text = str(error).lower()
    return any(marker in text for marker in _COOKIE_PLAYER_FAILURE_MARKERS)


def run_youtube_dl(
    youtube_dl_factory: Callable[[Mapping[str, Any]], Any],
    options: Mapping[str, Any],
    operation: Callable[[Any], _Result],
    *,
    logger: Any,
    context: str,
) -> _Result:
    """Run one yt-dlp operation with a narrowly scoped public-source fallback.

    The first attempt always keeps cookies, which is essential for age-gated
    sources. Only known cookie/player-client format failures trigger a second
    attempt without cookies. Other errors are raised unchanged, and an
    age-restricted source still fails as authentication-required if its safe
    authenticated attempt cannot provide media.
    """
    primary = dict(options)
    try:
        with youtube_dl_factory(primary) as ydl:
            return operation(ydl)
    except Exception as error:
        if not has_viewer_auth(primary) or not is_cookie_player_failure(error):
            raise
        logger.warning(
            "Authenticated YouTube %s hit the current cookie/player-client "
            "failure (%s). Retrying once without viewer cookies for public-video "
            "compatibility; age-restricted sources still require cookies.",
            context,
            error,
        )

    anonymous = without_viewer_auth(primary)
    with youtube_dl_factory(anonymous) as ydl:
        return operation(ydl)
