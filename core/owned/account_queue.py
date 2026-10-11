"""Project-owned sequential account process queue.

The browser adapter only knows how to process one account.  This queue owns
the policy around account isolation: each account gets its own child process,
its own timeout, and its own terminal callback.  A failure therefore cannot
prevent the following account from being attempted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


@dataclass(frozen=True)
class AccountJob:
    user: Mapping[str, Any]
    unique_id: str
    account: str


@dataclass(frozen=True)
class QueueResult:
    completed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0


class AccountProcessQueue:
    """Run account jobs one at a time with independent process limits."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        spawn: Callable[[AccountJob], Any],
        terminate: Callable[[Any], None],
        timeout_error: type[BaseException],
        on_start: Callable[[AccountJob, float], None] | None = None,
        on_complete: Callable[[AccountJob, float, Any], None] | None = None,
        on_spawn_failure: Callable[[AccountJob, float, BaseException], None] | None = None,
        on_process_failure: Callable[[AccountJob, float, Any], None] | None = None,
        on_timeout: Callable[[AccountJob, float], None] | None = None,
    ) -> None:
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.spawn = spawn
        self.terminate = terminate
        self.timeout_error = timeout_error
        self.on_start = on_start
        self.on_complete = on_complete
        self.on_spawn_failure = on_spawn_failure
        self.on_process_failure = on_process_failure
        self.on_timeout = on_timeout

    @staticmethod
    def jobs(users: Iterable[Mapping[str, Any]]) -> list[AccountJob]:
        output = []
        for user in users:
            uid = str(user.get("unique_id") or "").strip()
            account = str(user.get("username") or uid or "抖音账号")
            output.append(AccountJob(user=user, unique_id=uid, account=account))
        return output

    def run(self, users: Iterable[Mapping[str, Any]]) -> QueueResult:
        completed = failed = skipped = 0
        for job in self.jobs(users):
            if not job.unique_id:
                skipped += 1
                failed += 1
                continue
            started_at = time.time()
            if self.on_start is not None:
                self.on_start(job, started_at)
            try:
                process = self.spawn(job)
            except Exception as error:
                failed += 1
                if self.on_spawn_failure is not None:
                    self.on_spawn_failure(job, started_at, error)
                continue
            try:
                process.wait(timeout=self.timeout_seconds)
            except self.timeout_error:
                # A process can exit just as wait reaches its deadline.  Read
                # its final state before converting it into a timeout.
                if process.poll() is not None:
                    completed += 1
                    if self.on_complete is not None:
                        self.on_complete(job, started_at, process.returncode)
                    if process.returncode != 0:
                        failed += 1
                        if self.on_process_failure is not None:
                            self.on_process_failure(job, started_at, process.returncode)
                    continue
                failed += 1
                self.terminate(process)
                if self.on_timeout is not None:
                    self.on_timeout(job, started_at)
            else:
                completed += 1
                if self.on_complete is not None:
                    self.on_complete(job, started_at, process.returncode)
                if process.returncode != 0:
                    failed += 1
                    if self.on_process_failure is not None:
                        self.on_process_failure(job, started_at, process.returncode)
        return QueueResult(completed=completed, failed=failed, skipped=skipped)
