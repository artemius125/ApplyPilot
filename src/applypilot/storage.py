from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, account TEXT NOT NULL, mode TEXT NOT NULL,
  input_path TEXT NOT NULL DEFAULT '', requested_limit INTEGER NOT NULL DEFAULT 0,
  selected_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
  stop_reason TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run_items (
  run_id TEXT NOT NULL, vacancy_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  score INTEGER NOT NULL DEFAULT 0, resume TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, vacancy_id), FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS attempts (
  account TEXT NOT NULL DEFAULT 'default', vacancy_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  score INTEGER NOT NULL DEFAULT 0, resume TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY (account, vacancy_id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, vacancy_id TEXT NOT NULL,
  status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_event
  ON events(account, vacancy_id, status, note, run_id, created_at);
CREATE TABLE IF NOT EXISTS reservations (
  account TEXT NOT NULL, vacancy_id TEXT NOT NULL, run_id TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY (account, vacancy_id)
);
CREATE TABLE IF NOT EXISTS negotiation_statuses (
  account TEXT NOT NULL, vacancy_id TEXT NOT NULL, status TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (account, vacancy_id)
);
CREATE TABLE IF NOT EXISTS sync_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL DEFAULT 'default',
  source TEXT NOT NULL, status TEXT NOT NULL, item_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_imports (
  account TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_path TEXT NOT NULL,
  physical_data_lines INTEGER NOT NULL, logical_rows INTEGER NOT NULL,
  imported_at TEXT NOT NULL, PRIMARY KEY (account, source_sha256)
);
"""

TERMINAL_STATUSES = {"success", "already_applied"}
BLOCKED_STATUSES = TERMINAL_STATUSES | {"unknown", "submitting"}
STATUS_RANK = {
    "prepared": 0,
    "skipped": 0,
    "failed_before_submit": 1,
    "needs_manual": 1,
    "unknown": 2,
    "submitting": 3,
    "already_applied": 4,
    "success": 4,
}
LEGACY_STATUS_MAP = {
    "timeout": "unknown",
    "error": "failed_before_submit",
    "no_button": "needs_manual",
    "closed": "skipped",
    "dry-run": "prepared",
}


@dataclass(frozen=True)
class ImportReport:
    source_sha256: str
    physical_data_lines: int
    logical_rows: int
    events_added: int
    already_imported: bool


def now() -> str:
    return datetime.now(UTC).isoformat()


def _legacy_status(value: str) -> str:
    status = value.strip() or "unknown"
    if status.startswith("skipped_"):
        return "skipped"
    return LEGACY_STATUS_MAP.get(status, status)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sync_snapshots)")}
        if "account" not in columns:
            conn.execute("ALTER TABLE sync_snapshots ADD COLUMN account TEXT NOT NULL DEFAULT 'default'")

    def read_statuses(self, account: str = "default") -> dict[str, str]:
        """Read an existing journal without creating a database or schema."""
        if not self.path.exists():
            return {}
        uri = f"file:{self.path.resolve()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return {}
        conn.row_factory = sqlite3.Row
        try:
            return {row["vacancy_id"]: row["status"] for row in conn.execute(
                "SELECT vacancy_id,status FROM attempts WHERE account=?", (account,))}
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def import_csv(self, path: Path, account: str = "default") -> ImportReport:
        """Import one legacy CSV exactly once per account and source digest."""
        with path.open("rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
        physical_data_lines = max(0, path.read_bytes().count(b"\n") - 1)
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        with self.connect() as conn:
            imported = conn.execute(
                "SELECT 1 FROM legacy_imports WHERE account=? AND source_sha256=?", (account, digest)
            ).fetchone()
            if imported:
                return ImportReport(digest, physical_data_lines, len(rows), 0, True)
            events_added = 0
            for row_number, row in enumerate(rows, 1):
                vacancy_id = str(row.get("vacancy_id") or row.get("id") or "").strip()
                if not vacancy_id:
                    continue
                status = _legacy_status(str(row.get("status") or "unknown"))
                timestamp = str(row.get("timestamp") or "1970-01-01T00:00:00+00:00")
                run_id = f"legacy-import:{digest[:16]}:{row_number}"
                existing = conn.execute(
                    "SELECT status FROM attempts WHERE account=? AND vacancy_id=?", (account, vacancy_id)
                ).fetchone()
                values = (
                    row.get("name", ""), row.get("company", ""), row.get("url", ""),
                    int(row.get("score") or 0), row.get("resume", ""), status, row.get("note", ""),
                    run_id, timestamp,
                )
                if existing is None:
                    conn.execute(
                        """INSERT INTO attempts
                        (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (account, vacancy_id, *values[:8], timestamp, values[8]),
                    )
                elif STATUS_RANK.get(status, 0) >= STATUS_RANK.get(existing["status"], 0):
                    conn.execute(
                        """UPDATE attempts SET name=?,company=?,url=?,score=?,resume=?,status=?,note=?,
                        run_id=?,updated_at=? WHERE account=? AND vacancy_id=?""",
                        (*values, account, vacancy_id),
                    )
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO events(account,vacancy_id,status,note,run_id,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    (account, vacancy_id, status, row.get("note", ""), run_id, timestamp),
                )
                events_added += int(inserted.rowcount > 0)
            conn.execute(
                """INSERT INTO legacy_imports
                (account,source_sha256,source_path,physical_data_lines,logical_rows,imported_at)
                VALUES(?,?,?,?,?,?)""",
                (account, digest, str(path.resolve()), physical_data_lines, len(rows), now()),
            )
        return ImportReport(digest, physical_data_lines, len(rows), events_added, False)

    def statuses(self, account: str = "default") -> dict[str, str]:
        return self.read_statuses(account) if self.path.exists() else {}

    def blocked_ids(self, account: str = "default") -> set[str]:
        return {
            vacancy_id for vacancy_id, status in self.read_statuses(account).items()
            if status in BLOCKED_STATUSES
        }

    def start_run(self, run_id: str, account: str, mode: str, input_path: Path,
                  requested_limit: int, items: list[dict[str, Any]]) -> None:
        timestamp = now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runs(run_id,account,mode,input_path,requested_limit,selected_count,status,started_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, account, mode, str(input_path.resolve()), requested_limit, len(items), "running", timestamp),
            )
            for ordinal, item in enumerate(items, 1):
                conn.execute(
                    """INSERT INTO run_items
                    (run_id,vacancy_id,ordinal,name,company,url,score,resume,status,note,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, str(item.get("id", "")), ordinal, item.get("name", ""), item.get("company", ""),
                     item.get("url", ""), int(item.get("score", 0)), item.get("resume", ""), "prepared", "",
                     timestamp, timestamp),
                )

    def finish_run(self, run_id: str, status: str, stop_reason: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?,stop_reason=?,finished_at=? WHERE run_id=?",
                (status, stop_reason, now(), run_id),
            )

    def mark_run_item(self, run_id: str, vacancy_id: str, status: str, note: str = "") -> None:
        if not run_id:
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE run_items SET status=?,note=?,updated_at=? WHERE run_id=? AND vacancy_id=?",
                (status, note, now(), run_id, vacancy_id),
            )

    def run_summary(self, run_id: str) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                return None
            counts = {
                row["status"]: row["count"] for row in conn.execute(
                    "SELECT status,COUNT(*) AS count FROM run_items WHERE run_id=? GROUP BY status", (run_id,)
                )
            }
        return {"run": dict(run), "counts": counts}

    def record(self, item: dict[str, Any], status: str, note: str = "", run_id: str = "",
               account: str = "default") -> None:
        timestamp = now()
        vacancy_id = str(item.get("id", ""))
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO attempts
                (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,vacancy_id) DO UPDATE SET
                name=excluded.name, company=excluded.company, url=excluded.url,
                score=excluded.score, resume=excluded.resume, status=excluded.status,
                note=excluded.note, run_id=excluded.run_id, updated_at=excluded.updated_at""",
                (account, vacancy_id, item.get("name", ""), item.get("company", ""), item.get("url", ""),
                 int(item.get("score", 0)), item.get("resume", ""), status, note, run_id, timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO events(account,vacancy_id,status,note,run_id,created_at) VALUES(?,?,?,?,?,?)",
                (account, vacancy_id, status, note, run_id, timestamp),
            )
            if run_id:
                conn.execute(
                    "UPDATE run_items SET status=?,note=?,updated_at=? WHERE run_id=? AND vacancy_id=?",
                    (status, note, timestamp, run_id, vacancy_id),
                )

    def reserve(self, item: dict[str, Any], run_id: str, per_run: int, per_day: int,
                account: str = "default") -> tuple[bool, str]:
        """Atomically deduplicate and reserve exactly one potential submission."""
        vacancy_id = str(item.get("id", ""))
        if not vacancy_id:
            return False, "missing vacancy id"
        timestamp = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM attempts WHERE account=? AND vacancy_id=?", (account, vacancy_id)
            ).fetchone()
            if existing and existing["status"] in BLOCKED_STATUSES:
                return False, f"already handled ({existing['status']})"
            day_prefix = datetime.now(UTC).date().isoformat()
            run_count = conn.execute(
                "SELECT COUNT(*) FROM reservations WHERE account=? AND run_id=?", (account, run_id)
            ).fetchone()[0]
            day_count = conn.execute(
                "SELECT COUNT(*) FROM reservations WHERE account=? AND created_at LIKE ?",
                (account, f"{day_prefix}%"),
            ).fetchone()[0]
            if run_count >= per_run:
                return False, f"run budget exhausted ({per_run})"
            if day_count >= per_day:
                return False, f"daily budget exhausted ({per_day})"
            try:
                conn.execute(
                    "INSERT INTO reservations(account,vacancy_id,run_id,created_at) VALUES(?,?,?,?)",
                    (account, vacancy_id, run_id, timestamp),
                )
            except sqlite3.IntegrityError:
                return False, "already reserved"
            values = (
                account, vacancy_id, item.get("name", ""), item.get("company", ""), item.get("url", ""),
                int(item.get("score", 0)), item.get("resume", ""), "submitting", "submission started",
                run_id, timestamp, timestamp,
            )
            conn.execute(
                """INSERT INTO attempts
                (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,vacancy_id) DO UPDATE SET status='submitting',run_id=excluded.run_id,
                note=excluded.note,updated_at=excluded.updated_at""",
                values,
            )
            conn.execute(
                "INSERT INTO events(account,vacancy_id,status,note,run_id,created_at) VALUES(?,?,?,?,?,?)",
                (account, vacancy_id, "submitting", "submission started", run_id, timestamp),
            )
            conn.execute(
                "UPDATE run_items SET status='submitting',note='submission started',updated_at=? "
                "WHERE run_id=? AND vacancy_id=?",
                (timestamp, run_id, vacancy_id),
            )
        return True, "reserved"

    def count(self, account: str = "default") -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) n FROM attempts WHERE account=? GROUP BY status", (account,)
            )
            return {row["status"]: row["n"] for row in rows}

    def event_count(self, account: str = "default") -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events WHERE account=?", (account,)).fetchone()[0])

    def save_sync_snapshot(self, source: str, status: str, item_count: int = 0,
                           error: str = "", account: str = "default") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO sync_snapshots(account,source,status,item_count,error,created_at)
                VALUES(?,?,?,?,?,?)""",
                (account, source, status, item_count, error, now()),
            )

    def replace_negotiation_statuses(self, rows: list[dict[str, Any]], account: str = "default") -> None:
        fetched_at = now()
        with self.connect() as conn:
            for row in rows:
                vacancy_id = str(row.get("vacancy_id") or row.get("id") or "")
                if not vacancy_id:
                    continue
                conn.execute(
                    """INSERT INTO negotiation_statuses
                    (account,vacancy_id,status,name,company,updated_at,fetched_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(account,vacancy_id) DO UPDATE SET status=excluded.status,
                    name=excluded.name,company=excluded.company,updated_at=excluded.updated_at,
                    fetched_at=excluded.fetched_at""",
                    (account, vacancy_id, str(row.get("status", "")), row.get("name", ""),
                     row.get("company", ""), row.get("updated_at", ""), fetched_at),
                )

    def latest_sync(self, account: str = "default") -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT source,status,item_count,error,created_at FROM sync_snapshots
                WHERE account=? ORDER BY id DESC LIMIT 1""",
                (account,),
            ).fetchone()
            return dict(row) if row else None

    def reconcile(self, path: Path, account: str = "default") -> int:
        """Apply only explicit confirmed statuses; never retries an unknown attempt."""
        if path.suffix.lower() == ".json":
            rows = json.loads(path.read_text(encoding="utf-8"))
            rows = rows if isinstance(rows, list) else rows.get("items", [])
        else:
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        changed = 0
        for row in rows:
            confirmed = str(row.get("confirmed", "")).lower() in {"1", "true", "yes", "да"}
            status = str(row.get("status", ""))
            if not confirmed or status not in TERMINAL_STATUSES:
                continue
            item = {
                "id": row.get("vacancy_id") or row.get("id", ""), "name": row.get("name", ""),
                "company": row.get("company", ""), "url": row.get("url", ""),
                "score": row.get("score", 0), "resume": row.get("resume", ""),
            }
            self.record(item, status, "explicit reconciliation", "reconcile", account)
            changed += 1
        return changed

    @contextlib.contextmanager
    def run_lock(self):
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
