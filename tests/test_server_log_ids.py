import json
import shutil
import unittest
from pathlib import Path

import server


class ServerLogIdTests(unittest.TestCase):
    def test_load_log_continues_after_max_message_id(self):
        original = {
            "log_file": server.LOG_FILE,
            "archive_dir": server.LOG_ARCHIVE_DIR,
            "history": server._history,
            "next_id": server._next_id,
            "saved_count": server._saved_count,
        }
        tmp_path = Path("logs") / "test_tmp_server_log_ids"
        shutil.rmtree(tmp_path, ignore_errors=True)
        tmp_path.mkdir(parents=True)
        try:
            server.LOG_FILE = tmp_path / "CABINET_LOG.jsonl"
            server.LOG_ARCHIVE_DIR = tmp_path / "archive"
            server._history = []
            server._next_id = 1
            server._saved_count = 0
            rows = [
                {"id": "2", "role": "BOSS", "text": "old", "timestamp": "t"},
                {"id": "48", "role": "HUXLEY", "text": "newer", "timestamp": "t"},
            ]
            server.LOG_FILE.write_text(
                "".join(json.dumps(row, ensure_ascii=True) + "\n" for row in rows),
                encoding="utf-8",
            )

            server.load_log()

            self.assertEqual(server.next_id(), "49")
        finally:
            server.LOG_FILE = original["log_file"]
            server.LOG_ARCHIVE_DIR = original["archive_dir"]
            server._history = original["history"]
            server._next_id = original["next_id"]
            server._saved_count = original["saved_count"]
            shutil.rmtree(tmp_path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class GolRunParseTests(unittest.TestCase):
    def test_parses_run_tag(self):
        self.assertEqual(
            server.parse_gol_run("@gol run candidates_material_20"),
            "candidates_material_20",
        )
        self.assertEqual(
            server.parse_gol_run("  @GOL RUN Upload_Job.Reports-All  "),
            "Upload_Job.Reports-All",
        )

    def test_rejects_non_run_kes_messages(self):
        self.assertIsNone(server.parse_gol_run("@gol describe yourself"))
        self.assertIsNone(server.parse_gol_run("@gol run"))
        self.assertIsNone(server.parse_gol_run("@gol run two words"))
        self.assertIsNone(server.parse_gol_run("run candidates_material_20"))
        self.assertIsNone(server.parse_gol_run("@dro run candidates_material_20"))
