import json
import os
import tempfile
import unittest
from pathlib import Path

from core.owned.run_store import RunStore
from core.owned.task_policy import account_timeout_seconds
from core.owned.account_queue import AccountProcessQueue


class RunStoreTests(unittest.TestCase):
    def test_upserts_and_emits_status_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "send-status.json", keep_per_account=2, max_runs=10)
            entry = {"run_id": "r1", "unique_id": "u1", "account": "A", "status": "running"}

            store.upsert(entry)
            entry["status"] = "ok"
            saved = store.upsert(entry)

            self.assertEqual(saved["run_id"], "r1")
            self.assertEqual(store.get("r1")["status"], "ok")
            events = store.events()
            self.assertEqual([event["to"] for event in events[:2]], ["ok", "running"])

    def test_retention_is_per_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "send-status.json", keep_per_account=2, max_runs=10)
            for index in range(3):
                store.upsert({"run_id": f"a{index}", "unique_id": "a", "status": "ok"})
            for index in range(2):
                store.upsert({"run_id": f"b{index}", "unique_id": "b", "status": "ok"})

            runs = store.read()
            self.assertEqual({run["run_id"] for run in runs}, {"a1", "a2", "b0", "b1"})
            json.loads((Path(directory) / "send-status.json").read_text(encoding="utf-8"))


class TaskPolicyTests(unittest.TestCase):
    def test_timeout_policy_is_safe(self):
        original = os.environ.get("PANEL_ACCOUNT_TIMEOUT_SECONDS")
        try:
            os.environ["PANEL_ACCOUNT_TIMEOUT_SECONDS"] = "bad"
            self.assertEqual(account_timeout_seconds(), 600)
            self.assertEqual(account_timeout_seconds("0"), 1)
            self.assertEqual(account_timeout_seconds("999999"), 24 * 60 * 60)
        finally:
            if original is None:
                os.environ.pop("PANEL_ACCOUNT_TIMEOUT_SECONDS", None)
            else:
                os.environ["PANEL_ACCOUNT_TIMEOUT_SECONDS"] = original


class AccountQueueTests(unittest.TestCase):
    def test_queue_continues_after_spawn_failure(self):
        class QueueTimeout(Exception):
            pass

        class Process:
            def __init__(self, returncode=0):
                self.returncode = returncode

            def wait(self, timeout=None):
                return self.returncode

            def poll(self):
                return self.returncode

        seen = []

        def spawn(job):
            seen.append(job.unique_id)
            if job.unique_id == "bad":
                raise RuntimeError("cannot start")
            return Process()

        result = AccountProcessQueue(
            timeout_seconds=5,
            spawn=spawn,
            terminate=lambda process: None,
            timeout_error=QueueTimeout,
        ).run(
            [
                {"unique_id": "bad", "username": "bad"},
                {"unique_id": "good", "username": "good"},
            ]
        )

        self.assertEqual(seen, ["bad", "good"])
        self.assertEqual(result.completed, 1)
        self.assertEqual(result.failed, 1)

    def test_queue_marks_timeout_and_continues(self):
        class QueueTimeout(Exception):
            pass

        class Process:
            def __init__(self):
                self.returncode = None
                self.terminated = False

            def wait(self, timeout=None):
                raise QueueTimeout()

            def poll(self):
                return self.returncode

        processes = []
        timed_out = []

        def spawn(job):
            process = Process()
            processes.append(process)
            return process

        def terminate(process):
            process.terminated = True
            process.returncode = -9

        result = AccountProcessQueue(
            timeout_seconds=1,
            spawn=spawn,
            terminate=terminate,
            timeout_error=QueueTimeout,
            on_timeout=lambda job, started_at: timed_out.append(job.unique_id),
        ).run([{"unique_id": "u1", "username": "one"}])

        self.assertEqual(result.failed, 1)
        self.assertEqual(timed_out, ["u1"])
        self.assertTrue(processes[0].terminated)
