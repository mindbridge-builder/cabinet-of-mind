import asyncio
import base64
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import server


class FakeAdapter:
    def __init__(self, health=(True, "ok"), runtime=(True, "ok")):
        self.health = health
        self.runtime = runtime

    def healthcheck(self):
        return self.health

    def runtime_health(self):
        return self.runtime


class FakeDispatcher:
    def __init__(self, adapters):
        self.adapters = adapters


class MentionDispatcher:
    def __init__(self):
        self.calls = []

    def handle_user_mentions(self, text, thread_id):
        self.calls.append((text, thread_id))


class FakeWorkRuntime:
    def __init__(self):
        self.calls = []

    def start_process(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "work_id": f"work-{len(self.calls)}",
            "status": "running",
            "title": kwargs["title"],
        }

    def cancel(self, work_id):
        return False


class ServerStateTestCase(unittest.TestCase):
    """Base: isolates server.py's global state in a temp folder."""

    def setUp(self):
        self.original = {
            "log_file": server.LOG_FILE,
            "pending_file": server.PENDING_FILE,
            "history": server._history,
            "pending_queue": server.pending_queue,
            "next_id": server._next_id,
            "saved_count": server._saved_count,
            "dispatcher": server._dispatcher,
            "attachments_dir": server.ATTACHMENTS_DIR,
            "claude_limits_file": server.CLAUDE_LIMITS_FILE,
            "claude_limits": server._claude_limits,
            "reality_file": server.REALITY_FILE,
            "local_reality_file": server.LOCAL_REALITY_FILE,
            "allowed_roots": server.ALLOWED_ROOTS,
            "work_store": server._work_store,
            "work_runtime": server._work_runtime,
        }
        self.tmp_path = Path("logs") / "test_tmp_server_pending"
        shutil.rmtree(self.tmp_path, ignore_errors=True)
        self.tmp_path.mkdir(parents=True)
        server.LOG_FILE = self.tmp_path / "CABINET_LOG.jsonl"
        server.PENDING_FILE = self.tmp_path / "pending_queue.json"
        server.ATTACHMENTS_DIR = self.tmp_path / "attachments"
        server._history = []
        server.pending_queue = []
        server._next_id = 1
        server._saved_count = 0
        server._dispatcher = None
        server.CLAUDE_LIMITS_FILE = self.tmp_path / "claude_limits.json"
        server._claude_limits = None
        server.REALITY_FILE = self.tmp_path / "CABINET_REALITY.json"
        server.LOCAL_REALITY_FILE = self.tmp_path / "CABINET_REALITY.local.json"
        server.REALITY_FILE.write_text(json.dumps({
            "active_project": str(self.tmp_path),
            "allowed_roots": [str(server.WORK_DIR), str(self.tmp_path)],
        }), encoding="utf-8")
        server.ALLOWED_ROOTS = [server.WORK_DIR, self.tmp_path]
        server._work_store = None
        server._work_runtime = None

    def tearDown(self):
        server.LOG_FILE = self.original["log_file"]
        server.PENDING_FILE = self.original["pending_file"]
        server.ATTACHMENTS_DIR = self.original["attachments_dir"]
        server._history = self.original["history"]
        server.pending_queue = self.original["pending_queue"]
        server._next_id = self.original["next_id"]
        server._saved_count = self.original["saved_count"]
        server._dispatcher = self.original["dispatcher"]
        server.CLAUDE_LIMITS_FILE = self.original["claude_limits_file"]
        server._claude_limits = self.original["claude_limits"]
        server.REALITY_FILE = self.original["reality_file"]
        server.LOCAL_REALITY_FILE = self.original["local_reality_file"]
        server.ALLOWED_ROOTS = self.original["allowed_roots"]
        server._work_store = self.original["work_store"]
        server._work_runtime = self.original["work_runtime"]
        shutil.rmtree(self.tmp_path, ignore_errors=True)


class ServerPendingTests(ServerStateTestCase):
    def test_hands_prompt_includes_bootstrap_index(self):
        prompt = server._load_golem_prompt()

        self.assertIn("Cabinet Bootstrap Index", prompt)
        self.assertIn("PLAN.md", prompt)
        self.assertIn("cabinet_project_map.md", prompt)
        self.assertIn("CABINET_REALITY.local.json", prompt)

    def test_pending_enqueue_same_agent_same_thread_deduplicates(self):
        # One agent twice in the same thread → the second displaces the first.
        server.pending_enqueue({
            "from_role": "hux",
            "target_role": "hux",
            "thread_id": "thread-1",
            "kind": "boss_addressed",
            "label": "old",
        })
        server.pending_enqueue({
            "from_role": "hux",
            "target_role": "hux",
            "thread_id": "thread-1",
            "kind": "boss_addressed",
            "label": "new",
        })
        self.assertEqual([item["label"] for item in server.pending_queue], ["new"])

    def test_pending_enqueue_different_agents_same_thread_coexist(self):
        # Different agents in the same thread → both pendings are kept.
        server.pending_enqueue({
            "from_role": "hux",
            "target_role": "hux",
            "thread_id": "thread-1",
            "kind": "boss_addressed",
            "label": "hux-pending",
        })
        server.pending_enqueue({
            "from_role": "dro",
            "target_role": "dro",
            "thread_id": "thread-1",
            "kind": "boss_addressed",
            "label": "dro-pending",
        })
        labels = [item["label"] for item in server.pending_queue]
        self.assertIn("hux-pending", labels)
        self.assertIn("dro-pending", labels)
        self.assertEqual(len(server.pending_queue), 2)

    def test_text_n_rejects_only_first_pending_leaves_other_agent(self):
        # N rejects the first pending (hux), Diderot's pending in the same thread remains.
        server.pending_enqueue({
            "from_role": "hux",
            "target_role": "hux",
            "thread_id": "thread-1",
            "kind": "boss_addressed",
            "label": "hux-pending",
        })
        server.pending_queue.append({
            "from_role": "dro",
            "target_role": "dro",
            "thread_id": "thread-1",
            "kind": "boss_addressed",
            "label": "dro-pending",
            "id": "manual-dro",
        })

        asyncio.run(server.handle(None, json.dumps({"action": "message", "text": "N"})))

        # Huxley rejected, Diderot remains.
        self.assertEqual(len(server.pending_queue), 1)
        self.assertEqual(server.pending_queue[0]["from_role"], "dro")
        self.assertTrue(any("[rejected]" in item["text"] for item in server._history))

    def test_raw_clear_without_confirm_does_not_clear_history(self):
        server.add_msg("BOSS", "keep me")

        asyncio.run(server.handle(None, json.dumps({"action": "clear"})))

        self.assertEqual(len(server._history), 1)
        self.assertEqual(server._history[0]["text"], "keep me")

    def test_user_message_role_is_valid_utf8_nick(self):
        asyncio.run(server.handle(None, json.dumps({
            "action": "message",
            "text": "hello",
        })))

        self.assertEqual(server._history[0]["role"], "BOSS")

    def test_clear_confirm_creates_pending_and_approve_clears(self):
        server.add_msg("BOSS", "erase me")

        asyncio.run(server.handle(None, json.dumps({
            "action": "clear",
            "confirm_text": "CLEAR",
        })))

        self.assertEqual(len(server.pending_queue), 1)
        self.assertEqual(server.pending_queue[0]["kind"], "clear_history")
        self.assertGreater(len(server._history), 1)

        asyncio.run(server.handle(None, json.dumps({
            "action": "approve",
            "pending_id": server.pending_queue[0]["id"],
        })))

        self.assertEqual(server._history, [])
        self.assertEqual(server.pending_queue, [])

    def test_ui_contains_ws_token_query_bridge(self):
        ui = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("wstoken", ui)
        self.assertIn("?token=", ui)

    def test_message_attachments_are_saved_separately(self):
        dispatcher = MentionDispatcher()
        server._dispatcher = dispatcher
        payload = base64.b64encode(b"hello from attachment").decode("ascii")

        asyncio.run(server.handle(None, json.dumps({
            "action": "message",
            "text": "@dro see attached",
            "attachments": [{
                "name": "note.txt",
                "type": "text/plain",
                "data": f"data:text/plain;base64,{payload}",
            }],
        })))

        self.assertEqual(len(server._history), 1)
        msg = server._history[0]
        self.assertEqual(msg["text"], "@dro see attached")
        self.assertEqual(msg["attachments"][0]["name"], "note.txt")
        saved_path = Path(msg["attachments"][0]["path"])
        self.assertEqual(saved_path.read_bytes(), b"hello from attachment")
        self.assertIn(str(saved_path), dispatcher.calls[0][0])

    def test_ui_pending_shows_short_summary_and_reason(self):
        ui = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("pendingState.summary || label", ui)
        self.assertIn("pendingState.reason", ui)
        self.assertIn("pending-reason", ui)

    def test_health_uses_runtime_status_for_hux_and_dro_only(self):
        server._dispatcher = FakeDispatcher({
            "hux": FakeAdapter(runtime=(False, "limit: resets 21:30")),
            "dro": FakeAdapter(runtime=(False, "auth: login required")),
            "gol": FakeAdapter(runtime=(False, "local runtime ignored")),
        })

        health = server.health_check()

        self.assertTrue(health["adapter_hux"].startswith("FAIL: limit"))
        self.assertTrue(health["adapter_dro"].startswith("FAIL: auth"))
        self.assertEqual(health["adapter_gol"], "ok")

    def test_reads_latest_codex_limits_from_session_logs(self):
        sessions = self.tmp_path / "codex_sessions"
        older = sessions / "2026" / "05" / "12"
        newer = sessions / "2026" / "05" / "13"
        older.mkdir(parents=True)
        newer.mkdir(parents=True)
        old_file = older / "old.jsonl"
        new_file = newer / "new.jsonl"
        old_file.write_text(json.dumps({
            "timestamp": "old",
            "payload": {"rate_limits": {"primary": {"used_percent": 12}}},
        }), encoding="utf-8")
        new_file.write_text("\n".join([
            json.dumps({"timestamp": "noise", "payload": {}}),
            json.dumps({
                "timestamp": "new",
                "payload": {
                    "rate_limits": {
                        "primary": {"used_percent": 47, "resets_at": 1778672719},
                        "secondary": {"used_percent": 7, "resets_at": 1779266056},
                        "plan_type": "plus",
                    }
                },
            }),
        ]), encoding="utf-8")
        now = time.time()
        os.utime(old_file, (now - 10, now - 10))
        os.utime(new_file, (now, now))

        limits = server.read_latest_codex_limits(sessions)

        self.assertEqual(limits["primary_used_percent"], 47)
        self.assertEqual(limits["secondary_used_percent"], 7)
        self.assertEqual(limits["plan_type"], "plus")

    def test_ui_shows_joe_codex_limit_percent(self):
        ui = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="limit-dro"', ui)
        self.assertIn("setCodexLimit", ui)
        self.assertIn("primary_used_percent", ui)

    def test_ui_shows_cha_claude_limit(self):
        ui = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="limit-hux"', ui)
        self.assertIn("setClaudeLimit", ui)
        self.assertIn("claude_limits", ui)

    def test_reads_latest_claude_limits_from_file(self):
        limits_file = self.tmp_path / "claude_limits.json"
        limits_file.write_text(json.dumps({
            "status": "rejected",
            "resetsAt": 1778676600,
            "rateLimitType": "five_hour",
            "overageStatus": "allowed",
            "overageResetsAt": 1780272000,
            "isUsingOverage": True,
            "captured_at": "2026-05-13T14:30:00",
        }), encoding="utf-8")

        limits = server.read_latest_claude_limits(limits_file)

        self.assertEqual(limits["status"], "rejected")
        self.assertEqual(limits["rate_limit_type"], "five_hour")
        self.assertTrue(limits["is_using_overage"])
        self.assertEqual(limits["resets_at"], 1778676600)
        self.assertIsNotNone(limits["reset_label"])
        self.assertIsNotNone(limits["overage_reset_label"])

    def test_read_latest_claude_limits_missing_file_returns_none(self):
        self.assertIsNone(server.read_latest_claude_limits(self.tmp_path / "nope.json"))

    def test_health_refreshes_claude_limits_from_file_each_call(self):
        server.CLAUDE_LIMITS_FILE.write_text(json.dumps({
            "status": "allowed",
            "resetsAt": 1778676600,
            "rateLimitType": "five_hour",
            "isUsingOverage": False,
        }), encoding="utf-8")
        first = server.health_check()

        server.CLAUDE_LIMITS_FILE.write_text(json.dumps({
            "status": "allowed",
            "resetsAt": 1778694600,
            "rateLimitType": "five_hour",
            "isUsingOverage": False,
        }), encoding="utf-8")
        second = server.health_check()

        self.assertNotEqual(
            first["claude_limits"]["reset_label"],
            second["claude_limits"]["reset_label"],
        )
        self.assertEqual(second["claude_limits"]["resets_at"], 1778694600)

    def test_project_actions_require_explicit_manifest_actions_dir(self):
        actions_dir = self.tmp_path / ".cabinet" / "actions"
        actions_dir.mkdir(parents=True)
        (actions_dir / "demo.json").write_text(json.dumps({
            "id": "demo.expand",
            "title": "Demo expansion",
            "command": ["${python}", "-c", "print('ok')"],
        }), encoding="utf-8")

        formatted = server._format_project_actions()

        self.assertIn("no project actions", formatted)
        self.assertIn("work templates:", formatted)

    def test_project_manifest_can_override_actions_dir(self):
        (self.tmp_path / "project.json").write_text(json.dumps({
            "id": "demo_project",
            "name": "Demo Project",
            "actions_dir": "ops/actions",
        }), encoding="utf-8")
        actions_dir = self.tmp_path / "ops" / "actions"
        actions_dir.mkdir(parents=True)
        (actions_dir / "custom.json").write_text(json.dumps({
            "id": "custom.expand",
            "title": "Custom expansion",
            "command": ["${python}", "-c", "print('ok')"],
        }), encoding="utf-8")

        formatted = server._format_project_actions()
        health = server.health_check()

        self.assertIn("custom.expand: Custom expansion", formatted)
        self.assertEqual(health["project"]["id"], "demo_project")
        self.assertEqual(health["project"]["name"], "Demo Project")

    def test_action_command_starts_project_manifest_items(self):
        (self.tmp_path / "project.json").write_text(json.dumps({
            "id": "demo_project",
            "actions_dir": "ops/actions",
        }), encoding="utf-8")
        actions_dir = self.tmp_path / "ops" / "actions"
        actions_dir.mkdir(parents=True)
        (actions_dir / "demo.json").write_text(json.dumps({
            "id": "demo.expand",
            "title": "Demo expansion",
            "owner": "hux",
            "items": [
                {
                    "title": "Demo first",
                    "cwd": "${project_root}",
                    "command": ["${python}", "-c", "print('first')"],
                },
                {
                    "title": "Demo second",
                    "cwd": ".",
                    "command": ["${python}", "-c", "print('second')"],
                },
            ],
        }), encoding="utf-8")
        runtime = FakeWorkRuntime()
        server._work_runtime = runtime

        asyncio.run(server.handle(None, json.dumps({
            "action": "message",
            "text": "action demo.expand",
        })))

        self.assertEqual(len(runtime.calls), 2)
        self.assertEqual(runtime.calls[0]["title"], "Demo first")
        self.assertEqual(runtime.calls[0]["owner"], "hux")
        self.assertEqual(Path(runtime.calls[1]["cwd"]).resolve(), self.tmp_path.resolve())
        self.assertTrue(any("Started project action 'demo.expand'" in item["text"] for item in server._history))

    def test_action_command_starts_template_manifest_item_with_handoff(self):
        (self.tmp_path / "project.json").write_text(json.dumps({
            "id": "demo_project",
            "actions_dir": "ops/actions",
        }), encoding="utf-8")
        actions_dir = self.tmp_path / "ops" / "actions"
        actions_dir.mkdir(parents=True)
        (actions_dir / "demo.json").write_text(json.dumps({
            "id": "demo.pytest",
            "title": "Demo pytest",
            "template": "pytest",
            "params": {"args": ["tests/test_work_templates.py"]},
            "handoff": {
                "handoff_id": "handoff-server-template",
                "started_by": "dro",
                "estimated_direct_cost": "small",
            },
        }), encoding="utf-8")
        runtime = FakeWorkRuntime()
        server._work_runtime = runtime

        asyncio.run(server.handle(None, json.dumps({
            "action": "message",
            "text": "action demo.pytest",
        })))

        self.assertEqual(len(runtime.calls), 1)
        call = runtime.calls[0]
        self.assertEqual(call["command"][:3], [sys.executable, "-m", "pytest"])
        self.assertEqual(call["handoff"]["handoff_id"], "handoff-server-template")
        self.assertEqual(call["handoff"]["template_id"], "pytest")
        self.assertEqual(call["handoff"]["action_id"], "demo.pytest")


class ModelSwitchTests(ServerStateTestCase):
    def test_model_allowed_per_role(self):
        gol = FakeAdapter()
        gol.model = "golem:hands"
        server._dispatcher = FakeDispatcher({"gol": gol})

        # Any local coder model: installations extend ROLE_MODELS["gol"].
        with mock.patch.dict(
            server.ROLE_MODELS, {"gol": ["golem:hands", "any-coder:latest"]}
        ):
            asyncio.run(server.handle(None, json.dumps({"text": "!model gol any-coder:latest"})))

        self.assertEqual(gol.model, "any-coder:latest")
        self.assertTrue(any("Golem → model" in m["text"] for m in server._history))

    def test_model_from_other_role_rejected(self):
        hux = FakeAdapter()
        hux.model = "claude-opus-4-7"
        server._dispatcher = FakeDispatcher({"hux": hux})

        asyncio.run(server.handle(None, json.dumps({"text": "!model hux gpt-5.5"})))

        self.assertEqual(hux.model, "claude-opus-4-7")
        self.assertTrue(any("not available for @hux" in m["text"] for m in server._history))

    def test_fable_alias_resolves_for_cha(self):
        hux = FakeAdapter()
        hux.model = "claude-opus-4-7"
        server._dispatcher = FakeDispatcher({"hux": hux})

        asyncio.run(server.handle(None, json.dumps({"text": "!model hux fable"})))

        self.assertEqual(hux.model, "claude-fable-5")


class ClaudeLimitsTests(unittest.TestCase):
    def test_utilization_passed_through_for_ui_bar(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "claude_limits.json"
            p.write_text(json.dumps({
                "status": "allowed_warning",
                "resetsAt": 1781029200,
                "rateLimitType": "five_hour",
                "utilization": 0.48,
                "surpassedThreshold": 0.4,
                "isUsingOverage": False,
                "captured_at": "2026-06-11T13:00:00",
            }), encoding="utf-8")

            limits = server.read_latest_claude_limits(p)

            self.assertEqual(limits["utilization"], 0.48)
            self.assertEqual(limits["surpassed_threshold"], 0.4)
            self.assertEqual(limits["status"], "allowed_warning")
            self.assertTrue(limits["reset_label"])


class WorkUiFilterTests(unittest.TestCase):
    def test_terminal_items_fade_after_ttl_active_stay(self):
        now = 100000.0
        fresh_done = {"status": "succeeded", "heartbeat_epoch": now - 60}
        old_done = {"status": "succeeded", "heartbeat_epoch": now - server.WORK_UI_TERMINAL_TTL - 1}
        old_cancelled = {"status": "cancelled", "heartbeat_epoch": now - 86400}
        running = {"status": "running", "heartbeat_epoch": now - 86400}
        stale = {"status": "stale", "heartbeat_epoch": now - 86400}

        visible = server._filter_ui_work([fresh_done, old_done, old_cancelled, running, stale], now)

        self.assertIn(fresh_done, visible)
        self.assertIn(running, visible)  # active ones always live in the UI
        self.assertIn(stale, visible)    # stale might still be alive — show it
        self.assertNotIn(old_done, visible)
        self.assertNotIn(old_cancelled, visible)


class ClaudeOauthUsageTests(unittest.TestCase):
    def test_parses_five_hour_and_week_windows(self):
        usage = server._parse_claude_usage_payload({
            "five_hour": {"utilization": 79.0, "resets_at": "2026-06-11T13:39:59+00:00"},
            "seven_day": {"utilization": 14.0, "resets_at": "2026-06-13T13:59:59+00:00"},
        })
        self.assertEqual(usage["five_hour_pct"], 79.0)
        self.assertEqual(usage["seven_day_pct"], 14.0)
        self.assertTrue(usage["five_hour_reset_label"])
        self.assertTrue(usage["seven_day_reset_label"])

    def test_empty_payload_returns_none(self):
        self.assertIsNone(server._parse_claude_usage_payload({}))
        self.assertIsNone(server._parse_claude_usage_payload({"five_hour": None, "seven_day": {}}))


class WsTokenTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.pop("CABINET_WS_TOKEN", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["CABINET_WS_TOKEN"] = self._saved_env
        else:
            os.environ.pop("CABINET_WS_TOKEN", None)

    def test_token_generated_and_persisted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "ws_token.txt"
            first = server._ensure_ws_token(token_file)
            self.assertTrue(first)
            self.assertEqual(token_file.read_text(encoding="utf-8").strip(), first)
            # A repeat start reads the same token instead of generating a new one.
            second = server._ensure_ws_token(token_file)
            self.assertEqual(first, second)

    def test_env_override_wins_and_empty_disables(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "ws_token.txt"
            os.environ["CABINET_WS_TOKEN"] = "my-token"
            self.assertEqual(server._ensure_ws_token(token_file), "my-token")
            os.environ["CABINET_WS_TOKEN"] = ""
            self.assertEqual(server._ensure_ws_token(token_file), "")
            self.assertFalse(token_file.exists())


if __name__ == "__main__":
    unittest.main()
