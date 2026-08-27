"""Command-line entry point for the full-Short repost bot."""
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
)
from .fetcher import ShortsFetcher
from .models import StateDB
from .reprocessor import ShortReprocessor
from .runtime import pipeline_guard
from .scheduler import ShortsRepostScheduler
from .storage import CloudStorageManager
from .uploader import YouTubeUploader, resolve_credentials


def print_status() -> None:
    db = StateDB()
    rows = db.get_all_processed_videos(limit=20)
    print("\n=== SHORTS REPOST BOT STATUS ===")
    print(f"Real uploads in the last 24h (all accounts): {db.get_uploads_in_last_24_hours()}")
    for account in ACCOUNTS:
        name = account.get("name", "default")
        maximum = int(account.get("max_daily_uploads") or MAX_DAILY_UPLOADS)
        print(
            f"  {name}: {db.get_uploads_in_last_24_hours(account=name)}/{maximum} real uploads"
        )
    if rows:
        print(
            tabulate(
                [
                    [
                        row.get("account", ""),
                        row.get("video_id", ""),
                        str(row.get("title") or "")[:30],
                        row.get("status", ""),
                        row.get("youtube_short_id") or "-",
                    ]
                    for row in rows
                ],
                headers=["Account", "Source", "Title", "State", "YouTube"],
                tablefmt="grid",
            )
        )
    total, objects = CloudStorageManager().get_bucket_usage()
    print(f"R2 usage: {total / (1024**3):.3f} GB ({len(objects)} objects)\n")


def repost_one_url(url: str, account: Optional[dict] = None) -> bool:
    """Repost one URL for an explicitly selected destination account."""
    if not account:
        raise ValueError("Choose a destination account before reposting a Short")
    with pipeline_guard(blocking=False) as acquired:
        if not acquired:
            raise RuntimeError("Another repost pipeline is already active")
        selected = dict(account)
        name = str(selected.get("name") or "").strip()
        if not name:
            raise ValueError("The selected destination account has no name")

        fetcher = ShortsFetcher()
        info = fetcher.get_short_info(url)
        video_id = str(info.get("id") or fetcher._extract_video_id(url))
        video_title = str(info.get("title") or f"Short {video_id}")
        duration = float(info.get("duration") or 0)
        from .config import MAX_SHORT_DURATION_SEC

        if duration > MAX_SHORT_DURATION_SEC + 1:
            raise ValueError(
                f"This video is {duration:.1f}s, longer than the configured Short limit"
            )

        db = StateDB()
        if db.is_video_processed(video_id, name):
            return False
        claim = db.claim_video(video_id, name)
        if not claim:
            raise RuntimeError("This Short is already being processed")
        try:
            scheduler = ShortsRepostScheduler(accounts=[selected])
            scheduler.state_db = db
            scheduler.storage = CloudStorageManager()
            client_secret, token = resolve_credentials(selected)
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
            return scheduler._process_one(
                video_id,
                url,
                video_title,
                str(info.get("channel_url") or info.get("channel") or ""),
                account=name,
                max_daily=int(selected.get("max_daily_uploads") or MAX_DAILY_UPLOADS),
                fetcher=fetcher,
                reprocessor=ShortReprocessor(),
                uploader=uploader,
                like_subscribe=(
                    None
                    if "watermark_enabled" not in selected
                    else bool(selected.get("watermark_enabled"))
                ),
                like_subscribe_text=str(selected.get("watermark") or ""),
                top_watermark_enabled=(
                    None
                    if "top_watermark_enabled" not in selected
                    else bool(selected.get("top_watermark_enabled"))
                ),
                top_watermark_text=str(selected.get("top_watermark") or ""),
                extra_hashtags=str(selected.get("extra_hashtags") or ""),
                title_prefix=selected.get("title_prefix"),
                title_hashtags=str(selected.get("title_hashtags") or ""),
                smart_titles=selected.get("smart_titles"),
                delete_after_upload=bool(selected.get("delete_after_upload", False)),
                delete_r2_after_upload=bool(selected.get("delete_r2_after_upload", False)),
                process_mode=selected.get("process_mode") or "copy",
                subtitles_enabled=bool(selected.get("subtitles_enabled", False)),
                expected_channel=expected,
                expected_channel_id=str(selected.get("connected_channel_id") or ""),
                aspect=selected.get("aspect") or "auto",
                fill=selected.get("fill") or "crop",
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
    parser = argparse.ArgumentParser(description="YouTube Shorts repost bot")
    parser.add_argument(
        "--mode",
        choices=["scheduler", "once", "process-url", "status", "webui"],
        default="scheduler",
    )
    parser.add_argument("--url")
    parser.add_argument("--channels")
    parser.add_argument("--account")
    parser.add_argument("--interval", type=int, default=CYCLE_INTERVAL_HOURS)
    parser.add_argument("--host", default=WEBUI_HOST)
    parser.add_argument("--port", type=int, default=WEBUI_PORT)
    args = parser.parse_args()

    try:
        accounts = _selected_accounts(args.account)
    except ValueError as exc:
        parser.error(str(exc))
    if args.channels:
        channels = [item.strip() for item in args.channels.split(",") if item.strip()]
        for account in accounts:
            account["target_channels"] = channels

    if args.mode == "status":
        print_status()
    elif args.mode == "process-url":
        if not args.url:
            parser.error("--url is required for process-url")
        if not args.account and len(accounts) != 1:
            parser.error("--account is required when multiple destination accounts exist")
        repost_one_url(args.url, accounts[0] if accounts else None)
    elif args.mode == "once":
        ShortsRepostScheduler(interval_hours=args.interval, accounts=accounts).run_single_cycle()
    elif args.mode == "scheduler":
        ShortsRepostScheduler(interval_hours=args.interval, accounts=accounts).start_24_7_loop()
    elif args.mode == "webui":
        from .webui import run_webui

        run_webui(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
