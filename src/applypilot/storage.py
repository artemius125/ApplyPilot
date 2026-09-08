from __future__ import annotations

import contextlib
import csv
import fcntl
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
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
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, status TEXT NOT NULL,
  item_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
"""

TERMINAL_STATUSES = {"success", "already_applied"}
BLOCKED_STATUSES = TERMINAL_STATUSES | {"unknown", "submitting"}
STATUS_RANK = {
    "prepared": 0,
    "failed_before_submit": 1,
    "needs_manual": 1,
    "skipped": 1,
    "unknown": 2,
    "submitting": 3,
    "already_applied": 4,
    "success": 4,
}


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

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

    def import_csv(self, path: Path, account: str = "default") -> int:
        imported = 0
        with path.open(newline="", encoding="utf-8") as fh, self.connect() as conn:
            for row_number, row in enumerate(csv.DictReader(fh), 1):
                vid = str(row.get("vacancy_id") or row.get("id") or "").strip()
                if not vid:
                    continue
                status = str(row.get("status") or "unknown")
                if status == "timeout":
                    status = "unknown"
                if status == "error":
                    status = "failed_before_submit"
                timestamp = str(row.get("timestamp") or "1970-01-01T00:00:00+00:00")
                run_id = f"legacy-import:{path.name}:{row_number}"
                existing = conn.execute(
                    "SELECT status FROM attempts WHERE account=? AND vacancy_id=?", (account, vid)
                ).fetchone()
                if existing is None:
                    conn.execute("""INSERT INTO attempts
                        (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (account, vid, row.get("name", ""), row.get("company", ""), row.get("url", ""),
                         int(row.get("score") or 0), row.get("resume", ""), status, row.get("note", ""),
                         run_id, timestamp, timestamp))
                elif STATUS_RANK.get(status, 0) >= STATUS_RANK.get(existing["status"], 0):
                    conn.execute("""UPDATE attempts SET name=?,company=?,url=?,score=?,resume=?,status=?,note=?,run_id=?,updated_at=?
                        WHERE account=? AND vacancy_id=?""",
                        (row.get("name", ""), row.get("company", ""), row.get("url", ""),
                         int(row.get("score") or 0), row.get("resume", ""), status, row.get("note", ""),
                         run_id, timestamp, account, vid))
                conn.execute("""INSERT OR IGNORE INTO events
                    (account,vacancy_id,status,note,run_id,created_at) VALUES(?,?,?,?,?,?)""",
                    (account, vid, status, row.get("note", ""), run_id, timestamp))
                imported += 1
        return imported

    def statuses(self, account: str = "default") -> dict[str, str]:
        return self.read_statuses(account) if self.path.exists() else {}

    def record(self, item: dict[str, Any], status: str, note: str = "", run_id: str = "",
               account: str = "default") -> None:
        timestamp = now()
        with self.connect() as conn:
            conn.execute("""INSERT INTO attempts
                (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,vacancy_id) DO UPDATE SET
                name=excluded.name, company=excluded.company, url=excluded.url,
                score=excluded.score, resume=excluded.resume, status=excluded.status,
                note=excluded.note, run_id=excluded.run_id, updated_at=excluded.updated_at""",
                (account, str(item.get("id", "")), item.get("name", ""), item.get("company", ""),
                 item.get("url", ""), int(item.get("score", 0)), item.get("resume", ""), status,
                 note, run_id, timestamp, timestamp))
            conn.execute("INSERT INTO events(account,vacancy_id,status,note,run_id,created_at) VALUES(?,?,?,?,?,?)",
                         (account, str(item.get("id", "")), status, note, run_id, timestamp))

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
                conn.execute("INSERT INTO reservations(account,vacancy_id,run_id,created_at) VALUES(?,?,?,?)",
                             (account, vacancy_id, run_id, timestamp))
            except sqlite3.IntegrityError:
                return False, "already reserved"
            values = (account, vacancy_id, item.get("name", ""), item.get("company", ""), item.get("url", ""),
                      int(item.get("score", 0)), item.get("resume", ""), "submitting", "submission started",
                      run_id, timestamp, timestamp)
            conn.execute("""INSERT INTO attempts
                (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,vacancy_id) DO UPDATE SET status='submitting',run_id=excluded.run_id,
                note=excluded.note,updated_at=excluded.updated_at""", values)
            conn.execute("INSERT INTO events(account,vacancy_id,status,note,run_id,created_at) VALUES(?,?,?,?,?,?)",
                         (account, vacancy_id, "submitting", "submission started", run_id, timestamp))
        return True, "reserved"

    def count(self, account: str = "default") -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status,COUNT(*) n FROM attempts WHERE account=? GROUP BY status", (account,))
            return {row["status"]: row["n"] for row in rows}

    def event_count(self, account: str = "default") -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events WHERE account=?", (account,)).fetchone()[0])

    def save_sync_snapshot(self, source: str, status: str, item_count: int = 0,
                           error: str = "") -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO sync_snapshots(source,status,item_count,error,created_at) VALUES(?,?,?,?,?)",
                         (source, status, item_count, error, now()))

    def replace_negotiation_statuses(self, rows: list[dict[str, Any]], account: str = "default") -> None:
        fetched_at = now()
        with self.connect() as conn:
            for row in rows:
                vacancy_id = str(row.get("vacancy_id") or row.get("id") or "")
                if not vacancy_id:
                    continue
                conn.execute("""INSERT INTO negotiation_statuses
                    (account,vacancy_id,status,name,company,updated_at,fetched_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(account,vacancy_id) DO UPDATE SET status=excluded.status,
                    name=excluded.name,company=excluded.company,updated_at=excluded.updated_at,
                    fetched_at=excluded.fetched_at""",
                    (account, vacancy_id, str(row.get("status", "")), row.get("name", ""),
                     row.get("company", ""), row.get("updated_at", ""), fetched_at))

    def latest_sync(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT source,status,item_count,error,created_at FROM sync_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def can_submit(self, account: str, run_id: str, per_run: int, per_day: int) -> tuple[bool, str]:
        with self.connect() as conn:
            run_count = conn.execute(
                "SELECT COUNT(*) FROM reservations WHERE account=? AND run_id=?", (account, run_id)
            ).fetchone()[0]
            day_prefix = datetime.now(UTC).date().isoformat()
            day_count = conn.execute(
                "SELECT COUNT(*) FROM reservations WHERE account=? AND created_at LIKE ?",
                (account, f"{day_prefix}%")).fetchone()[0]
        if run_count >= per_run:
            return False, f"run budget exhausted ({per_run})"
        if day_count >= per_day:
            return False, f"daily budget exhausted ({per_day})"
        return True, "ok"

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
            item = {"id": row.get("vacancy_id") or row.get("id", ""), "name": row.get("name", ""),
                    "company": row.get("company", ""), "url": row.get("url", ""), "score": row.get("score", 0),
                    "resume": row.get("resume", "")}
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
