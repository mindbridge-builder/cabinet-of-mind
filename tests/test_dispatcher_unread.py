from pathlib import Path
import time
import unittest

from adapters.base import Adapter, CallResult
from core import routing
from core.dispatcher import Dispatcher


class RecordingAdapter(Adapter):
    def __init__(self):
        super().__init__("dro", "Diderot", Path.cwd())
        self.calls = []

    def call(
        self,
        system_prompt,
        history,
        user_message,
        timeout=300,
        allow_write_tools=False,
        thread_id=None,
        on_progress=None,
    ):
        self.calls.append({
            "history": history,
            "user_message": user_message,
            "allow_write_tools": allow_write_tools,
            "thread_id": thread_id,
        })
        return CallResult(
            text='ok\n{"route":"","message":"done"}',
            executed_tools=[],
            successful_fetch_count=0,
            run_id="run-1",
            elapsed=0,
            error=None,
            metrics={},
        )

    def healthcheck(self):
        return True, "ok"


class DispatcherUnreadTests(unittest.TestCase):
    def test_partial_timeout_is_shown_without_dispatching_mentions(self):
        class PartialJoeAdapter(Adapter):
            def __init__(self):
                super().__init__("dro", "Diderot", Path.cwd())

            def call(self, system_prompt, history, user_message, timeout=300,
                     allow_write_tools=False, thread_id=None, on_progress=None):
                return CallResult(
                    text='Partial analysis.\n@gol do this next.\n{"route":"gol","message":"do this"}',
                    executed_tools=[],
                    successful_fetch_count=0,
                    run_id="run-partial",
                    elapsed=0,
                    error="idle timeout 300s without Codex output",
                    metrics={"partial_timeout": True},
                )

            def healthcheck(self):
                return True, "ok"

        gol = RecordingAdapter()
        added = []
        dispatcher = Dispatcher(
            adapters={"dro": PartialJoeAdapter(), "gol": gol},
            system_prompts={"dro": "system", "gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": added.append({
                "id": str(len(added) + 1),
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            }) or added[-1],
        )

        dispatcher.call_agent("dro", "@dro think", "thread-1", 0)

        self.assertEqual(gol.calls, [])
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["type"], "message")
        self.assertIn("PARTIAL RESPONSE @dro", added[0]["text"])
        self.assertIn("Partial analysis.", added[0]["text"])

    def test_direct_question_uses_fast_chat_mode_without_write_tools(self):
        adapter = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"dro": adapter},
            system_prompts={"dro": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        dispatcher.handle_user_mentions("@dro what does workspace access mean?", "thread-1")
        deadline = time.time() + 1
        while not adapter.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(adapter.calls[0]["allow_write_tools"])
        self.assertIn("CABINET_TASK_MODE: work", adapter.calls[0]["user_message"])

    def test_sage_turn_budget_names_dispute_artifact_requirement(self):
        adapter = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"dro": adapter},
            system_prompts={"dro": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        msg = dispatcher._mode_prefixed_message(
            "@dro review the argument",
            task_mode="plan",
            role="dro",
        )

        self.assertIn("what changed because of the argument", msg)
        self.assertIn("high risk of this being ritual", msg)

    def test_continue_approved_resumes_with_workspace_tools(self):
        adapter = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"dro": adapter},
            system_prompts={"dro": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        dispatcher.continue_approved({
            "target_role": "dro",
            "label": "Review the UI proposal.",
            "message": "Continue after Boss approval.",
            "thread_id": "thread-1",
            "allow_write_tools": False,
        })

        deadline = time.time() + 1
        while not adapter.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("Boss approved this pending request", adapter.calls[0]["user_message"])
        self.assertIn("workspace-capable", adapter.calls[0]["user_message"])
        self.assertTrue(adapter.calls[0]["allow_write_tools"])

    def test_plan_mode_disables_write_tools_and_marks_message(self):
        adapter = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"dro": adapter},
            system_prompts={"dro": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        dispatcher.call_agent("dro", "@dro write a plan", "thread-1", 0)

        self.assertEqual(len(adapter.calls), 1)
        self.assertFalse(adapter.calls[0]["allow_write_tools"])
        self.assertIn("CABINET_TASK_MODE: plan", adapter.calls[0]["user_message"])

    def test_route_to_boss_without_write_intent_is_plain_answer_not_pending(self):
        approvals = []
        added = []
        dispatcher = Dispatcher(
            adapters={"dro": RecordingAdapter()},
            system_prompts={},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": added.append({
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            }) or added[-1],
            request_approval=approvals.append,
        )

        dispatcher.dispatch_next(
            "dro",
            'Need Boss opinion.\n{"route":"boss","write_intent":false,"arch_decision":false,"message":"Need Boss opinion."}',
            "thread-1",
            0,
        )

        self.assertEqual(approvals, [])
        self.assertEqual(added, [])

    def test_json_only_route_to_boss_without_approval_is_rendered(self):
        approvals = []
        added = []
        dispatcher = Dispatcher(
            adapters={"dro": RecordingAdapter()},
            system_prompts={},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": added.append({
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            }) or added[-1],
            request_approval=approvals.append,
        )

        dispatcher.dispatch_next(
            "dro",
            '{"route":"boss","write_intent":false,"arch_decision":false,"message":"Plain final."}',
            "thread-1",
            0,
        )

        self.assertEqual(approvals, [])
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["text"], "Plain final.")

    def test_route_to_boss_with_write_intent_returns_to_sender_with_write_tools(self):
        approvals = []
        dispatcher = Dispatcher(
            adapters={"dro": RecordingAdapter()},
            system_prompts={},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
            request_approval=approvals.append,
        )

        dispatcher.dispatch_next(
            "dro",
            'Need write approval.\n{"route":"boss","write_intent":true,"arch_decision":false,"message":"Edit server.py."}',
            "thread-1",
            0,
        )

        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["target_role"], "dro")
        self.assertTrue(approvals[0]["allow_write_tools"])
        self.assertEqual(approvals[0]["message"], "Edit server.py.")
        self.assertIn("DIDEROT", approvals[0]["summary"])
        self.assertEqual(approvals[0]["reason"], "edit/write")

    def test_high_risk_arch_route_goes_to_boss_approval_without_external_model(self):
        approvals = []
        other = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"dro": RecordingAdapter(), "hux": other},
            system_prompts={},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
            request_approval=approvals.append,
        )

        dispatcher.dispatch_next(
            "dro",
            'Final after internal third pass.\n{"route":"boss","write_intent":false,'
            '"arch_decision":true,"risk_level":"high","message":"Adopt architecture."}',
            "thread-1",
            0,
        )

        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["kind"], "boss_addressed")
        self.assertEqual(approvals[0]["target_role"], "dro")
        self.assertEqual(other.calls, [])

    def test_agent_fallback_mentions_dispatch_kes(self):
        # An @gol mention in an agent's text now dispatches like a regular agent.
        # The block was removed — JSON-route and a text @gol work the same way.
        hux = RecordingAdapter()
        gol = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"hux": hux, "gol": gol},
            system_prompts={"hux": "system", "gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        # Text with @gol and @hux inside a line — parse_route will return None,
        # _dispatch_mentions_in_response fires, both agents should launch.
        resp = "Splitting the tasks: @hux will take the architecture, @gol will do the implementation.\nReady to launch."
        dispatcher.dispatch_next("dro", resp, "thread-1", 0)
        deadline = time.time() + 1
        while (not hux.calls or not gol.calls) and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(hux.calls), 1)
        self.assertEqual(len(gol.calls), 1)

    def test_json_route_and_text_tags_are_equal_actions(self):
        hux = RecordingAdapter()
        gol = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"hux": hux, "gol": gol},
            system_prompts={"hux": "system", "gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        resp = (
            "@hux check the architecture risk.\n"
            '{"route":"gol","write_intent":false,"arch_decision":false,'
            '"message":"Make a targeted fix."}'
        )
        dispatcher.dispatch_next("dro", resp, "thread-1", 0)
        deadline = time.time() + 1
        while (not hux.calls or not gol.calls) and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(hux.calls), 1)
        self.assertEqual(len(gol.calls), 1)
        self.assertIn("Make a targeted fix.", gol.calls[0]["user_message"])

    def test_json_route_does_not_duplicate_same_text_tag(self):
        gol = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"gol": gol},
            system_prompts={"gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        resp = (
            "@gol make a targeted fix.\n"
            '{"route":"gol","write_intent":false,"arch_decision":false,'
            '"message":"Make a targeted fix."}'
        )
        dispatcher.dispatch_next("dro", resp, "thread-1", 0)
        deadline = time.time() + 1
        while not gol.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(gol.calls), 1)

    def test_sage_tag_always_dispatches_regardless_of_ack_prefix(self):
        # Sage-to-sage tags always dispatch — hops limit and prompt anti-echo
        # rules are the safety net against loops, not the dispatcher filter.
        hux = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"hux": hux},
            system_prompts={"hux": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        resp = "@hux - heard you: agreed, closing the echo loop.\n@boss - here's the final answer."
        dispatched = dispatcher._dispatch_mentions_in_response("dro", resp, "thread-1", 0)
        deadline = time.time() + 1
        while not hux.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertTrue(dispatched)
        self.assertEqual(len(hux.calls), 1)

    def test_long_ack_preface_with_substantive_block_dispatches(self):
        hux = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"hux": hux},
            system_prompts={"hux": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        resp = (
            "@hux - heard you:\n\n"
            "Our context has drifted a bit. The real task isn't just running 500, "
            "it's the batch pipeline with dedupe control, job artifacts, and report "
            "upload verification. The first working step now: Huxley needs to launch "
            "the command through a real WorkRuntime and return a real work_id."
        )
        dispatched = dispatcher._dispatch_mentions_in_response("dro", resp, "thread-1", 0)
        deadline = time.time() + 1
        while not hux.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertTrue(dispatched)
        self.assertEqual(len(hux.calls), 1)
        self.assertIn("WorkRuntime", hux.calls[0]["user_message"])

    def test_sage_tag_with_action_still_dispatches_fallback(self):
        hux = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"hux": hux},
            system_prompts={"hux": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        resp = "@hux - check whether this breaks the route JSON?"
        dispatched = dispatcher._dispatch_mentions_in_response("dro", resp, "thread-1", 0)
        deadline = time.time() + 1
        while not hux.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertTrue(dispatched)
        self.assertEqual(len(hux.calls), 1)
        self.assertIn("check", hux.calls[0]["user_message"])

    def test_unread_context_is_added_without_model_background_calls(self):
        history = [
            {"id": "1", "role": "BOSS", "text": "first", "timestamp": "t1", "type": "message"},
            {"id": "2", "role": "HUXLEY", "text": "reply", "timestamp": "t2", "type": "message"},
            {"id": "3", "role": "BOSS", "text": "untagged note", "timestamp": "t3", "type": "message"},
        ]
        added = []
        adapter = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"dro": adapter},
            system_prompts={"dro": "system"},
            history_provider=lambda: list(history),
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": added.append({
                "id": "4",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            }) or added[-1],
        )

        dispatcher.call_agent("dro", "@dro answer", "3", 0)

        sent_history = adapter.calls[0]["history"]
        context_items = [item for item in sent_history if item.get("type") == "context"]
        self.assertEqual(len(context_items), 1)
        self.assertIn("UNREAD FOR @dro", context_items[0]["text"])
        self.assertIn("untagged note", context_items[0]["text"])

    def test_seen_messages_are_not_repeated_for_same_role(self):
        history = [
            {"id": "1", "role": "BOSS", "text": "first", "timestamp": "t1", "type": "message"},
        ]
        adapter = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"dro": adapter},
            system_prompts={"dro": "system"},
            history_provider=lambda: list(history),
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "2",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
        )

        dispatcher.call_agent("dro", "@dro first", "1", 0)
        dispatcher.call_agent("dro", "@dro second", "1", 0)

        first_contexts = [item for item in adapter.calls[0]["history"] if item.get("type") == "context"]
        second_contexts = [item for item in adapter.calls[1]["history"] if item.get("type") == "context"]
        self.assertEqual(len(first_contexts), 1)
        self.assertEqual(second_contexts, [])


    def test_kes_explicit_nick_route_redirected_to_calling_sage(self):
        # Golem explicitly writes route:"boss", but Huxley was the one launched — must return to Huxley.
        hux = RecordingAdapter()
        gol_calls = []

        class KesAdapter(Adapter):
            def __init__(self):
                super().__init__("gol", "Gol", Path.cwd())
            def call(self, system_prompt, history, user_message, timeout=300,
                     allow_write_tools=False, thread_id=None, on_progress=None):
                gol_calls.append(user_message)
                return CallResult(
                    text='@boss — launch error.\n{"route":"boss","write_intent":false,"arch_decision":false,"message":"ModuleNotFoundError"}',
                    executed_tools=[], successful_fetch_count=0,
                    run_id="run-gol", elapsed=0, error=None, metrics={},
                )
            def healthcheck(self):
                return True, "ok"

        sys_msgs = []
        dispatcher = Dispatcher(
            adapters={"hux": hux, "gol": KesAdapter()},
            system_prompts={"hux": "system", "gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": (
                sys_msgs.append(text) if role == "SYSTEM" else None
            ) or {"id": "1", "role": role, "text": text, "thread_id": thread_id, "type": msg_type},
        )

        # Dispatch Golem on Huxley's behalf (from_role="hux")
        dispatcher.call_agent("gol", "do P3", "t1", 0, from_role="hux")
        deadline = time.time() + 2
        while not hux.calls and time.time() < deadline:
            time.sleep(0.01)

        # Huxley must get the turn back
        self.assertEqual(len(hux.calls), 1, "Huxley must get the turn after Golem")
        self.assertTrue(any("redirecting to @hux" in m for m in sys_msgs), f"sys msgs: {sys_msgs}")

    def test_kes_visible_nick_tag_rewritten_to_calling_joe(self):
        dro = RecordingAdapter()

        class KesAdapter(Adapter):
            def __init__(self):
                super().__init__("gol", "Gol", Path.cwd())

            def call(self, system_prompt, history, user_message, timeout=300,
                     allow_write_tools=False, thread_id=None, on_progress=None):
                return CallResult(
                    text='@boss — done.\n{"route":"boss","write_intent":false,"arch_decision":false,"message":"done"}',
                    executed_tools=[], successful_fetch_count=0,
                    run_id="run-gol", elapsed=0, error=None, metrics={},
                )

            def healthcheck(self):
                return True, "ok"

        added = []
        dispatcher = Dispatcher(
            adapters={"dro": dro, "gol": KesAdapter()},
            system_prompts={"dro": "system", "gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": added.append({
                "id": str(len(added) + 1),
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            }) or added[-1],
        )

        dispatcher.call_agent("gol", "do P3", "t1", 0, from_role="dro")
        deadline = time.time() + 2
        while not dro.calls and time.time() < deadline:
            time.sleep(0.01)

        gol_messages = [item for item in added if item["role"] == "GOLEM"]
        self.assertEqual(len(dro.calls), 1, "Diderot must get the turn after Golem")
        self.assertTrue(gol_messages, f"messages: {added}")
        self.assertTrue(gol_messages[0]["text"].startswith("@dro"), gol_messages[0]["text"])
        self.assertNotIn("@boss", gol_messages[0]["text"].splitlines()[0])

    def test_kes_no_tool_work_report_is_blocked_from_routing(self):
        dro = RecordingAdapter()

        class KesAdapter(Adapter):
            def __init__(self):
                super().__init__("gol", "Gol", Path.cwd())

            def call(self, system_prompt, history, user_message, timeout=300,
                     allow_write_tools=False, thread_id=None, on_progress=None):
                return CallResult(
                    text=(
                        "@dro fixed it\n"
                        "files_changed: project/upload_reports.py\n"
                        "commit: 7b18a2c\n"
                        "verification: tests passed\n"
                        '{"route":"dro","write_intent":false,'
                        '"arch_decision":false,"message":"done"}'
                    ),
                    executed_tools=[],
                    successful_fetch_count=0,
                    run_id="run-gol",
                    elapsed=0,
                    error=None,
                    metrics={},
                )

            def healthcheck(self):
                return True, "ok"

        added = []
        dispatcher = Dispatcher(
            adapters={"dro": dro, "gol": KesAdapter()},
            system_prompts={"dro": "system", "gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": added.append({
                "id": str(len(added) + 1),
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            }) or added[-1],
        )

        dispatcher.call_agent("gol", "do the work", "t1", 0, from_role="dro")
        time.sleep(0.05)

        self.assertEqual(dro.calls, [])
        errors = [item for item in added if item["type"] == "error"]
        self.assertEqual(len(errors), 1, f"messages: {added}")
        self.assertIn("[UNRELIABLE REPORT @gol]", errors[0]["text"])
        self.assertIn("7b18a2c", errors[0]["text"])

    def test_kes_introspection_chat_report_is_not_flagged_phantom(self):
        # Asking Golem to describe itself is chat, not work: a description that
        # looks like a work report (no tools) must NOT be wrapped as untrustworthy.
        class KesAdapter(Adapter):
            def __init__(self):
                super().__init__("gol", "Gol", Path.cwd())

            def call(self, system_prompt, history, user_message, timeout=300,
                     allow_write_tools=False, thread_id=None, on_progress=None):
                return CallResult(
                    text=(
                        "@boss I am Golem, the hands of the Cabinet.\n"
                        "files_changed: none observed\n"
                        "commit: none observed\n"
                        '{"route":"","write_intent":false,'
                        '"arch_decision":false,"message":"done"}'
                    ),
                    executed_tools=[],
                    successful_fetch_count=0,
                    run_id="run-gol",
                    elapsed=0,
                    error=None,
                    metrics={},
                )

            def healthcheck(self):
                return True, "ok"

        added = []
        dispatcher = Dispatcher(
            adapters={"gol": KesAdapter()},
            system_prompts={"gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": added.append({
                "id": str(len(added) + 1),
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            }) or added[-1],
        )

        dispatcher.call_agent("gol", "describe yourself and your role", "t1", 0)
        time.sleep(0.05)

        errors = [item for item in added if item["type"] == "error"]
        self.assertEqual(errors, [], f"chat introspection must not be flagged: {added}")
        messages = [item for item in added if item["type"] == "message"]
        self.assertTrue(
            any("I am Golem" in m["text"] for m in messages),
            f"description should be posted normally: {added}",
        )


    def test_gol_run_delegation_uses_machine_path_not_llm(self):
        # Cabinet norm: a sage hands off repeatable work with the line '@gol run <id>' —
        # it executes as code (action_starter), Golem's LLM is never invoked.
        gol = RecordingAdapter()
        started = []
        dispatcher = Dispatcher(
            adapters={"gol": gol},
            system_prompts={"gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1", "role": role, "text": text,
                "thread_id": thread_id, "type": msg_type,
            },
            action_starter=lambda action_id, from_role: started.append(
                (action_id, from_role)
            ),
        )

        resp = "@gol run candidates_material_20"
        dispatched = dispatcher._dispatch_mentions_in_response("dro", resp, "t1", 0)
        time.sleep(0.05)

        self.assertTrue(dispatched)
        self.assertEqual(started, [("candidates_material_20", "dro")])
        self.assertEqual(gol.calls, [])

    def test_kes_non_run_message_still_goes_to_llm(self):
        gol = RecordingAdapter()
        started = []
        dispatcher = Dispatcher(
            adapters={"gol": gol},
            system_prompts={"gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1", "role": role, "text": text,
                "thread_id": thread_id, "type": msg_type,
            },
            action_starter=lambda action_id, from_role: started.append(
                (action_id, from_role)
            ),
        )

        resp = "@gol read PLAN.md and summarize the first section"
        dispatched = dispatcher._dispatch_mentions_in_response("dro", resp, "t1", 0)
        deadline = time.time() + 1
        while not gol.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertTrue(dispatched)
        self.assertEqual(started, [])
        self.assertEqual(len(gol.calls), 1)


class AddressedBlockTests(unittest.TestCase):
    SAGE_REPLY = (
        "Boss, I looked at the problem. There are three reasons, I'll go in order.\n"
        "Diderot was wrong about the cache.\n"
        "\n"
        "@gol — fix the indent in .think-draft to 38px.\n"
        "Check it visually after the fix.\n"
        "\n"
        "@boss — overall I recommend option B."
    )

    def test_block_runs_from_tag_to_next_tag(self):
        block = routing.extract_addressed_block(self.SAGE_REPLY, "gol")
        self.assertIn("@gol — fix the indent", block)
        self.assertIn("Check it visually", block)
        self.assertNotIn("three reasons", block)
        self.assertNotIn("option B", block)

    def test_inline_tag_starts_mid_line(self):
        text = "First the analysis. Then I'll ask @gol about the indent and come back."
        block = routing.extract_addressed_block(text, "gol")
        self.assertTrue(block.startswith("@gol about the indent"))

    def test_tag_only_in_code_block_returns_none(self):
        text = "Here's an example:\n```\n@gol — this is a listing\n```\nThe end."
        self.assertIsNone(routing.extract_addressed_block(text, "gol"))

    def test_markdown_decorated_tags_bound_block(self):
        text = "**@gol — task one.**\ndetails\n- @dro — look at the architecture\ntail for diderot"
        block = routing.extract_addressed_block(text, "gol")
        self.assertIn("task one", block)
        self.assertIn("details", block)
        self.assertNotIn("architecture", block)

    def test_dispatcher_passes_addressed_block_not_full_text(self):
        gol = RecordingAdapter()
        dispatcher = Dispatcher(
            adapters={"gol": gol},
            system_prompts={"gol": "system"},
            history_provider=lambda: [],
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "1", "role": role, "text": text,
                "thread_id": thread_id, "type": msg_type,
            },
        )

        dispatcher._dispatch_mentions_in_response("hux", self.SAGE_REPLY, "t1", 0)
        deadline = time.time() + 2
        while not gol.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(gol.calls), 1)
        sent = gol.calls[0]["user_message"]
        self.assertIn("@gol — fix the indent", sent)
        self.assertNotIn("Diderot was wrong", sent)
        self.assertNotIn("option B", sent)


class SeenPersistenceTests(unittest.TestCase):
    @staticmethod
    def _make_dispatcher(adapter, history, seen_file):
        return Dispatcher(
            adapters={"dro": adapter},
            system_prompts={"dro": "system"},
            history_provider=lambda: history,
            broadcaster=lambda payload: None,
            add_msg=lambda role, text, thread_id, msg_type="message": {
                "id": "100",
                "role": role,
                "text": text,
                "thread_id": thread_id,
                "type": msg_type,
            },
            seen_file=seen_file,
        )

    def test_last_seen_survives_restart(self):
        import tempfile

        history = [
            {"id": "1", "role": "BOSS", "text": "old", "timestamp": "t", "type": "message"},
            {"id": "2", "role": "DIDEROT", "text": "reply", "timestamp": "t", "type": "message"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            seen_file = Path(tmp) / "agent_seen.json"

            first = self._make_dispatcher(RecordingAdapter(), history, seen_file)
            first.call_agent("dro", "@dro question", None, 0)
            self.assertEqual(first._last_seen_id_by_role.get("dro"), 2)

            # "Restart": a new dispatcher with the same file, history unchanged.
            adapter = RecordingAdapter()
            second = self._make_dispatcher(adapter, history, seen_file)
            self.assertEqual(second._last_seen_id_by_role.get("dro"), 2)

            # No unread blocks at all: everything was already read before the restart.
            second.call_agent("dro", "@dro another question", None, 0)
            sent_history = adapter.calls[0]["history"]
            self.assertFalse(
                [h for h in sent_history if h.get("type") == "context"],
                f"unread context after restart: {sent_history}",
            )

    def test_reset_seen_clears_state_and_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            seen_file = Path(tmp) / "agent_seen.json"
            dispatcher = self._make_dispatcher(RecordingAdapter(), [], seen_file)
            dispatcher._last_seen_id_by_role = {"dro": 7}
            dispatcher._save_seen({"dro": 7})

            dispatcher.reset_seen()
            self.assertEqual(dispatcher._last_seen_id_by_role, {})

            reloaded = self._make_dispatcher(RecordingAdapter(), [], seen_file)
            self.assertEqual(reloaded._last_seen_id_by_role, {})


if __name__ == "__main__":
    unittest.main()
