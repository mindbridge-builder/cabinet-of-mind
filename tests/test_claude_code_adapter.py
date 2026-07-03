"""Tests for ClaudeCodeAdapter — verifies the claude.exe binary path and adapter wiring."""
from pathlib import Path
import unittest

from adapters.claude_code_cli import (
    CLAUDE_EXE,
    ClaudeCodeAdapter,
    _emit_stream_progress,
    _TextDraftStreamer,
    parse_stream_json,
)


class ClaudeCodeAdapterTests(unittest.TestCase):

    def test_claude_exe_constant_points_to_real_file(self):
        self.assertTrue(
            Path(CLAUDE_EXE).exists(),
            f"claude.exe not found at {CLAUDE_EXE}",
        )

    def test_adapter_instantiates_without_error(self):
        a = ClaudeCodeAdapter(workspace=Path.cwd())
        self.assertEqual(a.claude_exe, CLAUDE_EXE)

    def test_adapter_has_no_node_exe_or_cli_js_attrs(self):
        a = ClaudeCodeAdapter(workspace=Path.cwd())
        self.assertFalse(hasattr(a, "node_exe"), "old node_exe attr still present")
        self.assertFalse(hasattr(a, "cli_js"), "old cli_js attr still present")

    def test_healthcheck_returns_ok(self):
        a = ClaudeCodeAdapter(workspace=Path.cwd())
        ok, msg = a.healthcheck()
        self.assertTrue(ok, f"healthcheck failed: {msg}")
        self.assertIn("ok", msg)

    def test_custom_claude_exe_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            ClaudeCodeAdapter(workspace=Path.cwd(), claude_exe=r"C:\nonexistent\claude.exe")


class ParseStreamJsonTests(unittest.TestCase):

    def test_extracts_result_text_and_rate_limit(self):
        stdout = "\n".join([
            '{"type":"system","subtype":"init"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}',
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1778676600,"rateLimitType":"five_hour","isUsingOverage":true}}',
            '{"type":"result","subtype":"success","is_error":false,"result":"Final answer"}',
        ])
        text, info, is_err = parse_stream_json(stdout)
        self.assertEqual(text, "Final answer")
        self.assertFalse(is_err)
        self.assertIsNotNone(info)
        self.assertEqual(info["status"], "rejected")
        self.assertEqual(info["resetsAt"], 1778676600)
        self.assertTrue(info["isUsingOverage"])

    def test_handles_empty_and_malformed_lines(self):
        stdout = "\n\nnot-json\n{\"type\":\"result\",\"result\":\"ok\",\"is_error\":false}\n"
        text, info, is_err = parse_stream_json(stdout)
        self.assertEqual(text, "ok")
        self.assertIsNone(info)
        self.assertFalse(is_err)

    def test_marks_error_when_result_is_error(self):
        stdout = '{"type":"result","is_error":true,"result":"boom"}'
        text, info, is_err = parse_stream_json(stdout)
        self.assertEqual(text, "boom")
        self.assertTrue(is_err)

    def test_ignores_stream_event_lines(self):
        stdout = "\n".join([
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"x"}}}',
            '{"type":"result","result":"final","is_error":false}',
        ])
        text, _info, is_err = parse_stream_json(stdout)
        self.assertEqual(text, "final")
        self.assertFalse(is_err)


class TextDraftStreamerTests(unittest.TestCase):
    def _collect(self):
        events = []
        streamer = _TextDraftStreamer(events.append, "hux")
        streamer.EMIT_INTERVAL = 0  # no throttling in tests
        return events, streamer

    def test_deltas_accumulate_into_draft(self):
        events, streamer = self._collect()
        for line in (
            '{"type":"stream_event","event":{"type":"content_block_start","content_block":{"type":"text"}}}',
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Wel"}}}',
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"come"}}}',
        ):
            _emit_stream_progress(line, events.append, "hux", streamer)
        self.assertTrue(events)
        self.assertEqual(events[-1]["type"], "agent_text_draft")
        self.assertEqual(events[-1]["agent"], "hux")
        self.assertEqual(events[-1]["text"], "Welcome")

    def test_assistant_message_resyncs_draft(self):
        events, streamer = self._collect()
        _emit_stream_progress(
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"draft with lost par"}}}',
            events.append, "hux", streamer,
        )
        # A full assistant message replaces the accumulated deltas.
        _emit_stream_progress(
            '{"type":"assistant","message":{"content":[{"type":"text","text":"First thought."}]}}',
            events.append, "hux", streamer,
        )
        _emit_stream_progress(
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Second"}}}',
            events.append, "hux", streamer,
        )
        self.assertEqual(events[-1]["text"], "First thought.\n\nSecond")

    def test_tool_use_events_still_emitted(self):
        events, streamer = self._collect()
        _emit_stream_progress(
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"a.py"}}]}}',
            events.append, "hux", streamer,
        )
        kinds = [e["type"] for e in events]
        self.assertIn("agent_tool_use", kinds)


if __name__ == "__main__":
    unittest.main()
