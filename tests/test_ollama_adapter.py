import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from adapters.ollama import OllamaAdapter
from core.work_runtime import WorkRuntime
from core.work_store import WorkStore


class RetryXmlAdapter(OllamaAdapter):
    def __init__(self):
        super().__init__(workspace=Path.cwd())
        self.calls = 0

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            raise urllib.error.HTTPError(
                url="http://127.0.0.1:11434/api/chat",
                code=500,
                msg="Internal Server Error",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"XML syntax error on line 3: unexpected EOF"}'),
            )
        return {
            "message": {
                "content": '@boss ok\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "done_reason": "stop",
        }


class CapturePayloadAdapter(OllamaAdapter):
    def __init__(self):
        super().__init__(workspace=Path.cwd())
        self.payloads = []

    def _post_chat(self, payload, timeout):
        self.payloads.append(payload)
        return {
            "message": {
                "content": '@boss ok\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "done_reason": "stop",
        }


class RetryRouteAdapter(OllamaAdapter):
    """Returns a response without routing JSON on first call, correct one on second."""
    def __init__(self):
        super().__init__(workspace=Path.cwd())
        self.calls = 0

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            # Missing routing JSON
            return {"message": {"content": "@boss Task complete."}, "done_reason": "stop"}
        return {
            "message": {
                "content": '@boss Task complete.\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "done_reason": "stop",
        }


class ToolThenTimeoutAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.target = workspace / "result.txt"
        self.target.write_text("tool result ok\n", encoding="utf-8")

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": str(self.target)},
                        }
                    }]
                }
            }
        raise TimeoutError("timed out")


class ToolThenFinalClaimsAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.target = workspace / "result.txt"

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": str(self.target), "content": "real\n"},
                        }
                    }]
                }
            }
        return {
            "message": {
                "content": (
                    "@hux - done\n"
                    "files_changed: fake.csv\n"
                    "commit: deadbee\n"
                    "verification: fake tests passed\n"
                    '{"route":"hux","write_intent":false,"arch_decision":false,"message":"done"}'
                )
            },
            "done_reason": "stop",
        }


class ToolThenEmptyThenFinalAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.target = workspace / "result.txt"

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": str(self.target), "content": "real\n"},
                        }
                    }]
                },
                "done_reason": "stop",
            }
        if self.calls in {2, 3}:
            return {"message": {"content": ""}, "done_reason": "stop"}
        return {
            "message": {
                "content": '@boss recovered\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "done_reason": "stop",
        }


class ToolThenAlwaysEmptyAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.target = workspace / "result.txt"

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": str(self.target), "content": "real\n"},
                        }
                    }]
                },
                "done_reason": "stop",
            }
        return {"message": {"content": ""}, "done_reason": "stop"}


class WriteFinalThenCommitAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.target = workspace / "result.txt"

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": str(self.target), "content": "real\n"},
                        }
                    }]
                },
                "done_reason": "stop",
            }
        if self.calls in {2, 3}:
            return {
                "message": {
                    "content": '@boss done\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
                },
                "done_reason": "stop",
            }
        if self.calls == 4:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "git_commit",
                            "arguments": {
                                "cwd": str(self.workspace),
                                "files": ["result.txt"],
                                "message": "test: commit result",
                            },
                        }
                    }]
                },
                "done_reason": "stop",
            }
        return {
            "message": {
                "content": '@boss committed\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "done_reason": "stop",
        }


class FailedWorkThenCommitAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace], work_runtime=object())
        self.calls = 0
        self.target = workspace / "result.txt"
        self.saw_postcondition_prompt = False

    def _tool_start_work(self, args: dict) -> dict:
        return {
            "type": "start_work",
            "work_id": "failed-work-1",
            "status": "running",
            "title": "failed work",
        }

    def _tool_work_status(self, args: dict) -> dict:
        return {
            "type": "work_status",
            "exists": True,
            "work": {
                "work_id": "failed-work-1",
                "status": "failed",
                "error": "process exited 1",
            },
        }

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if any(
            "WorkRuntime postcondition is not satisfied" in (message.get("content") or "")
            for message in payload.get("messages", [])
        ):
            self.saw_postcondition_prompt = True
        if self.calls == 1:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "run_work",
                            "arguments": {
                                "title": "failed work",
                                "cwd": str(self.workspace),
                                "command": ["python", "worker.py"],
                            },
                        }
                    }]
                },
                "done_reason": "stop",
            }
        if self.calls == 2:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "work_status",
                            "arguments": {"work_id": "failed-work-1"},
                        }
                    }]
                },
                "done_reason": "stop",
            }
        if self.calls == 3:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": str(self.target), "content": "partial\n"},
                        }
                    }]
                },
                "done_reason": "stop",
            }
        if self.calls == 4:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "git_commit",
                            "arguments": {
                                "cwd": str(self.workspace),
                                "files": ["result.txt"],
                                "message": "test: commit partial failed work",
                            },
                        }
                    }]
                },
                "done_reason": "stop",
            }
        return {
            "message": {
                "content": (
                    '@boss success\n'
                    '{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
                )
            },
            "done_reason": "stop",
        }


class JsonToolThenFinalAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.target = workspace / "result.txt"
        self.target.write_text("json tool ok\n", encoding="utf-8")

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            tool_json = json.dumps(
                {"name": "read_file", "arguments": {"path": str(self.target)}},
                indent=2,
            )
            return {
                "message": {
                    "content": "@boss\n\n" + tool_json
                },
                "done_reason": "stop",
            }
        return {
            "message": {
                "content": '@boss ok\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "done_reason": "stop",
        }


class QwenToolCallThenFinalAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.second_payload = None
        self.target = workspace / "result.txt"
        self.target.write_text("qwen tool ok\n", encoding="utf-8")

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            tool_json = json.dumps(
                {"name": "read_file", "arguments": {"path": str(self.target)}},
                indent=2,
            )
            return {
                "message": {
                    "content": f"<tool_call>\n{tool_json}\n</tool_call>"
                },
                "done_reason": "stop",
            }
        self.second_payload = payload
        return {
            "message": {
                "content": '@boss ok\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "done_reason": "stop",
        }


class RepeatingJsonToolAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.target = workspace / "result.txt"
        self.target.write_text("repeat tool ok\n", encoding="utf-8")

    def _post_chat(self, payload, timeout):
        self.calls += 1
        return {
            "message": {
                "content": json.dumps(
                    {"name": "read_file", "arguments": {"path": str(self.target)}},
                    indent=2,
                )
            },
            "done_reason": "stop",
        }


class ContextPressureAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.target = workspace / "result.txt"
        self.target.write_text("context ok\n", encoding="utf-8")

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": str(self.target)},
                        }
                    }]
                },
                "prompt_eval_count": 15000,
                "eval_count": 12,
                "done_reason": "stop",
            }
        return {
            "message": {
                "content": '@boss ok\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "prompt_eval_count": 15200,
            "eval_count": 20,
            "done_reason": "stop",
        }


class LargeReadPayloadAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.second_payload = None
        self.target = workspace / "large.txt"
        self.target.write_text("x" * (self.MAX_OUTPUT_CHARS + 5000), encoding="utf-8")

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": str(self.target)},
                        }
                    }]
                },
                "prompt_eval_count": 1000,
                "eval_count": 10,
                "done_reason": "stop",
            }
        self.second_payload = payload
        return {
            "message": {
                "content": '@boss ok\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "prompt_eval_count": 2000,
            "eval_count": 10,
            "done_reason": "stop",
        }


class ManyReadsAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace])
        self.calls = 0
        self.final_payload = None
        self.files = []
        for idx in range(5):
            path = workspace / f"file_{idx}.txt"
            path.write_text(f"content-{idx}\n" + ("x" * 1200), encoding="utf-8")
            self.files.append(path)

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if self.calls <= len(self.files):
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": str(self.files[self.calls - 1])},
                        }
                    }]
                },
                "prompt_eval_count": 1000 + self.calls,
                "eval_count": 10,
                "done_reason": "stop",
            }
        self.final_payload = payload
        return {
            "message": {
                "content": '@boss ok\n{"route":"","write_intent":false,"arch_decision":false,"message":"done"}'
            },
            "prompt_eval_count": 2000,
            "eval_count": 10,
            "done_reason": "stop",
        }


class DirectBatchIgnoresWorkRuntimeAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        runtime = WorkRuntime(WorkStore(workspace / ".cabinet" / "work"))
        super().__init__(
            workspace=workspace,
            allowed_roots=[workspace],
            work_runtime=runtime,
        )
        self.calls = 0
        self.saw_workruntime_prompt = False
        self.files = []
        for idx in range(6):
            path = workspace / f"batch_{idx}.txt"
            path.write_text(f"batch-content-{idx}\n", encoding="utf-8")
            self.files.append(path)

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if any(
            "WorkRuntime is mandatory" in (message.get("content") or "")
            for message in payload.get("messages", [])
        ):
            self.saw_workruntime_prompt = True
        path = self.files[min(self.calls - 1, len(self.files) - 1)]
        return {
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": str(path)},
                    }
                }]
            },
            "prompt_eval_count": 1000 + self.calls,
            "eval_count": 10,
            "done_reason": "stop",
        }


class DirectBatchTaskIgnoresEarlyStopAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        runtime = WorkRuntime(WorkStore(workspace / ".cabinet" / "work"))
        super().__init__(
            workspace=workspace,
            allowed_roots=[workspace],
            work_runtime=runtime,
        )
        self.calls = 0
        self.saw_workruntime_prompt = False
        sources = [f"source_{idx:02d}.txt" for idx in range(1, 11)]
        (workspace / "task.txt").write_text(
            "Read each source file with read_file, one file per tool call. Source files: "
            + ", ".join(sources),
            encoding="utf-8",
        )
        for name in sources:
            (workspace / name).write_text(f"{name}: data\n", encoding="utf-8")

    def _post_chat(self, payload, timeout):
        self.calls += 1
        if any(
            "WorkRuntime is mandatory" in (message.get("content") or "")
            for message in payload.get("messages", [])
        ):
            self.saw_workruntime_prompt = True
        path = "task.txt" if self.calls == 1 else f"source_{min(self.calls - 1, 10):02d}.txt"
        return {
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": str(self.workspace / path)},
                    }
                }]
            },
            "prompt_eval_count": 1000 + self.calls,
            "eval_count": 10,
            "done_reason": "stop",
        }


class DirectBatchTaskNoRuntimeAdapter(OllamaAdapter):
    def __init__(self, workspace: Path):
        super().__init__(workspace=workspace, allowed_roots=[workspace], work_runtime=None)
        self.calls = 0
        sources = [f"source_{idx:02d}.txt" for idx in range(1, 11)]
        (workspace / "task.txt").write_text(
            "Read all source files with read_file, one file per tool call. Source files: "
            + ", ".join(sources),
            encoding="utf-8",
        )
        for name in sources:
            (workspace / name).write_text(f"{name}: data\n", encoding="utf-8")

    def _post_chat(self, payload, timeout):
        self.calls += 1
        path = "task.txt" if self.calls == 1 else f"source_{min(self.calls - 1, 10):02d}.txt"
        return {
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": str(self.workspace / path)},
                    }
                }]
            },
            "prompt_eval_count": 1000 + self.calls,
            "eval_count": 10,
            "done_reason": "stop",
        }


class OllamaAdapterTests(unittest.TestCase):
    def test_sends_hands_options_to_ollama(self):
        adapter = CapturePayloadAdapter()

        result = adapter.call("system", [], "@gol ping", timeout=1)

        self.assertIsNone(result.error)
        self.assertEqual(
            adapter.payloads[0]["options"],
            {
                "num_ctx": 16384,
                "temperature": 0,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0,
                "presence_penalty": 0,
                "repeat_penalty": 1.1,
            },
        )

    def test_work_runtime_tools_are_exposed(self):
        adapter = OllamaAdapter(workspace=Path.cwd())

        schemas = adapter._tool_schemas(allow_write_tools=False)
        names = {item["function"]["name"] for item in schemas}

        self.assertIn("start_work", names)
        self.assertIn("run_work", names)
        self.assertIn("work_status", names)
        self.assertIn("work_logs", names)
        self.assertIn("cancel_work", names)

        by_name = {item["function"]["name"]: item["function"] for item in schemas}
        self.assertIn("short diagnostics", by_name["bash_run"]["description"])
        self.assertIn("important commands", by_name["start_work"]["description"])
        self.assertIn("argv array", by_name["start_work"]["description"])
        self.assertIn("Omit cwd", by_name["start_work"]["description"])
        self.assertIn("placeholder paths", by_name["start_work"]["description"])
        self.assertIn("Do not invent worker", by_name["start_work"]["description"])
        self.assertIn("action_id", by_name["start_work"]["parameters"]["properties"])
        self.assertNotIn("cwd", by_name["start_work"]["parameters"]["required"])
        self.assertNotIn("cwd", by_name["run_work"]["parameters"]["required"])
        self.assertNotIn("command", by_name["start_work"]["parameters"]["required"])

    def test_parses_json_tool_call_content_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = JsonToolThenFinalAdapter(workspace)

            result = adapter.call("system", [], "@gol read result.txt", timeout=5)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 2)
            self.assertIn("read_file", result.executed_tools)
            self.assertEqual(result.metrics["observed_report"]["tool_count"], 1)

    def test_parses_qwen_tool_call_and_normalizes_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = QwenToolCallThenFinalAdapter(workspace)

            result = adapter.call("system", [], "@gol read result.txt", timeout=5)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 2)
            self.assertIn("read_file", result.executed_tools)
            self.assertIsNotNone(adapter.second_payload)
            assistant_messages = [
                m for m in adapter.second_payload["messages"]
                if m.get("role") == "assistant" and m.get("tool_calls")
            ]
            self.assertEqual(len(assistant_messages), 1)
            self.assertEqual(assistant_messages[0]["content"], "")
            self.assertEqual(
                assistant_messages[0]["tool_calls"][0]["function"]["name"],
                "read_file",
            )
            self.assertNotIn("<tool_call>", json.dumps(adapter.second_payload["messages"], ensure_ascii=False))

    def test_stops_repeated_identical_json_tool_call_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = RepeatingJsonToolAdapter(workspace)

            result = adapter.call("system", [], "@gol read result.txt", timeout=30)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 4)
            self.assertEqual(result.executed_tools, ["read_file", "read_file", "read_file"])
            self.assertTrue(result.metrics.get("duplicate_tool_loop"))
            self.assertIn("CABINET OBSERVED REPORT", result.text)
            self.assertIn("observed_report", result.metrics)

    def test_records_context_pressure_per_ollama_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = ContextPressureAdapter(workspace)

            result = adapter.call("system", [], "@gol read result.txt", timeout=5)

            self.assertIsNone(result.error)
            self.assertEqual(len(result.metrics["ollama_steps"]), 2)
            self.assertTrue(result.metrics["context_pressure"])
            self.assertGreater(result.metrics["max_prompt_context_ratio"], 0.85)
            self.assertTrue(result.metrics["observed_report"]["context"]["context_pressure"])
            self.assertIn("context_pressure: true", result.text)

    def test_read_file_result_is_bounded_for_tool_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = LargeReadPayloadAdapter(workspace)

            result = adapter.call("system", [], "@gol read large.txt", timeout=5)

            self.assertIsNone(result.error)
            self.assertIsNotNone(adapter.second_payload)
            tool_messages = [m for m in adapter.second_payload["messages"] if m.get("role") == "tool"]
            self.assertEqual(len(tool_messages), 1)
            tool_payload = json.loads(tool_messages[0]["content"])
            self.assertTrue(tool_payload["truncated"])
            self.assertGreater(tool_payload["original_chars"], adapter.MAX_OUTPUT_CHARS)
            self.assertLess(tool_payload["returned_chars"], tool_payload["original_chars"])

    def test_compacts_old_tool_results_in_prompt_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = ManyReadsAdapter(workspace)

            result = adapter.call("system", [], "@gol read files", timeout=5)

            self.assertIsNone(result.error)
            self.assertIsNotNone(adapter.final_payload)
            tool_messages = [m for m in adapter.final_payload["messages"] if m.get("role") == "tool"]
            self.assertEqual(len(tool_messages), 5)
            payloads = [json.loads(m["content"]) for m in tool_messages]
            self.assertTrue(payloads[0]["compacted"])
            self.assertTrue(payloads[1]["compacted"])
            self.assertNotIn("content", payloads[0])
            self.assertIn("content_omitted_chars", payloads[0])
            self.assertIn("content", payloads[-1])

    def test_workruntime_guard_stops_direct_batch_file_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = DirectBatchIgnoresWorkRuntimeAdapter(workspace)

            result = adapter.call("system", [], "@gol process all batch files", timeout=10)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, adapter.MAX_DIRECT_BATCH_READS + 1)
            self.assertTrue(adapter.saw_workruntime_prompt)
            self.assertEqual(result.executed_tools, ["read_file"] * (adapter.MAX_DIRECT_BATCH_READS - 1))
            self.assertTrue(result.metrics["workruntime_required"])
            self.assertEqual(result.metrics["stopped_reason"], "workruntime_required")
            self.assertEqual(
                result.metrics["workruntime_guard"]["direct_reads"],
                adapter.MAX_DIRECT_BATCH_READS,
            )
            self.assertIn("WorkRuntime is required", result.text)
            self.assertIn("CABINET OBSERVED REPORT", result.text)
            self.assertEqual(
                result.metrics["observed_report"]["tool_count"],
                adapter.MAX_DIRECT_BATCH_READS - 1,
            )

    def test_workruntime_guard_stops_direct_batch_after_task_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = DirectBatchTaskIgnoresEarlyStopAdapter(workspace)

            result = adapter.call("system", [], "@gol execute task.txt", timeout=10)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 3)
            self.assertTrue(adapter.saw_workruntime_prompt)
            self.assertEqual(result.executed_tools, ["read_file"])
            self.assertTrue(result.metrics["workruntime_required"])
            self.assertEqual(result.metrics["stopped_reason"], "workruntime_required")
            self.assertTrue(result.metrics["workruntime_guard"]["batch_task_detected"])
            self.assertEqual(result.metrics["workruntime_guard"]["direct_reads"], 2)
            self.assertEqual(
                result.metrics["workruntime_guard"]["batch_task"]["source_file_count"],
                10,
            )
            self.assertEqual(result.metrics["observed_report"]["tool_count"], 1)

    def test_workruntime_guard_hard_stops_batch_when_runtime_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = DirectBatchTaskNoRuntimeAdapter(workspace)

            result = adapter.call("system", [], "@gol execute task.txt", timeout=10)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 2)
            self.assertEqual(result.executed_tools, ["read_file"])
            self.assertTrue(result.metrics["workruntime_required"])
            self.assertEqual(result.metrics["stopped_reason"], "workruntime_required")
            self.assertTrue(result.metrics["workruntime_guard"]["batch_task_detected"])
            self.assertEqual(result.metrics["observed_report"]["tool_count"], 1)
            self.assertNotIn("bash_run", result.executed_tools)
            self.assertFalse(any(t.startswith("git_commit") for t in result.executed_tools))

    def test_workruntime_guard_stops_direct_mutation_loop(self):
        # A freestyle mutation loop (repeated write_file) gets redirected to
        # WorkRuntime the same way a read/append loop does — that's "removing the freestyle".
        adapter = OllamaAdapter(workspace=Path.cwd(), work_runtime=object())
        pressure = adapter._workruntime_guard_pressure(
            ["write_file", "write_file"], ["write_file"],
        )
        self.assertIsNotNone(pressure)
        self.assertEqual(pressure["direct_writes"], adapter.MAX_DIRECT_BATCH_WRITES)
        self.assertEqual(
            pressure["max_direct_batch_writes"], adapter.MAX_DIRECT_BATCH_WRITES
        )

    def test_workruntime_guard_allows_single_edit_and_commit(self):
        # A single focused edit + commit stays direct: Golem is real hands
        # for targeted coding, only the flaky loop gets cut.
        adapter = OllamaAdapter(workspace=Path.cwd(), work_runtime=object())
        pressure = adapter._workruntime_guard_pressure(
            ["read_file", "write_file", "git_commit:abc123"], [],
        )
        self.assertIsNone(pressure)

    def test_protected_self_config_blocks_writes(self):
        # Golem must not modify its own model/prompt/contract files via file
        # tools (it edited its own Modelfile via bash on 2026-07-01).
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "models").mkdir()
            (ws / "prompts").mkdir()
            mf = ws / "models" / "golem.hands.Modelfile"
            mf.write_text("FROM x\n", encoding="utf-8")
            prompt = ws / "prompts" / "GOL_HANDS.md"
            prompt.write_text("role\n", encoding="utf-8")
            adapter = OllamaAdapter(workspace=ws, allowed_roots=[ws])

            r = adapter._tool_write_file({"path": str(mf), "content": "FROM y\n"})
            self.assertIn("protected self-config", r.get("error", ""))
            r2 = adapter._tool_delete_path({"path": str(prompt)})
            self.assertIn("protected self-config", r2.get("error", ""))
            # a normal workspace file still writes fine
            r3 = adapter._tool_write_file({"path": str(ws / "notes.txt"), "content": "hi"})
            self.assertEqual(r3.get("type"), "write_file")
            self.assertNotIn("error", r3)

    def test_start_work_runs_background_process_and_status_reads_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            store = WorkStore(workspace / "work", stale_after_s=60)
            runtime = WorkRuntime(store)
            adapter = OllamaAdapter(
                workspace=workspace,
                allowed_roots=[workspace],
                work_runtime=runtime,
            )
            artifact = workspace / "result.txt"

            started = adapter._tool_start_work({
                "title": "demo",
                "cwd": str(workspace),
                "command": [
                    sys.executable,
                    "-c",
                    (
                        "import json, pathlib; "
                        f"pathlib.Path(r'{artifact}').write_text('done'); "
                        "print(json.dumps({'type':'result','summary':'ok'}), flush=True)"
                    ),
                ],
            })

            self.assertIn("work_id", started)
            work_id = started["work_id"]
            deadline = time.time() + 5
            status = {}
            while time.time() < deadline:
                status = adapter._tool_work_status({"work_id": work_id})
                if status.get("work", {}).get("status") == "succeeded":
                    break
                time.sleep(0.05)

            logs = adapter._tool_work_logs({"work_id": work_id})
            self.assertEqual(status["work"]["status"], "succeeded")
            self.assertEqual(status["work"]["summary"], "ok")
            self.assertIn("summary", logs["stdout"])
            self.assertEqual(artifact.read_text(encoding="utf-8"), "done")

    def test_start_work_batch_redirect_blocks_missing_worker_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            store = WorkStore(workspace / "work", stale_after_s=60)
            adapter = OllamaAdapter(
                workspace=workspace,
                allowed_roots=[workspace],
                work_runtime=WorkRuntime(store),
            )

            result = adapter._tool_start_work({
                "title": "invented batch worker",
                "command": ["python", "-m", "batch_processor"],
                "batch_redirect": True,
            })

            self.assertTrue(result["blocked"])
            self.assertIn("module not found", result["error"])
            self.assertEqual(list((workspace / "work").glob("*")), [])

    def test_start_work_batch_redirect_allows_existing_worker_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            worker = workspace / "worker.py"
            worker.write_text(
                "from pathlib import Path\nPath('ok.txt').write_text('ok', encoding='utf-8')\n",
                encoding="utf-8",
            )
            store = WorkStore(workspace / "work", stale_after_s=60)
            adapter = OllamaAdapter(
                workspace=workspace,
                allowed_roots=[workspace],
                work_runtime=WorkRuntime(store),
            )

            result = adapter._tool_start_work({
                "title": "real batch worker",
                "command": ["python", "worker.py"],
                "batch_redirect": True,
            })

            self.assertIn("work_id", result)
            work_id = result["work_id"]
            deadline = time.time() + 5
            status = {}
            while time.time() < deadline:
                status = adapter._tool_work_status({"work_id": work_id})
                if status.get("work", {}).get("status") == "succeeded":
                    break
                time.sleep(0.05)
            self.assertEqual(status["work"]["status"], "succeeded")
            self.assertEqual((workspace / "ok.txt").read_text(encoding="utf-8"), "ok")

    def test_start_work_runs_manifest_action_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            actions_dir = workspace / "ops" / "actions"
            actions_dir.mkdir(parents=True)
            (workspace / "project.json").write_text(
                json.dumps({"actions_dir": "ops/actions"}),
                encoding="utf-8",
            )
            (actions_dir / "worker.json").write_text(
                json.dumps({
                    "id": "batch.worker",
                    "title": "Batch worker",
                    "command": [
                        "${python}",
                        "-c",
                        "from pathlib import Path; Path('manifest_ok.txt').write_text('ok', encoding='utf-8')",
                    ],
                    "cwd": "${project_root}",
                }),
                encoding="utf-8",
            )
            store = WorkStore(workspace / "work", stale_after_s=60)
            adapter = OllamaAdapter(
                workspace=workspace,
                allowed_roots=[workspace],
                work_runtime=WorkRuntime(store),
            )

            result = adapter._tool_start_work({"action_id": "batch.worker", "batch_redirect": True})

            self.assertEqual(result["action_id"], "batch.worker")
            work_id = result["work_id"]
            deadline = time.time() + 5
            status = {}
            while time.time() < deadline:
                status = adapter._tool_work_status({"work_id": work_id})
                if status.get("work", {}).get("status") == "succeeded":
                    break
                time.sleep(0.05)
            self.assertEqual(status["work"]["status"], "succeeded")
            self.assertEqual((workspace / "manifest_ok.txt").read_text(encoding="utf-8"), "ok")

    def test_start_work_runs_template_manifest_action_with_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            actions_dir = workspace / "ops" / "actions"
            actions_dir.mkdir(parents=True)
            (workspace / "project.json").write_text(
                json.dumps({"actions_dir": "ops/actions"}),
                encoding="utf-8",
            )
            (actions_dir / "worker.json").write_text(
                json.dumps({
                    "id": "batch.template",
                    "title": "Template worker",
                    "template": "noop",
                    "handoff": {
                        "handoff_id": "handoff-adapter-template",
                        "started_by": "dro",
                        "estimated_direct_cost": "small",
                    },
                }),
                encoding="utf-8",
            )
            store = WorkStore(workspace / "work", stale_after_s=60)
            adapter = OllamaAdapter(
                workspace=workspace,
                allowed_roots=[workspace],
                work_runtime=WorkRuntime(store),
            )

            result = adapter._tool_start_work({"action_id": "batch.template", "batch_redirect": True})

            self.assertEqual(result["action_id"], "batch.template")
            work_id = result["work_id"]
            deadline = time.time() + 5
            status = {}
            while time.time() < deadline:
                status = adapter._tool_work_status({"work_id": work_id})
                if status.get("work", {}).get("status") == "succeeded":
                    break
                time.sleep(0.05)
            self.assertEqual(status["work"]["status"], "succeeded")
            self.assertEqual(status["work"]["handoff_id"], "handoff-adapter-template")
            self.assertEqual(status["work"]["template_id"], "noop")

    def test_retries_once_on_missing_routing_json(self):
        adapter = RetryRouteAdapter()

        result = adapter.call("system", [], "@gol ping", timeout=5)

        self.assertIsNone(result.error)
        self.assertEqual(adapter.calls, 2)
        self.assertIn('{"route"', result.text)

    def test_retries_once_on_ollama_xml_syntax_error(self):
        adapter = RetryXmlAdapter()

        result = adapter.call("system", [], "@gol ping", timeout=1)

        self.assertIsNone(result.error)
        self.assertEqual(adapter.calls, 2)
        self.assertIn("ok", result.text)

    def test_timeout_after_tool_calls_returns_partial_result_to_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = ToolThenTimeoutAdapter(workspace)

            result = adapter.call(
                "system",
                [],
                "CABINET_TASK_MODE: work\nCABINET_HISTORY_LIMIT: 20\nCABINET_FROM: hux\n\n@gol run command",
                timeout=5,
            )

            self.assertIsNone(result.error)
            self.assertEqual(result.executed_tools, ["read_file"])
            self.assertTrue(result.metrics.get("partial_timeout"))
            self.assertIn("@hux", result.text)
            self.assertIn("tool result ok", result.text)
            self.assertIn("CABINET OBSERVED REPORT", result.text)
            self.assertIn("observed_report", result.metrics)
            self.assertEqual(result.metrics["observed_report"]["tool_count"], 1)
            route = json.loads(result.text.splitlines()[-1])
            self.assertEqual(route["route"], "hux")

    def test_final_report_replaces_golem_self_report_with_observed_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = ToolThenFinalClaimsAdapter(workspace)

            result = adapter.call(
                "system",
                [],
                "CABINET_TASK_MODE: work\nCABINET_FROM: hux\n\n@gol write result",
                timeout=5,
            )

            self.assertIsNone(result.error)
            self.assertEqual(result.executed_tools, ["write_file"])
            self.assertIn("CABINET OBSERVED REPORT", result.text)
            self.assertIn(str(workspace / "result.txt"), result.text)
            self.assertIn("files_changed:", result.text)
            self.assertIn("commit: none observed", result.text)
            self.assertNotIn("fake.csv", result.text)
            self.assertNotIn("deadbee", result.text)
            self.assertNotIn("fake tests passed", result.text)
            route = json.loads(result.text.splitlines()[-1])
            self.assertEqual(route["route"], "hux")

    def test_retries_once_on_empty_response_after_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = ToolThenEmptyThenFinalAdapter(workspace)

            result = adapter.call("system", [], "@gol write result", timeout=5, allow_write_tools=False)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 4)
            self.assertIn("write_file", result.executed_tools)
            self.assertIn("recovered", result.text)

    def test_empty_response_after_retries_returns_partial_observed_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = ToolThenAlwaysEmptyAdapter(workspace)

            result = adapter.call("system", [], "@gol write result", timeout=5, allow_write_tools=False)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 1 + adapter.MAX_EMPTY_RETRIES_AFTER_TOOLS + 1)
            self.assertTrue(result.metrics["partial_empty_response"])
            self.assertEqual(result.metrics["empty_response_retries"], adapter.MAX_EMPTY_RETRIES_AFTER_TOOLS)
            self.assertIn("CABINET OBSERVED REPORT", result.text)
            self.assertIn("empty response", result.text)
            self.assertIn(str(workspace / "result.txt"), result.text)
            self.assertIn("commit: none observed", result.text)

    def test_requires_git_commit_after_write_before_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "golem@example.test"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Golem Test"], cwd=workspace, check=True)
            adapter = WriteFinalThenCommitAdapter(workspace)

            result = adapter.call("system", [], "@gol write result", timeout=10)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 5)
            self.assertTrue(any(t.startswith("git_commit:success:") for t in result.executed_tools))
            self.assertIn("commit:", result.text)

    def test_blocks_commit_and_success_after_failed_workruntime_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "golem@example.test"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Golem Test"], cwd=workspace, check=True)
            adapter = FailedWorkThenCommitAdapter(workspace)

            result = adapter.call("system", [], "@gol run failed work and commit result", timeout=10)

            self.assertIsNone(result.error)
            self.assertEqual(adapter.calls, 6)
            self.assertTrue(adapter.saw_postcondition_prompt)
            self.assertIn("run_work", result.executed_tools)
            self.assertIn("work_status", result.executed_tools)
            self.assertIn("write_file", result.executed_tools)
            self.assertIn("git_commit:failed", result.executed_tools)
            self.assertFalse(any(t.startswith("git_commit:success") for t in result.executed_tools))
            self.assertTrue(result.metrics["workruntime_postcondition_failed"])
            self.assertEqual(
                result.metrics["workruntime_postcondition"]["work_statuses"],
                {"failed-work-1": "failed"},
            )
            self.assertIn("WorkRuntime postconditions were not satisfied", result.text)
            self.assertIn("failed-work-1 status=failed", result.text)
            self.assertIn("commit: none observed", result.text)
            git_head = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(git_head.returncode, 0)

    def test_delete_path_blocks_recursive_directory_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            doomed = workspace / "doomed"
            doomed.mkdir()
            (doomed / "note.txt").write_text("keep", encoding="utf-8")
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])

            result = adapter._tool_delete_path({"path": str(doomed), "recursive": True})

            self.assertIn("blocked", result["error"])
            self.assertTrue((doomed / "note.txt").exists())

    def test_tool_paths_resolve_relative_to_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "task.txt").write_text("workspace task", encoding="utf-8")
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])

            read_result = adapter._tool_read_file({"path": "task.txt"})
            write_result = adapter._tool_write_file({"path": "out/result.txt", "content": "done"})

            self.assertEqual(read_result["content"], "workspace task")
            self.assertEqual((workspace / "out" / "result.txt").read_text(encoding="utf-8"), "done")
            self.assertTrue(write_result["changed"])

    def test_task_mode_history_limit_trims_fast_context(self):
        adapter = OllamaAdapter(workspace=Path.cwd())
        history = [
            {"id": str(i), "role": "BOSS", "text": f"msg-{i}", "timestamp": "t", "type": "message"}
            for i in range(12)
        ]

        messages = adapter._build_messages(
            "system",
            history,
            "CABINET_TASK_MODE: chat\nCABINET_HISTORY_LIMIT: 8\n\n@gol ping",
        )
        body = "\n".join(m["content"] for m in messages)

        self.assertIn("msg-11", body)
        self.assertIn("msg-4", body)
        self.assertNotIn("msg-3", body)

    def test_hands_tool_cycle_writes_project_map_and_commits_exact_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "golem@example.test"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Golem Test"], cwd=workspace, check=True)

            target = workspace / "task.txt"
            project_map = workspace / "cabinet_project_map.md"
            untouched = workspace / "untouched.txt"
            project_map.write_text("# Map\n", encoding="utf-8")
            untouched.write_text("not staged", encoding="utf-8")
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])

            write_result = adapter._tool_write_file({"path": str(target), "content": "draft\n"})
            replace_result = adapter._tool_replace_text({"path": str(target), "old": "draft", "new": "done"})
            map_result = adapter._tool_replace_text({
                "path": str(project_map),
                "old": "# Map\n",
                "new": "# Map\n\n### pending - Golem tool cycle\n- Files: `task.txt`\n- Verification: unit test\n",
            })
            commit_result = adapter._tool_git_commit({
                "files": [str(target), str(project_map)],
                "message": "test: verify golem tool cycle",
            })

            self.assertEqual(write_result["type"], "write_file")
            self.assertEqual(replace_result["replaced"], 1)
            self.assertEqual(map_result["replaced"], 1)
            self.assertTrue(commit_result["success"], commit_result)
            self.assertEqual(target.read_text(encoding="utf-8"), "done\n")

            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(status, ["?? untouched.txt"])

    def test_git_commit_accepts_cwd_inside_allowed_root(self):
        with tempfile.TemporaryDirectory() as tmp_ws, tempfile.TemporaryDirectory() as tmp_alt:
            workspace = Path(tmp_ws)
            alt = Path(tmp_alt)
            subprocess.run(["git", "init"], cwd=alt, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "golem@example.test"], cwd=alt, check=True)
            subprocess.run(["git", "config", "user.name", "Golem Test"], cwd=alt, check=True)
            (alt / "README.md").write_text("alt repo\n", encoding="utf-8")

            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace, alt])
            result = adapter._tool_git_commit({
                "cwd": str(alt),
                "files": ["README.md"],
                "message": "init alt repo",
            })
            self.assertTrue(result.get("success"), result)
            log = subprocess.run(
                ["git", "log", "--oneline"], cwd=alt, check=True, capture_output=True, text=True,
            ).stdout
            self.assertIn("init alt repo", log)

    def test_git_commit_treats_leading_slash_file_as_cwd_relative(self):
        with tempfile.TemporaryDirectory() as tmp_ws, tempfile.TemporaryDirectory() as tmp_alt:
            workspace = Path(tmp_ws)
            alt = Path(tmp_alt)
            docs = alt / "docs"
            docs.mkdir()
            subprocess.run(["git", "init"], cwd=alt, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "golem@example.test"], cwd=alt, check=True)
            subprocess.run(["git", "config", "user.name", "Golem Test"], cwd=alt, check=True)
            (docs / "project_scope.md").write_text("next steps\n", encoding="utf-8")

            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace, alt])
            result = adapter._tool_git_commit({
                "cwd": str(alt),
                "files": ["/docs/project_scope.md"],
                "message": "docs(project): add next steps section",
            })
            self.assertTrue(result.get("success"), result)
            status = subprocess.run(
                ["git", "status", "--short"], cwd=alt, check=True, capture_output=True, text=True,
            ).stdout
            self.assertEqual("", status)

    def test_git_commit_rejects_cwd_outside_allowed_roots(self):
        with tempfile.TemporaryDirectory() as tmp_ws, tempfile.TemporaryDirectory() as tmp_alt:
            workspace = Path(tmp_ws)
            alt = Path(tmp_alt)
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])
            result = adapter._tool_git_commit({
                "cwd": str(alt),
                "files": ["README.md"],
                "message": "should fail",
            })
            self.assertIn("cwd outside allowed roots", result.get("error", ""))

    def test_git_commit_rejects_sibling_prefix_cwd(self):
        with tempfile.TemporaryDirectory() as parent:
            parent_path = Path(parent)
            allowed = parent_path / "root"
            sibling = parent_path / "root_evil"
            allowed.mkdir()
            sibling.mkdir()
            adapter = OllamaAdapter(workspace=allowed, allowed_roots=[allowed])
            result = adapter._tool_git_commit({
                "cwd": str(sibling),
                "files": ["README.md"],
                "message": "should fail",
            })
            self.assertIn("cwd outside allowed roots", result.get("error", ""))

    def test_bash_run_rejects_sibling_prefix_cwd(self):
        with tempfile.TemporaryDirectory() as parent:
            parent_path = Path(parent)
            allowed = parent_path / "root"
            sibling = parent_path / "root_evil"
            allowed.mkdir()
            sibling.mkdir()
            adapter = OllamaAdapter(workspace=allowed, allowed_roots=[allowed])

            result = adapter._tool_bash_run({"cwd": str(sibling), "command": "echo should-not-run"})

            self.assertIn("cwd outside allowed roots", result.get("error", ""))

    def test_bash_run_uses_powershell_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

            with patch("adapters.ollama.os.name", "nt"), patch("subprocess.run", Mock(return_value=completed)) as run:
                result = adapter._tool_bash_run({"command": "cat README.md"})

            self.assertEqual(result["returncode"], 0)
            called_args = run.call_args.args[0]
            self.assertEqual(called_args[:5], ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"])
            self.assertEqual(called_args[-1], "cat README.md")
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_bash_run_allows_long_worker_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")

            with patch("subprocess.run", Mock(return_value=completed)) as run:
                adapter._tool_bash_run({"command": "echo ok", "timeout": 9999})

            self.assertEqual(run.call_args.kwargs["timeout"], adapter.BASH_TIMEOUT_MAX)

    def test_known_files_patch_dispatches_to_harness_and_commits(self):
        class KnownFilesPatchAdapter(OllamaAdapter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.payloads = []

            def _post_chat(self, payload, timeout):
                self.payloads.append(payload)
                prompt = payload["messages"][-1]["content"]
                assert "Current pkg/calc.py:" in prompt
                assert "Current tests/test_calc.py:" in prompt
                content = (
                    "=== PATCH: pkg/calc.py ===\n"
                    "<<<<<<< SEARCH\n"
                    "def add(a, b):\n"
                    "    return a + b\n"
                    "=======\n"
                    "def add(a, b):\n"
                    "    return a + b\n"
                    "\n"
                    "\n"
                    "def subtract(a, b):\n"
                    "    return a - b\n"
                    ">>>>>>> REPLACE\n"
                    "=== PATCH: tests/test_calc.py ===\n"
                    "<<<<<<< SEARCH\n"
                    "from pkg.calc import add\n"
                    "\n"
                    "def test_add():\n"
                    "    assert add(2, 3) == 5\n"
                    "=======\n"
                    "from pkg.calc import add, subtract\n"
                    "\n"
                    "def test_add():\n"
                    "    assert add(2, 3) == 5\n"
                    "\n"
                    "\n"
                    "def test_subtract():\n"
                    "    assert subtract(5, 2) == 3\n"
                    ">>>>>>> REPLACE\n"
                )
                return {
                    "message": {"content": content},
                    "prompt_eval_count": 111,
                    "eval_count": 222,
                    "done_reason": "stop",
                }

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "pkg").mkdir()
            (workspace / "tests").mkdir()
            (workspace / "pkg" / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n",
                encoding="utf-8",
            )
            (workspace / "tests" / "test_calc.py").write_text(
                "from pkg.calc import add\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "golem@example.test"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Golem Test"], cwd=workspace, check=True)
            subprocess.run(["git", "add", "--", "."], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True, text=True)

            adapter = KnownFilesPatchAdapter(workspace=workspace, allowed_roots=[workspace])
            result = adapter.call(
                "system",
                [],
                (
                    '@gol patch mode=known-files files=[pkg/calc.py, tests/test_calc.py] '
                    f'verify="{sys.executable} -m pytest tests/test_calc.py" '
                    'scope="add subtract helper"'
                ),
                timeout=30,
            )

            self.assertIsNone(result.error)
            self.assertIn("known-files patch: auto_committed", result.text)
            self.assertIn("attempts: 1", result.text)
            self.assertTrue(any(tool.startswith("git_commit:success:") for tool in result.executed_tools))
            self.assertIn("subtract", (workspace / "pkg" / "calc.py").read_text(encoding="utf-8"))
            # v4: one prompt carries both files; retry sampling diversity enabled
            self.assertEqual(len(adapter.payloads), 1)
            self.assertEqual(adapter.payloads[0]["options"]["num_ctx"], 32768)
            self.assertEqual(adapter.payloads[0]["options"]["temperature"], 0.4)
            kp = result.metrics["known_files_patch"]
            self.assertEqual(kp["model_num_ctx"], 32768)
            self.assertEqual(kp["model_context_total_tokens"], 333)
            self.assertFalse(kp["model_context_shift_suspected"])
            self.assertEqual(kp["attempts_used"], 1)
            self.assertEqual(
                [a["strategy"] for a in kp["attempts"]], ["initial"]
            )
            self.assertEqual(kp["attempts"][0]["result"], "pass")
            commit_body = subprocess.run(
                ["git", "log", "-1", "--pretty=%B"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertTrue(commit_body.rstrip().endswith("Cabinet-Author: @gol"))


    def test_append_file_creates_and_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])
            target = workspace / "notes.txt"

            r1 = adapter._tool_append_file({"path": str(target), "content": "line1\n"})
            r2 = adapter._tool_append_file({"path": str(target), "content": "line2\n"})

            self.assertEqual(r1["type"], "append_file")
            self.assertEqual(r2["type"], "append_file")
            self.assertEqual(target.read_text(encoding="utf-8"), "line1\nline2\n")

    def test_append_file_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])
            target = workspace / "sub" / "deep" / "log.txt"

            result = adapter._tool_append_file({"path": str(target), "content": "hello\n"})

            self.assertEqual(result["type"], "append_file")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_write_file_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])
            target = workspace / "new_dir" / "file.txt"

            result = adapter._tool_write_file({"path": str(target), "content": "data"})

            self.assertEqual(result["type"], "write_file")
            self.assertNotIn("error", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "data")

    def test_search_text_default_glob_includes_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])
            (workspace / "README.md").write_text("find_me in markdown", encoding="utf-8")
            (workspace / "code.py").write_text("find_me in python", encoding="utf-8")

            result = adapter._tool_search_text({"query": "find_me", "path": str(workspace)})

            paths = [r["path"] for r in result["results"]]
            self.assertTrue(any("README.md" in p for p in paths))
            self.assertTrue(any("code.py" in p for p in paths))


def _ollama_available() -> bool:
    try:
        import urllib.request as _ur
        _ur.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


def _run_ollama_live_tests() -> bool:
    return os.environ.get("CABINET_RUN_OLLAMA_LIVE") == "1" and _ollama_available()


@unittest.skipUnless(
    _run_ollama_live_tests(),
    "set CABINET_RUN_OLLAMA_LIVE=1 with Ollama running to execute live model smoke-tests",
)
class OllamaLiveToolLoopTests(unittest.TestCase):
    """Integration smoke-test: requires real Ollama with golem:hands model."""

    def test_live_write_and_commit(self):
        """Golem must read a task file, write a result file, and commit it via real Ollama."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "golem@live.test"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Golem Live"], cwd=workspace, check=True)
            (workspace / "task.txt").write_text("Write 'hello_live' into result.txt", encoding="utf-8")

            adapter = OllamaAdapter(workspace=workspace, allowed_roots=[workspace])
            result = adapter.call(
                system_prompt=(
                    "You are Golem, a hands agent. Read task.txt, write the required content "
                    "into result.txt, then commit result.txt with git_commit."
                ),
                history=[],
                user_message="@gol execute the task in task.txt",
                timeout=240,
                allow_write_tools=True,
            )

            # Smoke: adapter completed without a hard error and executed at least one tool call.
            self.assertIsNone(result.error, msg=f"Ollama returned error: {result.error}")
            self.assertTrue(result.executed_tools, msg=f"no tools were called; response: {result.text[:300]}")


if __name__ == "__main__":
    unittest.main()
