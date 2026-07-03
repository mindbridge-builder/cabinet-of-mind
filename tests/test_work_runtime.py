import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.work_runtime import WorkRuntime
from core.work_store import WorkStore


class WorkRuntimeTests(unittest.TestCase):
    def test_process_work_records_progress_artifact_and_success(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkStore(root / "work", stale_after_s=60)
            updates = []
            runtime = WorkRuntime(store, lambda payload: updates.append(payload))
            artifact = root / "out.txt"

            meta = runtime.start_process(
                title="demo work",
                command=[
                    sys.executable,
                    "-c",
                    (
                        "import json, pathlib; "
                        "print(json.dumps({'type':'progress','current':1,'total':2}), flush=True); "
                        f"pathlib.Path(r'{artifact}').write_text('done'); "
                        f"print(json.dumps({{'type':'artifact','path':r'{artifact}'}}), flush=True); "
                        "print(json.dumps({'type':'result','summary':'ok'}), flush=True)"
                    ),
                ],
                cwd=root,
                owner="dro",
            )

            deadline = time.time() + 5
            final = store.get(meta["work_id"])
            while time.time() < deadline:
                final = store.get(meta["work_id"])
                if final and final.get("status") == "succeeded":
                    break
                time.sleep(0.05)

            self.assertEqual(final["status"], "succeeded")
            self.assertEqual(final["progress"]["current"], 1)
            self.assertEqual(final["summary"], "ok")
            self.assertEqual(final["artifacts"][0]["path"], str(artifact))
            self.assertTrue(any(item.get("type") == "work_update" for item in updates))

    def test_handoff_metrics_are_recorded_for_workruntime_process(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkStore(root / "work", stale_after_s=60)
            runtime = WorkRuntime(store)

            meta = runtime.start_process(
                title="handoff work",
                command=[sys.executable, "-c", "print('ok')"],
                cwd=root,
                owner="dro",
                executor="workruntime_test",
                handoff={
                    "handoff_id": "handoff-test-1",
                    "template_id": "run_command",
                    "action_id": "demo.action",
                    "task_ref": "test-task",
                    "expected_files_changed": ["out.txt"],
                    "started_by": "dro",
                    "tokens_sage_pre": 123,
                    "tokens_sage_verify": 45,
                    "estimated_direct_cost": "small",
                },
            )

            deadline = time.time() + 5
            final = store.get(meta["work_id"])
            while time.time() < deadline:
                final = store.get(meta["work_id"])
                if final and final.get("status") == "succeeded":
                    break
                time.sleep(0.05)

            self.assertEqual(final["handoff_id"], "handoff-test-1")
            self.assertEqual(final["template_id"], "run_command")
            events = store.read_events(meta["work_id"])
            self.assertTrue(any(item.get("type") == "handoff_started" for item in events))
            self.assertTrue(any(item.get("type") == "handoff_finished" for item in events))

            metrics_path = root / ".cabinet" / "metrics" / "handoffs.jsonl"
            rows = [
                json.loads(line)
                for line in metrics_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([row["type"] for row in rows], ["handoff_started", "handoff_finished"])
            self.assertEqual(rows[0]["handoff_id"], "handoff-test-1")
            self.assertEqual(rows[0]["template_id"], "run_command")
            self.assertEqual(rows[0]["tokens_sage_pre"], 123)
            self.assertEqual(rows[1]["result"], "clean")
            self.assertEqual(rows[1]["tokens_sage_verify"], 45)
            self.assertIn("wall_time", rows[1])

    def test_cancel_marks_work_cancelled(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkStore(root / "work", stale_after_s=60)
            runtime = WorkRuntime(store)

            meta = runtime.start_process(
                title="slow work",
                command=[sys.executable, "-c", "import time; time.sleep(10)"],
                cwd=root,
            )
            self.assertTrue(runtime.cancel(meta["work_id"]))
            final = store.get(meta["work_id"])
            self.assertEqual(final["status"], "cancelled")

    def test_store_marks_stale_running_work(self):
        with TemporaryDirectory() as tmp:
            store = WorkStore(Path(tmp) / "work", stale_after_s=0)
            meta = store.create(title="stale")
            store.mark_running(meta["work_id"], pid=123)

            items = store.list()

            self.assertEqual(items[0]["status"], "stale")


if __name__ == "__main__":
    unittest.main()
