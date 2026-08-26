"""
SQLite state management for idempotency, retry queues, processing leases, and
per-account YouTube upload quotas.

All operations that decide whether work may start use ``BEGIN IMMEDIATE`` so
multiple Web UI/scheduler threads cannot claim the same video or reserve the
same final quota slot at the same time.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import DB_PATH, logger

# Only these states mean that the source must never be selected again.
# R2-only, quota-waiting, dry-run and failed records intentionally remain
# retryable.
TERMINAL_VIDEO_STATUSES = frozenset(
    {"UPLOADED_YOUTUBE", "PROCESSED", "PROCESSED_MULTI", "SKIPPED"}
)


class StateDB:
    """Persistent, concurrency-safe state for all configured accounts."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _utcnow() -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    @classmethod
    def _iso_now(cls) -> str:
        return cls._utcnow().isoformat()

    def init_db(self) -> None:
        """Create the current schema and migrate the old single-account schema."""
        with self._get_connection() as conn:
            # WAL lets status reads continue while a worker commits an update.
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError as exc:
                logger.warning("Could not enable SQLite WAL mode: %s", exc)

            has_processed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='processed_videos'"
            ).fetchone()
            if has_processed:
                cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(processed_videos)")
                }
                if "account" not in cols:
                    logger.info("Migrating processed_videos for multi-account support...")
                    conn.executescript(
                        """
                        CREATE TABLE processed_videos_new (
                            video_id TEXT NOT NULL,
                            account TEXT NOT NULL DEFAULT '',
                            video_url TEXT,
                            channel_id TEXT,
                            title TEXT,
                            peak_time REAL,
                            clip_start REAL,
                            clip_end REAL,
                            r2_key TEXT,
                            youtube_short_id TEXT,
                            status TEXT,
                            error_msg TEXT,
                            created_at TEXT,
                            updated_at TEXT,
                            PRIMARY KEY (video_id, account)
                        );
                        INSERT INTO processed_videos_new (
                            video_id, account, video_url, channel_id, title,
                            peak_time, clip_start, clip_end, r2_key,
                            youtube_short_id, status, error_msg, created_at, updated_at
                        )
                        SELECT video_id, '', video_url, channel_id, title,
                            peak_time, clip_start, clip_end, r2_key,
                            youtube_short_id, status, error_msg, created_at, updated_at
                        FROM processed_videos;
                        DROP TABLE processed_videos;
                        ALTER TABLE processed_videos_new RENAME TO processed_videos;
                        """
                    )
            else:
                conn.execute(
                    """
                    CREATE TABLE processed_videos (
                        video_id TEXT NOT NULL,
                        account TEXT NOT NULL DEFAULT '',
                        video_url TEXT,
                        channel_id TEXT,
                        title TEXT,
                        peak_time REAL,
                        clip_start REAL,
                        clip_end REAL,
                        r2_key TEXT,
                        youtube_short_id TEXT,
                        status TEXT,
                        error_msg TEXT,
                        created_at TEXT,
                        updated_at TEXT,
                        PRIMARY KEY (video_id, account)
                    )
                    """
                )

            has_uploads = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_uploads'"
            ).fetchone()
            if has_uploads:
                cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(daily_uploads)")
                }
                if "account" not in cols:
                    logger.info("Migrating daily_uploads for multi-account support...")
                    conn.executescript(
                        """
                        CREATE TABLE daily_uploads_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            video_id TEXT NOT NULL,
                            account TEXT NOT NULL DEFAULT '',
                            youtube_short_id TEXT,
                            uploaded_at TEXT NOT NULL
                        );
                        INSERT INTO daily_uploads_new (
                            video_id, account, youtube_short_id, uploaded_at
                        )
                        SELECT video_id, '', youtube_short_id, uploaded_at
                        FROM daily_uploads;
                        DROP TABLE daily_uploads;
                        ALTER TABLE daily_uploads_new RENAME TO daily_uploads;
                        """
                    )
            else:
                conn.execute(
                    """
                    CREATE TABLE daily_uploads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        video_id TEXT NOT NULL,
                        account TEXT NOT NULL DEFAULT '',
                        youtube_short_id TEXT,
                        uploaded_at TEXT NOT NULL
                    )
                    """
                )

            # Old builds could record the same upload more than once. Keep the
            # first row before adding the uniqueness guarantee.
            conn.execute(
                """
                DELETE FROM daily_uploads
                WHERE id NOT IN (
                    SELECT MIN(id) FROM daily_uploads GROUP BY video_id, account
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_upload_video_account
                ON daily_uploads(video_id, account)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_daily_upload_account_time
                ON daily_uploads(account, uploaded_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processing_claims (
                    video_id TEXT NOT NULL,
                    account TEXT NOT NULL DEFAULT '',
                    claim_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (video_id, account)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL DEFAULT '',
                    reserved_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_upload_reservation_account
                ON upload_reservations(account, expires_at)
                """
            )
            conn.commit()
        logger.debug("StateDB initialized at %s", self.db_path)

    # ------------------------------------------------------------------
    # Video state and processing leases
    def is_video_processed(self, video_id: str, account: str = "") -> bool:
        placeholders = ",".join("?" for _ in TERMINAL_VIDEO_STATUSES)
        with self._get_connection() as conn:
            row = conn.execute(
                f"""
                SELECT 1 FROM processed_videos
                WHERE video_id = ? AND account = ? AND status IN ({placeholders})
                """,
                (video_id, account, *sorted(TERMINAL_VIDEO_STATUSES)),
            ).fetchone()
            return row is not None

    def claim_video(
        self,
        video_id: str,
        account: str = "",
        lease_minutes: int = 180,
    ) -> Optional[str]:
        """Atomically claim a source for processing; return a lease token or None."""
        now = self._utcnow()
        expires = now + dt.timedelta(minutes=max(1, lease_minutes))
        claim_id = uuid.uuid4().hex
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM processing_claims WHERE datetime(expires_at) <= datetime(?)",
                (now.isoformat(),),
            )
            terminal = self.is_video_processed_in_connection(conn, video_id, account)
            active = conn.execute(
                "SELECT 1 FROM processing_claims WHERE video_id = ? AND account = ?",
                (video_id, account),
            ).fetchone()
            if terminal or active:
                conn.rollback()
                return None
            conn.execute(
                """
                INSERT INTO processing_claims (
                    video_id, account, claim_id, claimed_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (video_id, account, claim_id, now.isoformat(), expires.isoformat()),
            )
            conn.commit()
            return claim_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def is_video_processed_in_connection(
        conn: sqlite3.Connection, video_id: str, account: str
    ) -> bool:
        placeholders = ",".join("?" for _ in TERMINAL_VIDEO_STATUSES)
        return (
            conn.execute(
                f"""
                SELECT 1 FROM processed_videos
                WHERE video_id = ? AND account = ? AND status IN ({placeholders})
                """,
                (video_id, account, *sorted(TERMINAL_VIDEO_STATUSES)),
            ).fetchone()
            is not None
        )

    def release_video_claim(
        self, video_id: str, account: str = "", claim_id: Optional[str] = None
    ) -> None:
        with self._get_connection() as conn:
            if claim_id:
                conn.execute(
                    """
                    DELETE FROM processing_claims
                    WHERE video_id = ? AND account = ? AND claim_id = ?
                    """,
                    (video_id, account, claim_id),
                )
            else:
                conn.execute(
                    "DELETE FROM processing_claims WHERE video_id = ? AND account = ?",
                    (video_id, account),
                )
            conn.commit()

    def get_video_state(
        self, video_id: str, account: str = ""
    ) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM processed_videos WHERE video_id = ? AND account = ?",
                (video_id, account),
            ).fetchone()
            return dict(row) if row else None

    def record_video_state(
        self,
        video_id: str,
        video_url: str = "",
        channel_id: str = "",
        title: str = "",
        peak_time: float = 0.0,
        clip_start: float = 0.0,
        clip_end: float = 0.0,
        r2_key: str = "",
        youtube_short_id: str = "",
        status: str = "FETCHED",
        error_msg: str = "",
        account: str = "",
    ) -> None:
        now = self._iso_now()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO processed_videos (
                    video_id, account, video_url, channel_id, title, peak_time,
                    clip_start, clip_end, r2_key, youtube_short_id,
                    status, error_msg, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, account) DO UPDATE SET
                    video_url=coalesce(nullif(excluded.video_url, ''), processed_videos.video_url),
                    channel_id=coalesce(nullif(excluded.channel_id, ''), processed_videos.channel_id),
                    title=coalesce(nullif(excluded.title, ''), processed_videos.title),
                    peak_time=excluded.peak_time,
                    clip_start=excluded.clip_start,
                    clip_end=excluded.clip_end,
                    r2_key=coalesce(nullif(excluded.r2_key, ''), processed_videos.r2_key),
                    youtube_short_id=coalesce(
                        nullif(excluded.youtube_short_id, ''),
                        processed_videos.youtube_short_id
                    ),
                    status=excluded.status,
                    error_msg=excluded.error_msg,
                    updated_at=excluded.updated_at
                """,
                (
                    video_id,
                    account,
                    video_url,
                    channel_id,
                    title,
                    peak_time,
                    clip_start,
                    clip_end,
                    r2_key,
                    youtube_short_id,
                    status,
                    error_msg,
                    now,
                    now,
                ),
            )
            conn.commit()
        logger.debug("Recorded state for %s/%s: status=%s", account, video_id, status)

    # ------------------------------------------------------------------
    # Upload quota and atomic reservations
    def get_last_upload_time(self, account: str = "") -> Optional[dt.datetime]:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT uploaded_at FROM daily_uploads
                WHERE account = ? ORDER BY datetime(uploaded_at) DESC LIMIT 1
                """,
                (account,),
            ).fetchone()
        if not row:
            return None
        try:
            parsed = dt.datetime.fromisoformat(row["uploaded_at"])
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed
        except (TypeError, ValueError):
            return None

    def get_uploads_in_last_24_hours(self, account: Optional[str] = None) -> int:
        """Count real uploads. ``None`` means all accounts; ``''`` means legacy default."""
        cutoff = (self._utcnow() - dt.timedelta(hours=24)).isoformat()
        with self._get_connection() as conn:
            if account is None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM daily_uploads
                    WHERE datetime(uploaded_at) >= datetime(?)
                    """,
                    (cutoff,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM daily_uploads
                    WHERE datetime(uploaded_at) >= datetime(?) AND account = ?
                    """,
                    (cutoff, account),
                ).fetchone()
            return int(row["cnt"] if row else 0)

    def _quota_usage_in_connection(
        self, conn: sqlite3.Connection, account: str, now: dt.datetime
    ) -> tuple[int, int]:
        cutoff = (now - dt.timedelta(hours=24)).isoformat()
        uploaded = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM daily_uploads
                WHERE account = ? AND datetime(uploaded_at) >= datetime(?)
                """,
                (account, cutoff),
            ).fetchone()["cnt"]
        )
        reserved = int(
            conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM upload_reservations
                WHERE account = ? AND datetime(expires_at) > datetime(?)
                """,
                (account, now.isoformat()),
            ).fetchone()["cnt"]
        )
        return uploaded, reserved

    def can_upload_today(
        self, max_daily_uploads: int = 10, account: str = ""
    ) -> tuple[bool, int]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM upload_reservations WHERE datetime(expires_at) <= datetime(?)",
                (now.isoformat(),),
            )
            uploaded, reserved = self._quota_usage_in_connection(conn, account, now)
            conn.commit()
        remaining = max(0, int(max_daily_uploads) - uploaded - reserved)
        return remaining > 0, remaining

    def reserve_upload_slot(
        self,
        max_daily_uploads: int = 10,
        account: str = "",
        lease_minutes: int = 120,
    ) -> tuple[Optional[str], int]:
        """Atomically reserve one upload slot and return (reservation, remaining)."""
        max_daily_uploads = max(0, int(max_daily_uploads))
        now = self._utcnow()
        expires = now + dt.timedelta(minutes=max(5, lease_minutes))
        reservation = uuid.uuid4().hex
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM upload_reservations WHERE datetime(expires_at) <= datetime(?)",
                (now.isoformat(),),
            )
            uploaded, reserved = self._quota_usage_in_connection(conn, account, now)
            if uploaded + reserved >= max_daily_uploads:
                conn.rollback()
                return None, 0
            conn.execute(
                """
                INSERT INTO upload_reservations (
                    reservation_id, account, reserved_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (reservation, account, now.isoformat(), expires.isoformat()),
            )
            remaining = max_daily_uploads - uploaded - reserved - 1
            conn.commit()
            return reservation, max(0, remaining)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_upload_reservation(self, reservation_id: Optional[str]) -> None:
        if not reservation_id:
            return
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM upload_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )
            conn.commit()

    def record_upload(
        self,
        video_id: str,
        youtube_short_id: str,
        account: str = "",
        reservation_id: Optional[str] = None,
    ) -> None:
        """Record one real upload exactly once and consume its reservation."""
        now = self._iso_now()
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO daily_uploads (
                    video_id, account, youtube_short_id, uploaded_at
                ) VALUES (?, ?, ?, ?)
                """,
                (video_id, account, youtube_short_id, now),
            )
            conn.execute(
                """
                UPDATE processed_videos
                SET youtube_short_id = ?, status = 'UPLOADED_YOUTUBE',
                    error_msg = '', updated_at = ?
                WHERE video_id = ? AND account = ?
                """,
                (youtube_short_id, now, video_id, account),
            )
            if reservation_id:
                conn.execute(
                    "DELETE FROM upload_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        logger.info(
            "Recorded real YouTube upload for %s/%s -> %s",
            account,
            video_id,
            youtube_short_id,
        )

    # ------------------------------------------------------------------
    def get_all_processed_videos(
        self, limit: int = 50, account: Optional[str] = None
    ) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            if account is None:
                rows = conn.execute(
                    "SELECT * FROM processed_videos ORDER BY datetime(updated_at) DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM processed_videos WHERE account = ?
                    ORDER BY datetime(updated_at) DESC LIMIT ?
                    """,
                    (account, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def count_video_records(self, account: Optional[str] = None) -> int:
        with self._get_connection() as conn:
            if account is None:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM processed_videos").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM processed_videos WHERE account = ?",
                    (account,),
                ).fetchone()
            return int(row["cnt"] if row else 0)

    def delete_account_data(self, account: str) -> None:
        """Delete all local state/quota/lease rows for one removed account."""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in (
                "processed_videos",
                "daily_uploads",
                "processing_claims",
                "upload_reservations",
            ):
                conn.execute(f"DELETE FROM {table} WHERE account = ?", (account,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_accounts_in_db(self) -> list[str]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT account FROM processed_videos WHERE account != ''"
            ).fetchall()
            return [row["account"] for row in rows]
