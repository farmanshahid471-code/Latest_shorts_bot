"""Command-line entry point for the clip-farming bot."""
from __future__ import annotations

import argparse
from typing import Optional

from tabulate import tabulate

from .config import (
    ACCOUNTS,
    CYCLE_INTERVAL_HOURS,
    MAX_DAILY_UPLOADS,
    WEBUI_HOST,
    WEBUI_PORT,
    logger,
)
from .fetcher import YouTubeFetcher
from .models import StateDB
from .runtime import pipeline_guard
from .scheduler import ShortsBotScheduler
from .storage import CloudStorageManager
from .uploader import YouTubeUploader, resolve_credentials


def print_status() -> None:
    db = StateDB()
    videos = db.get_all_processed_videos(limit=15)
    print("\n=== YOUTUBE SHORTS BOT STATUS ===")
    print(f"Real YouTube uploads in the last 24h (all accounts): {db.get_uploads_in_last_24_hours()}")
    for account in ACCOUNTS:
        name = account.get("name", "default")
        maximum = int(account.get("max_daily_uploads") or MAX_DAILY_UPLOADS)
        used = db.get_uploads_in_last_24_hours(account=name)
        print(f"  {name}: {used}/{maximum} real uploads")
    if videos:
        rows = [
            [
                video.get("account", ""),
                video.get("video_id", ""),
                str(video.get("title") or "")[:30],
                video.get("status", ""),
                video.get("youtube_short_id") or "-",
                str(video.get("updated_at") or "")[:19],
            ]
            for video in videos
        ]
        print(tabulate(rows, headers=["Account", "Video", "Title", "State", "YouTube", "Updated"], tablefmt="grid"))
    storage = CloudStorageManager()
    total, objects = storage.get_bucket_usage()
    print(f"R2 usage: {total / (1024**3):.3f} GB ({len(objects)} objects)\n")


def process_single_url(
    url: str,
    count: int = 1,
    aspect: Optional[str] = None,
    fill: Optional[str] = None,
    logo_position: Optional[str] = None,
    like_subscribe: Optional[bool] = None,
    like_subscribe_text: Optional[str] = None,
    top_watermark_enabled: Optional[bool] = None,
    top_watermark_text: Optional[str] = None,
    account: Optional[dict] = None,
) -> int:
    """Process one source video without allowing overlap with a scheduler cycle."""
    with pipeline_guard(blocking=False) as acquired:
        if not acquired:
            logger.warning("Another clip pipeline is active; manual request rejected.")
            return 0
        selected = dict(account or {"name": "default"})
        name = str(selected.get("name") or "default")
        aspect = aspect or selected.get("aspect")
        fill = fill or selected.get("fill")
        if logo_position is None:
            logo_position = (
                selected.get("logo_position")
                if selected.get("logo_remove")
                else "off"
            )
        if like_subscribe is None and "watermark_enabled" in selected:
            like_subscribe = bool(selected.get("watermark_enabled"))
        if like_subscribe_text is None:
            like_subscribe_text = str(selected.get("watermark") or "").strip()
        if top_watermark_enabled is None and "top_watermark_enabled" in selected:
            top_watermark_enabled = bool(selected.get("top_watermark_enabled"))
        if top_watermark_text is None:
            top_watermark_text = str(selected.get("top_watermark") or "").strip()

        fetcher = YouTubeFetcher(
            strategy=selected.get("selection_strategy"),
            heatmap_weight=selected.get("heatmap_weight"),
            audio_weight=selected.get("audio_excitement_weight"),
        )
        count = min(20, max(1, int(count)))
        if count == 1:
            info, _peak, start, end = fetcher.extract_heatmap_and_select_window(url)
            windows = [{"start": start, "end": end}]
        else:
            ranked = fetcher.select_top_windows(url, count=count)
            windows = [{"start": item["start"], "end": item["end"]} for item in ranked]
            # One metadata request for title/upload details.
            info, _peak, _start, _end = fetcher.extract_heatmap_and_select_window(url)
        video_id = str(info.get("id") or fetcher._extract_video_id(url))
        video_title = str(info.get("title") or f"Video {video_id}")

        db = StateDB()
        claim = db.claim_video(video_id, name)
        if not claim:
            logger.warning("%s is already uploaded or being processed for %s.", video_id, name)
            return 0
        try:
            scheduler = ShortsBotScheduler(accounts=[selected])
            scheduler.state_db = db
            scheduler.storage = CloudStorageManager()
            client_secret, token = resolve_credentials(selected if account else None)
            uploader = YouTubeUploader(
                client_secret_file=client_secret,
                token_file=token,
                state_db=db,
            )
            expected = str(
                selected.get("expected_channel")
                or selected.get("connected_channel")
                or ""
            ).strip()
            return scheduler._process_video_windows(
                video_id,
                url,
                video_title,
                str(info.get("channel_url") or info.get("channel") or ""),
                windows,
                account=name,
                max_daily=int(selected.get("max_daily_uploads") or MAX_DAILY_UPLOADS),
                uploader=uploader,
                info=info,
                aspect=aspect,
                fill=fill,
                logo_position=logo_position,
                like_subscribe=like_subscribe,
                like_subscribe_text=like_subscribe_text,
                top_watermark_enabled=top_watermark_enabled,
                top_watermark_text=top_watermark_text,
                extra_hashtags=str(selected.get("extra_hashtags") or ""),
                title_prefix=selected.get("title_prefix"),
                title_hashtags=str(selected.get("title_hashtags") or ""),
                smart_titles=selected.get("smart_titles"),
                delete_after_upload=bool(selected.get("delete_after_upload", False)),
                delete_r2_after_upload=bool(selected.get("delete_r2_after_upload", False)),
                subtitles_enabled=bool(selected.get("subtitles_enabled", True)),
                expected_channel=expected,
                expected_channel_id=str(selected.get("connected_channel_id") or ""),
            )
        finally:
            db.release_video_claim(video_id, name, claim)


def _selected_accounts(name: Optional[str]) -> list[dict]:
    if not name:
        return [account for account in ACCOUNTS if account.get("enabled", True)]
    matches = [
        account
        for account in ACCOUNTS
        if str(account.get("name") or "").casefold() == name.casefold()
    ]
    if not matches:
        raise ValueError(f"No account named '{name}' exists")
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube Shorts clip-farming bot")
    parser.add_argument(
        "--mode",
        choices=["scheduler", "once", "process-url", "status", "prune-r2", "test-yt-auth", "webui"],
        default="scheduler",
    )
    parser.add_argument("--url")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--aspect", choices=["auto", "3:4", "9:16"])
    parser.add_argument("--fill", choices=["blur", "crop"])
    parser.add_argument("--logo")
    parser.add_argument("--like-subscribe")
    parser.add_argument("--top-watermark")
    parser.add_argument("--channels")
    parser.add_argument("--account")
    parser.add_argument("--interval", type=int, default=CYCLE_INTERVAL_HOURS)
    parser.add_argument("--host", default=WEBUI_HOST)
    parser.add_argument("--port", type=int, default=WEBUI_PORT)
    args = parser.parse_args()

    try:
        selected = _selected_accounts(args.account)
    except ValueError as exc:
        parser.error(str(exc))

    if args.channels:
        channels = [value.strip() for value in args.channels.split(",") if value.strip()]
        for account in selected:
            account["target_channels"] = channels

    if args.mode == "status":
        print_status()
    elif args.mode == "prune-r2":
        print(f"Deleted {CloudStorageManager().enforce_storage_limit()} R2 object(s).")
    elif args.mode == "test-yt-auth":
        account = selected[0] if selected else None
        client_secret, token = resolve_credentials(account)
        service = YouTubeUploader(
            client_secret_file=client_secret, token_file=token
        )._get_authenticated_service()
        print("YouTube authentication successful." if service else "YouTube authentication failed; see logs.")
    elif args.mode == "process-url":
        if not args.url:
            parser.error("--url is required for process-url")
        if not args.account and len(selected) != 1:
            parser.error("--account is required when multiple destination accounts exist")
        like = None
        if args.like_subscribe is not None:
            like = args.like_subscribe.lower() in {"on", "true", "1", "yes"}
        top = args.top_watermark
        if top and top.lower() in {"off", "false", "none", "0"}:
            top = ""
        process_single_url(
            args.url,
            count=args.count,
            aspect=args.aspect,
            fill=args.fill,
            logo_position=args.logo,
            like_subscribe=like,
            top_watermark_text=top,
            account=selected[0] if selected else None,
        )
    elif args.mode == "once":
        ShortsBotScheduler(interval_hours=args.interval, accounts=selected).run_single_cycle()
    elif args.mode == "scheduler":
        ShortsBotScheduler(interval_hours=args.interval, accounts=selected).start_24_7_loop()
    elif args.mode == "webui":
        from .webui import run_webui

        run_webui(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
