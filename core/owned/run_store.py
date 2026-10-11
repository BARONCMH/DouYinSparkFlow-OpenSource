"""Durable task-run storage owned by the panel.

The first panel version let several worker processes read and rewrite the
same JSON file directly.  That made a late timeout update able to overwrite a
newer run, and made the panel and worker each implement subtly different
retention rules.  This module provides one small, process-safe boundary for
run records while keeping the existing JSON format compatible.

The store deliberately does not persist cookies or other credentials.  It
only handles task metadata and a small lifecycle event journal.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator
from uuid import uuid4

try:  # Linux containers have fcntl; the fallback keeps local Windows tests usable.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class RunStore:
    """Store recent account runs and lifecycle events in private files.

    ``send-status.json`` remains the public compatibility format consumed by
    existing panel code.  Every write is atomic and protected by a sidecar
    lock, so concurrent account workers cannot lose each other's updates.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        keep_per_account: int = 25,
        max_runs: int = 100,
        event_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.event_path = Path(event_path) if event_path else self.path.with_name("run-events.jsonl")
        self.keep_per_account = max(1, int(keep_per_account))
        self.max_runs = max(1, int(max_runs))
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            handle = None
            try:
                handle = self.lock_path.open("a+", encoding="utf-8")
                try:
                    os.chmod(self.lock_path, 0o600)
                except OSError:
                    pass
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                if handle is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()

    def _read_unlocked(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return []
        runs = data.get("runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            return []
        return [copy.deepcopy(item) for item in runs if isinstance(item, dict)]

    def _write_unlocked(self, runs: Iterable[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"runs": list(runs)}, ensure_ascii=False, indent=1)
        fd, name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def _write_event_unlocked(self, event: dict) -> None:
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        fd = os.open(self.event_path, flags, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    @staticmethod
    def _account_key(run: dict) -> str:
        return str(run.get("unique_id") or run.get("account") or "")

    @staticmethod
    def _run_key(run: dict) -> str:
        return str(run.get("run_id") or "")

    def _trim(self, runs: list[dict]) -> list[dict]:
        kept: list[dict] = []
        seen: dict[str, int] = {}
        for run in runs:
            key = self._account_key(run)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= self.keep_per_account and len(kept) < self.max_runs:
                kept.append(run)
        return kept

    def read(self, limit: int = 0, unique_id: str = "") -> list[dict]:
        """Return newest-first records without exposing a mutable internal list."""
        with self._locked():
            runs = self._read_unlocked()
        if unique_id:
            wanted = str(unique_id)
            runs = [item for item in runs if str(item.get("unique_id") or "") == wanted]
        return runs[: max(0, int(limit))] if limit else runs

    def get(self, run_id: str) -> dict | None:
        wanted = str(run_id or "")
        if not wanted:
            return None
        for run in self.read():
            if self._run_key(run) == wanted:
                return run
        return None

    def upsert(self, entry: dict) -> dict:
        """Insert or replace a run and emit an event on status transitions."""
        if not isinstance(entry, dict):
            raise TypeError("run entry must be a dict")
        record = copy.deepcopy(entry)
        record.setdefault("run_id", uuid4().hex)
        record.setdefault("at", _now_text())
        record["updated_at"] = _now_text()
        run_id = self._run_key(record)
        with self._locked():
            runs = self._read_unlocked()
            index = next(
                (i for i, item in enumerate(runs) if self._run_key(item) == run_id),
                None,
            )
            previous = runs[index] if index is not None else None
            if index is None:
                runs.insert(0, record)
            else:
                runs[index] = record
            runs = self._trim(runs)
            self._write_unlocked(runs)
            old_status = str((previous or {}).get("status") or "")
            new_status = str(record.get("status") or "")
            if old_status != new_status and new_status:
                self._write_event_unlocked(
                    {
                        "event_id": uuid4().hex,
                        "at": record.get("updated_at") or _now_text(),
                        "type": "run.status",
                        "run_id": run_id,
                        "account": str(record.get("account") or ""),
                        "unique_id": str(record.get("unique_id") or ""),
                        "from": old_status,
                        "to": new_status,
                    }
                )
        return record

    def events(self, limit: int = 100) -> list[dict]:
        """Read recent lifecycle events, newest first."""
        try:
            lines = self.event_path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return []
        output: list[dict] = []
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict):
                output.append(item)
            if len(output) >= max(1, int(limit)):
                break
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
        }
