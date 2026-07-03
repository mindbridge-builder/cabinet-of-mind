from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from adapters.base import Adapter
from adapters.codex_cli import (
    CodexAdapter,
    _CodexDraftStreamer,
    _communicate_with_startup_watchdog,
    _emit_codex_line_progress,
    _extract_codex_jsonl,
    _partial_text_for_watchdog_error,
)


class DummyAdapter(Adapter):
    def call(self, *args, **kwargs):
        raise NotImplementedError

    def healthcheck(self):
        return True, "ok"


class CodexJsonParserTests(unittest.TestCase):
    def test_extracts_agent_message_and_shell_tool(self):
        stdout = "\n".join([
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"item.completed","item":{"type":"function_call","name":"shell_command","call_id":"c1"}}',
            '{"type":"agent_message","message":"done"}',
        ])

        text, tools, fetch_count, metrics = _extract_codex_jsonl(stdout)

        self.assertEqual(text, "done")
        self.assertEqual(tools, ["shell_command"])
        self.assertEqual(fetch_count, 0)
        self.assertEqual(metrics["json_events"], 3)

    def test_extracts_nested_assistant_message_and_successful_fetch(self):
        stdout = "\n".join([
            '{"type":"response_item","payload":{"type":"function_call","name":"web_fetch","call_id":"w1"}}',
            '{"type":"response_item","payload":{"type":"function_call_output","call_id":"w1","output":"page text"}}',
            '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"answer"}]}}',
        ])

        text, tools, fetch_count, _metrics = _extract_codex_jsonl(stdout)

        self.assertEqual(text, "answer")
        self.assertEqual(tools, ["web_fetch"])
        self.assertEqual(fetch_count, 1)

    def test_draft_streamer_emits_growing_text(self):
        events = []
        draft = _CodexDraftStreamer(events.append, "dro")
        draft.EMIT_INTERVAL = 0  # no throttling in tests
        for line in (
            '{"type":"item.completed","item":{"type":"function_call","name":"shell_command","call_id":"c1"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"Let me look at the code first."}}',
            '{"type":"agent_message","message":"Done: here is the output."}',
        ):
            _emit_codex_line_progress(line, events.append, "dro", draft)

        drafts = [e for e in events if e["type"] == "agent_text_draft"]
        self.assertEqual(len(drafts), 2)
        self.assertEqual(drafts[0]["text"], "Let me look at the code first.")
        self.assertEqual(drafts[1]["text"], "Let me look at the code first.\n\nDone: here is the output.")
        self.assertTrue(all(e["agent"] == "dro" for e in drafts))

    def test_draft_streamer_handles_deltas(self):
        events = []
        draft = _CodexDraftStreamer(events.append, "dro")
        draft.EMIT_INTERVAL = 0
        for line in (
            '{"type":"agent_message_delta","delta":"Think"}',
            '{"type":"agent_message_delta","delta":"ing."}',
        ):
            _emit_codex_line_progress(line, events.append, "dro", draft)
        self.assertEqual(events[-1]["text"], "Thinking.")

    def test_runtime_health_flips_red_then_green(self):
        adapter = DummyAdapter("dro", "Diderot", Path.cwd())

        adapter.mark_runtime_error("limit: resets 21:30")
        ok, msg = adapter.runtime_health()
        self.assertFalse(ok)
        self.assertIn("limit", msg)

        adapter.mark_runtime_ok()
        ok, msg = adapter.runtime_health()
        self.assertTrue(ok)
        self.assertEqual(msg, "ok")

    def test_startup_watchdog_fails_silent_process(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        with mock.patch("adapters.codex_cli._kill_process_tree", lambda p: p.kill()):
            stdout, stderr, error = _communicate_with_startup_watchdog(
                proc,
                "prompt",
                timeout=5,
                startup_timeout=0.2,
            )

        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        self.assertIn("startup timeout", error)

    def test_watchdog_fails_fast_on_codex_403(self):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys, time; "
                    "sys.stderr.write('failed to connect: HTTP error: 403 Forbidden\\n'); "
                    "sys.stderr.flush(); time.sleep(5)"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        with mock.patch("adapters.codex_cli._kill_process_tree", lambda p: p.kill()):
            _stdout, stderr, error = _communicate_with_startup_watchdog(
                proc,
                "prompt",
                timeout=5,
                startup_timeout=2,
                idle_timeout=4,
            )

        self.assertIn("403 Forbidden", stderr)
        self.assertIn("fatal Codex CLI output", error)

    def test_watchdog_fails_after_output_goes_idle(self):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time; print('{\"type\":\"thread.started\"}', flush=True); time.sleep(5)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        with mock.patch("adapters.codex_cli._kill_process_tree", lambda p: p.kill()):
            stdout, _stderr, error = _communicate_with_startup_watchdog(
                proc,
                "prompt",
                timeout=5,
                startup_timeout=2,
                idle_timeout=0.2,
            )

        self.assertIn("thread.started", stdout)
        self.assertIn("idle timeout", error)

    def test_partial_text_is_extracted_for_timeout_errors_only(self):
        stdout = "\n".join([
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"agent_message","message":"partial opinion"}',
        ])

        text, tools, fetch_count, metrics = _partial_text_for_watchdog_error(
            stdout,
            "idle timeout 300s without Codex output",
        )
        fatal_text, _fatal_tools, _fatal_fetch_count, _fatal_metrics = _partial_text_for_watchdog_error(
            stdout,
            "fatal Codex CLI output: 403 Forbidden",
        )

        self.assertEqual(text, "partial opinion")
        self.assertEqual(tools, [])
        self.assertEqual(fetch_count, 0)
        self.assertEqual(metrics["json_events"], 2)
        self.assertEqual(fatal_text, "")

    def test_codex_prompt_clips_large_history_items(self):
        adapter = CodexAdapter(codex_cmd=sys.executable)
        huge_text = "x" * 30000

        prompt = adapter._build_prompt(
            "system",
            [{"role": "DIDEROT", "type": "message", "timestamp": "t", "text": huge_text}],
            "CABINET_HISTORY_LIMIT: 4\n\n@dro ping",
        )

        self.assertLess(len(prompt), 10000)
        self.assertIn("TRUNCATED", prompt)
        self.assertNotIn("x" * 10000, prompt)


if __name__ == "__main__":
    unittest.main()
