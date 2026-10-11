"""Small SQLite backend for task runs and lifecycle events.

The panel keeps the JSON files as a compatibility mirror.  SQLite is opt-in
through ``PANEL_RUN_STORE_SQLITE=1`` so an existing deployment can migrate
without changing its public file format or requiring a third-party package.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Iterable


class SQLiteRunStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _init_db(self) -> None:
        with self._lock, closing(self._connect()) as db:
            with db:
                db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_seq ON runs(seq DESC);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq DESC);
                """
                )

    @staticmethod
    def _payload(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        try:
            value = json.loads(str(row["payload"]))
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def count(self) -> int:
        with self._lock, closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

    def read(self, limit: int = 0, unique_id: str = "") -> list[dict]:
        query = "SELECT payload FROM runs"
        args: list[object] = []
        if unique_id:
            # Filtering payload JSON in SQLite would make this backend depend
            # on JSON1.  The panel only keeps a small bounded run set, so read
            # the ordered rows and filter in Python for portability.
            query += " ORDER BY seq DESC"
        else:
            query += " ORDER BY seq DESC"
        with self._lock, closing(self._connect()) as db:
            rows = db.execute(query, args).fetchall()
        output = []
        wanted = str(unique_id or "")
        for row in rows:
            item = self._payload(row)
            if not item:
                continue
            if wanted and str(item.get("unique_id") or "") != wanted:
                continue
            output.append(copy.deepcopy(item))
            if limit and len(output) >= int(limit):
                break
        return output

    def get(self, run_id: str) -> dict | None:
        with self._lock, closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
        return copy.deepcopy(self._payload(row))

    def replace_runs(self, runs: Iterable[dict]) -> None:
        records = [copy.deepcopy(item) for item in runs if isinstance(item, dict)]
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM runs")
            seq = len(records)
            for item in reversed(records):
                run_id = str(item.get("run_id") or "")
                if not run_id:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO runs(run_id,seq,payload) VALUES(?,?,?)",
                    (run_id, seq, json.dumps(item, ensure_ascii=False, separators=(",", ":"))),
                )
                seq -= 1
            db.execute("COMMIT")

    def upsert(self, item: dict) -> None:
        record = copy.deepcopy(item)
        run_id = str(record.get("run_id") or "")
        if not run_id:
            return
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT COALESCE(MAX(seq),0) + 1 FROM runs").fetchone()[0]
            db.execute(
                "INSERT INTO runs(run_id,seq,payload) VALUES(?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET seq=excluded.seq,payload=excluded.payload",
                (run_id, int(current), json.dumps(record, ensure_ascii=False, separators=(",", ":"))),
            )
            db.execute("COMMIT")

    def add_event(self, event: dict) -> None:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            return
        with self._lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            seq = int(db.execute("SELECT COALESCE(MAX(seq),0) + 1 FROM events").fetchone()[0])
            db.execute(
                "INSERT OR REPLACE INTO events(event_id,seq,payload) VALUES(?,?,?)",
                (event_id, seq, json.dumps(event, ensure_ascii=False, separators=(",", ":"))),
            )
            db.execute("COMMIT")

    def events(self, limit: int = 100) -> list[dict]:
        with self._lock, closing(self._connect()) as db:
            rows = db.execute("SELECT payload FROM events ORDER BY seq DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        output = []
        for row in rows:
            item = self._payload(row)
            if item:
                output.append(item)
        return output

    def health(self) -> dict:
        runs = self.read()
        active = sum(str(item.get("status") or "") in {"running", "queued"} for item in runs)
        latest = max(
            (str(item.get("updated_at") or item.get("finished_at") or item.get("at") or "") for item in runs),
            default="",
        )
        try:
            bytes_used = self.path.stat().st_size
        except OSError:
            bytes_used = 0
        return {
            "ok": True,
            "stored_runs": len(runs),
            "active_runs": active,
            "latest_at": latest,
            "bytes": bytes_used,
            "events": len(self.events(1000)),
            "backend": "sqlite",
        }

    def import_legacy(self, runs: Iterable[dict], events: Iterable[dict]) -> bool:
        """Import JSON once, without overwriting an already-used database."""
        if self.count():
            return False
        records = [item for item in runs if isinstance(item, dict)]
        self.replace_runs(records)
        event_list = [item for item in events if isinstance(item, dict)]
        for event in reversed(event_list):
            self.add_event(event)
        return bool(records or event_list)
