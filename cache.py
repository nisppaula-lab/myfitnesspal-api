"""
SQLite cache for MyFitnessPal data.

Provides a local store for diary entries, daily aggregates, measurements,
daily goals, and sync metadata.  Tools read from here (fast, sub-ms),
a background sync process writes to here.

Schema
------
  diary_entries   — one row per food entry per meal per day
  diary_daily     — aggregated daily totals + water (cups) + notes
  daily_goals     — nutrition goals per day (calorie/macro targets)
  measurements    — body measurements by date and type
  sync_meta       — which dates have been synced and their status
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


DB_FILENAME = "mfp_cache.db"
_SCHEMA_VERSION = 2


def _get_db_path(cache_dir: Path) -> Path:
    return cache_dir / DB_FILENAME


class MFPCache:
    """Thread-safe (one connection per process) SQLite cache for MFP data."""

    def __init__(self, cache_dir: Path) -> None:
        self._path = _get_db_path(cache_dir)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db: sqlite3.Connection | None = None
        self._init_db()
        self._migrate()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(str(self._path))
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        return self._db

    def _init_db(self) -> None:
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS diary_entries (
                date         TEXT NOT NULL,
                meal         TEXT NOT NULL,
                entry_id     INTEGER NOT NULL,
                food_name    TEXT,
                brand        TEXT,
                quantity     REAL,
                unit         TEXT,
                calories     REAL,
                protein      REAL,
                carbs        REAL,
                fat          REAL,
                fiber        REAL,
                sugar        REAL,
                sodium       REAL,
                PRIMARY KEY (date, meal, entry_id)
            );

            CREATE TABLE IF NOT EXISTS diary_daily (
                date         TEXT PRIMARY KEY,
                calories     REAL,
                protein      REAL,
                carbs        REAL,
                fat          REAL,
                fiber        REAL,
                sugar        REAL,
                sodium       REAL,
                water_cups   REAL,
                notes        TEXT,
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_goals (
                date         TEXT PRIMARY KEY,
                calories     REAL,
                protein      REAL,
                carbs        REAL,
                fat          REAL,
                fiber        REAL,
                sugar        REAL,
                sodium       REAL
            );

            CREATE TABLE IF NOT EXISTS measurements (
                date         TEXT NOT NULL,
                type         TEXT NOT NULL,
                value        REAL NOT NULL,
                PRIMARY KEY (date, type)
            );

            CREATE TABLE IF NOT EXISTS sync_meta (
                date         TEXT PRIMARY KEY,
                status       TEXT NOT NULL DEFAULT 'complete',
                synced_at    TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_diary_entries_date
                ON diary_entries(date);
            CREATE INDEX IF NOT EXISTS idx_measurements_date
                ON measurements(date);
            CREATE INDEX IF NOT EXISTS idx_measurements_type
                ON measurements(type);
        """)

        # Insert initial schema version if empty
        cur = conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
        conn.commit()
        conn.close()

    def _migrate(self) -> None:
        """Handle schema migrations from v1 to v2."""
        conn = self._get_conn()
        cur = conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        current_version = row["version"] if row else 1

        if current_version < 2:
            # v1 → v2: add water_cups, notes to diary_daily; add daily_goals table;
            # add quantity, unit to diary_entries
            try:
                conn.execute("ALTER TABLE diary_daily ADD COLUMN water_cups REAL")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                conn.execute("ALTER TABLE diary_daily ADD COLUMN notes TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE diary_entries ADD COLUMN quantity REAL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE diary_entries ADD COLUMN unit TEXT")
            except sqlite3.OperationalError:
                pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_goals (
                    date         TEXT PRIMARY KEY,
                    calories     REAL,
                    protein      REAL,
                    carbs        REAL,
                    fat          REAL,
                    fiber        REAL,
                    sugar        REAL,
                    sodium       REAL
                )
            """)

            # Migrate water_ml → water_cups (1 cup = 237 ml)
            try:
                conn.execute("""
                    UPDATE diary_daily
                    SET water_cups = ROUND(water_ml / 237.0, 1)
                    WHERE water_ml IS NOT NULL AND water_cups IS NULL
                """)
            except sqlite3.OperationalError:
                pass

            conn.execute("UPDATE schema_version SET version = 2")
            conn.commit()

    # ------------------------------------------------------------------
    # Diary
    # ------------------------------------------------------------------

    def upsert_diary_day(
        self,
        target_date: date,
        meals_data: List[Dict[str, Any]],
        daily_totals: Dict[str, float],
        water_cups: float | None = None,
        notes: str | None = None,
        goals: Dict[str, float] | None = None,
    ) -> None:
        """Replace all entries + daily totals + goals for a single date."""
        conn = self._get_conn()
        date_str = target_date.isoformat()
        now = datetime.utcnow().isoformat()

        # Remove old entries for this date
        conn.execute("DELETE FROM diary_entries WHERE date = ?", (date_str,))

        # Insert current entries
        entry_idx = 0
        for meal in meals_data:
            meal_name = meal.get("name", "unknown")
            for entry in meal.get("entries", []):
                conn.execute(
                    """INSERT INTO diary_entries
                       (date, meal, entry_id, food_name, brand, quantity, unit,
                        calories, protein, carbs, fat, fiber, sugar, sodium)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        date_str,
                        meal_name,
                        entry_idx,
                        entry.get("name"),
                        entry.get("brand"),
                        entry.get("quantity"),
                        entry.get("unit"),
                        entry.get("calories"),
                        entry.get("protein"),
                        entry.get("carbohydrates"),
                        entry.get("fat"),
                        entry.get("fiber"),
                        entry.get("sugar"),
                        entry.get("sodium"),
                    ),
                )
                entry_idx += 1

        # Upsert daily totals
        conn.execute(
            """INSERT OR REPLACE INTO diary_daily
               (date, calories, protein, carbs, fat, fiber, sugar, sodium,
                water_cups, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date_str,
                daily_totals.get("calories"),
                daily_totals.get("protein"),
                daily_totals.get("carbohydrates"),
                daily_totals.get("fat"),
                daily_totals.get("fiber"),
                daily_totals.get("sugar"),
                daily_totals.get("sodium"),
                water_cups,
                notes,
                now,
            ),
        )

        # Upsert daily goals
        if goals:
            conn.execute(
                """INSERT OR REPLACE INTO daily_goals
                   (date, calories, protein, carbs, fat, fiber, sugar, sodium)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date_str,
                    goals.get("calories"),
                    goals.get("protein"),
                    goals.get("carbohydrates"),
                    goals.get("fat"),
                    goals.get("fiber"),
                    goals.get("sugar"),
                    goals.get("sodium"),
                ),
            )

        conn.commit()

    def get_diary(self, start_date: date, end_date: date) -> List[Dict]:
        """Return daily aggregates for date range, newest first."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM diary_daily
               WHERE date >= ? AND date <= ?
               ORDER BY date DESC""",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_diary_date(self, target_date: date) -> Dict | None:
        """Return daily aggregate for a single date, or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM diary_daily WHERE date = ?",
            (target_date.isoformat(),),
        ).fetchone()
        return dict(row) if row else None

    def get_diary_entries(
        self, target_date: date
    ) -> List[Dict]:
        """Return detailed entries for a single date."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM diary_entries
               WHERE date = ?
               ORDER BY meal, entry_id""",
            (target_date.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Daily goals
    # ------------------------------------------------------------------

    def get_daily_goals(self, target_date: date) -> Dict[str, float]:
        """Return nutrition goals for a date, or empty dict."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM daily_goals WHERE date = ?",
            (target_date.isoformat(),),
        ).fetchone()
        if row is None:
            return {}
        return {k: v for k, v in dict(row).items() if k != "date" and v is not None}

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    def upsert_measurement(self, target_date: date, mtype: str, value: float) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO measurements (date, type, value)
               VALUES (?, ?, ?)""",
            (target_date.isoformat(), mtype, value),
        )
        conn.commit()

    def upsert_measurements(
        self, data: Dict[date, float], mtype: str
    ) -> None:
        """Bulk-insert measurements from a dict {date: value}."""
        conn = self._get_conn()
        rows = [(d.isoformat(), mtype, v) for d, v in data.items()]
        conn.executemany(
            """INSERT OR REPLACE INTO measurements (date, type, value)
               VALUES (?, ?, ?)""",
            rows,
        )
        conn.commit()

    def get_measurements(
        self, mtype: str, start_date: date, end_date: date
    ) -> List[Dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT date, value FROM measurements
               WHERE type = ? AND date >= ? AND date <= ?
               ORDER BY date""",
            (mtype, start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Sync metadata
    # ------------------------------------------------------------------

    def mark_synced(self, target_date: date, status: str = "complete") -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO sync_meta (date, status, synced_at)
               VALUES (?, ?, ?)""",
            (target_date.isoformat(), status, datetime.utcnow().isoformat()),
        )
        conn.commit()

    def mark_range_synced(self, start_date: date, end_date: date, status: str = "complete") -> None:
        conn = self._get_conn()
        now = datetime.utcnow().isoformat()
        conn.executemany(
            """INSERT OR REPLACE INTO sync_meta (date, status, synced_at)
               VALUES (?, ?, ?)""",
            [(d.isoformat(), status, now) for d in date_range(start_date, end_date)],
        )
        conn.commit()

    def get_synced_dates(self, start_date: date, end_date: date) -> List[str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT date FROM sync_meta WHERE date >= ? AND date <= ?",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        return [r["date"] for r in rows]

    def is_synced(self, target_date: date) -> bool:
        """Return True if date is synced with status 'complete'."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT status FROM sync_meta WHERE date = ?",
            (target_date.isoformat(),),
        ).fetchone()
        return row is not None and row["status"] == "complete"

    def needs_resync(self, target_date: date) -> bool:
        """Return True if date needs re-sync (stale, missing, or status=missing)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT status FROM sync_meta WHERE date = ?",
            (target_date.isoformat(),),
        ).fetchone()
        if row is None:
            return True  # never synced
        return row["status"] != "complete"

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def __del__(self) -> None:
        self.close()


def date_range(start: date, end: date):
    """Yield inclusive dates from start to end."""
    import datetime as dt

    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)
