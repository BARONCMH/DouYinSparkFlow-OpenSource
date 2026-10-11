import json
import os
import tempfile
import unittest
from pathlib import Path

from core.owned.run_store import RunStore
from core.owned.task_policy import account_timeout_seconds


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
