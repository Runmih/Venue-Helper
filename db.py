import json
import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str = "venue_helper.db") -> None:
        self.path = Path(path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS configs (
                    home_guild_id INTEGER PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS profiles (
                    home_guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    link_type TEXT NOT NULL,
                    PRIMARY KEY (home_guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS availability (
                    home_guild_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    shifts_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (home_guild_id, event_date, user_id)
                );

                CREATE TABLE IF NOT EXISTS dispatch_log (
                    home_guild_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    dispatch_key TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (home_guild_id, event_date, dispatch_key)
                );

                CREATE TABLE IF NOT EXISTS message_index (
                    message_id INTEGER PRIMARY KEY,
                    home_guild_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_runs (
                    home_guild_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decided_by INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (home_guild_id, event_date)
                );
                """
            )

    def save_config(self, home_guild_id: int, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO configs(home_guild_id, data_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(home_guild_id) DO UPDATE SET
                    data_json=excluded.data_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (home_guild_id, json.dumps(data)),
            )

    def get_config(self, home_guild_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM configs WHERE home_guild_id = ?", (home_guild_id,)
            ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def list_configs(self) -> list[tuple[int, dict[str, Any]]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT home_guild_id, data_json FROM configs").fetchall()
        return [(int(r["home_guild_id"]), json.loads(r["data_json"])) for r in rows]

    def upsert_profile(
        self,
        home_guild_id: int,
        user_id: int,
        display_name: str,
        url: str,
        link_type: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO profiles(home_guild_id, user_id, display_name, url, link_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(home_guild_id, user_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    url=excluded.url,
                    link_type=excluded.link_type
                """,
                (home_guild_id, user_id, display_name, url, link_type),
            )

    def get_profile(self, home_guild_id: int, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT display_name, url, link_type
                FROM profiles
                WHERE home_guild_id = ? AND user_id = ?
                """,
                (home_guild_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def set_availability(
        self,
        home_guild_id: int,
        event_date: str,
        user_id: int,
        status: str,
        shifts: list[int],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO availability(home_guild_id, event_date, user_id, status, shifts_json, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(home_guild_id, event_date, user_id) DO UPDATE SET
                    status=excluded.status,
                    shifts_json=excluded.shifts_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (home_guild_id, event_date, user_id, status, json.dumps(shifts)),
            )

    def get_availability(self, home_guild_id: int, event_date: str) -> dict[int, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, status, shifts_json
                FROM availability
                WHERE home_guild_id = ? AND event_date = ?
                """,
                (home_guild_id, event_date),
            ).fetchall()
        return {
            int(r["user_id"]): {
                "status": r["status"],
                "shifts": json.loads(r["shifts_json"]),
            }
            for r in rows
        }

    def mark_dispatched(self, home_guild_id: int, event_date: str, dispatch_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO dispatch_log(home_guild_id, event_date, dispatch_key)
                VALUES (?, ?, ?)
                """,
                (home_guild_id, event_date, dispatch_key),
            )

    def was_dispatched(self, home_guild_id: int, event_date: str, dispatch_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM dispatch_log
                WHERE home_guild_id = ? AND event_date = ? AND dispatch_key = ?
                """,
                (home_guild_id, event_date, dispatch_key),
            ).fetchone()
        return row is not None

    def index_message(
        self,
        message_id: int,
        home_guild_id: int,
        event_date: str,
        kind: str,
        guild_id: int,
        channel_id: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO message_index
                    (message_id, home_guild_id, event_date, kind, guild_id, channel_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, home_guild_id, event_date, kind, guild_id, channel_id),
            )

    def get_message_context(self, message_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM message_index WHERE message_id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_messages(self, home_guild_id: int, event_date: str, kind: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM message_index
                WHERE home_guild_id = ? AND event_date = ? AND kind = ?
                """,
                (home_guild_id, event_date, kind),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_event_status(
        self,
        home_guild_id: int,
        event_date: str,
        status: str,
        decided_by: int | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO event_runs(home_guild_id, event_date, status, decided_by, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(home_guild_id, event_date) DO UPDATE SET
                    status=excluded.status,
                    decided_by=excluded.decided_by,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (home_guild_id, event_date, status, decided_by),
            )

    def get_event_status(self, home_guild_id: int, event_date: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status FROM event_runs
                WHERE home_guild_id = ? AND event_date = ?
                """,
                (home_guild_id, event_date),
            ).fetchone()
        return str(row["status"]) if row else None
