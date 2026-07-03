"""Dispatcher for Cabinet of Mind.

Routes user @-mentions and optional participant routing JSON to adapters.
Agents run as normal workspace-capable models by default. Pending approval is
only for critical actions that an agent explicitly routes to Boss.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from adapters.base import Adapter, CallResult
from core import routing, storage
from core.validators import check_phantom_claims


_role_locks: dict[str, threading.Lock] = {
    "hux": threading.Lock(),
    "dro": threading.Lock(),
    "gol": threading.Lock(),
}


MAX_UNREAD_ITEMS = 80
MAX_UNREAD_ITEM_CHARS = 1400
# Discussion (plan) gets the longest history: a sage's opinion is only as
# valuable as the amount of the argument it can see. Context without write
# tools is cheaper, so we can afford more of it here than in work/review.
DISCUSS_HISTORY_MSGS = 60
NORMAL_HISTORY_MSGS = 20

CRITICAL_WORDS = (
    "delete", "remove", "wipe", "clear", "purge", "destroy", "reset",
    "secret", "token", "password", "deploy", "publish",
)


def _msg_id_value(item: dict) -> int:
    try:
        return int(item.get("id") or 0)
    except (TypeError, ValueError):
        return 0


_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _strip_code_and_quotes(text: str) -> str:
    """Strips fenced code blocks, inline code, and blockquote lines (`> `).

    Used before searching for @-mentions, so that quoted history and listings
    containing @hux/@dro don't trigger extra fallback-dispatch hops.
    """
    if not text:
        return ""
    stripped = _FENCED_BLOCK_RE.sub(" ", text)
    stripped = _INLINE_CODE_RE.sub(" ", stripped)
    lines = [ln for ln in stripped.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines)


def _shorten(text: str, limit: int = MAX_UNREAD_ITEM_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED {len(text) - limit} chars]"


class Dispatcher:
    """Routes user mentions and optional route tags to the right adapter."""

    def __init__(
        self,
        adapters: dict[str, Adapter],
        system_prompts: dict[str, str],
        history_provider: Callable[[], list[dict]],
        broadcaster: Callable[[dict], None],
        add_msg: Callable[[str, str, str | None, str], dict],
        request_approval: Callable[[dict], None] | None = None,
        max_hops: int = 12,
        seen_file: Path | None = None,
        action_starter: Callable[[str, str | None], None] | None = None,
    ):
        self.adapters = adapters
        self.system_prompts = system_prompts
        self.history_provider = history_provider
        self.broadcaster = broadcaster
        self.add_msg = add_msg
        self.request_approval = request_approval
        self.max_hops = max_hops
        # Golem's machine run-path: the server passes the project-action starter here.
        self.action_starter = action_starter
        self._seen_lock = threading.Lock()
        # last_seen survives a server restart: otherwise after a restart the
        # whole history counts as unread and the unread index duplicates it to agents.
        self._seen_file = seen_file
        self._last_seen_id_by_role: dict[str, int] = (
            storage.load_seen_file(seen_file) if seen_file else {}
        )

    @staticmethod
    def display_role(role: str) -> str:
        return {"hux": "HUXLEY", "dro": "DIDEROT", "gol": "GOLEM"}.get(role, role.upper())

    def call_agent(
        self,
        role: str,
        message: str,
        thread_id: str | None,
        hops: int,
        allow_write_tools: bool = True,
        from_role: str | None = None,
        task_mode: str | None = None,
        terminal: bool = False,
    ) -> None:
        """Call adapter[role], persist result, dispatch follow-ups.

        terminal=True marks a closing turn (e.g. the forced ceiling synthesis):
        the result is posted but never dispatched onward, so it cannot start a
        new round past the exchange ceiling.

        Heartbeat: while the adapter is running we tick {type: thinking_progress}
        every 15s so the UI shows live elapsed time instead of a silent wait.
        Stops when the call returns (heartbeat_stop event).
        """
        adapter = self.adapters.get(role)
        if not adapter:
            self._sys(f"unknown role @{role}")
            return

        lock = _role_locks.get(role)
        if not lock or not lock.acquire(blocking=False):
            self._sys(f"@{role} is already running; duplicate launch skipped")
            return

        try:
            task_mode = task_mode or self._classify_task_mode(message)
            allow_write_tools = allow_write_tools and task_mode not in {"chat", "plan", "review"}
            self.broadcaster({"type": "thinking", "agent": role})
            self._stage(role, "queued", 0)
            t0 = time.time()

            # Heartbeat thread: every 15s broadcast progress while adapter works.
            # Closes the silent-wait gap that caused Boss to think Huxley hung at 17:13.
            heartbeat_stop = threading.Event()

            def _heartbeat() -> None:
                while not heartbeat_stop.wait(15):
                    elapsed_s = int(time.time() - t0)
                    self.broadcaster({
                        "type": "thinking_progress",
                        "agent": role,
                        "elapsed": elapsed_s,
                    })

            hb_thread = threading.Thread(target=_heartbeat, daemon=True)
            hb_thread.start()

            self._stage(role, "context", int(time.time() - t0))
            history = self._history_with_unread_context(role)
            user_message = self._mode_prefixed_message(message, task_mode, from_role, hops, role)
            self._stage(role, "agent_start", int(time.time() - t0))

            def _on_progress(event: dict) -> None:
                """Forward adapter tool-use events to UI via broadcaster."""
                self.broadcaster(event)

            try:
                result: CallResult = adapter.call(
                    system_prompt=self.system_prompts.get(role, ""),
                    history=history,
                    user_message=user_message,
                    timeout=3600,
                    allow_write_tools=allow_write_tools,
                    thread_id=thread_id,
                    on_progress=_on_progress,
                )
            finally:
                heartbeat_stop.set()

            self._stage(role, "agent_done", int(time.time() - t0))
            self._mark_seen(role, history)
            elapsed = time.time() - t0

            if result.error == "cancelled":
                # User explicitly stopped this agent — silent exit, no error posted.
                self.broadcaster({"type": "agent_stopped", "agent": role})
                return

            if result.error:
                if result.text and result.metrics.get("partial_timeout"):
                    visible_text = routing.route_tail_visible_text(result.text) or result.text
                    partial = (
                        f"[PARTIAL RESPONSE @{role}: the model cut off on timeout; "
                        "the text is not finalized and will not be routed]\n\n"
                        f"{visible_text}"
                    )
                    msg = self.add_msg(self.display_role(role), partial, thread_id, "message")
                    msg["elapsed"] = round(elapsed, 1)
                    msg["run_id"] = result.run_id
                    msg["partial_timeout"] = True
                    self.broadcaster({"type": "message", "message": msg})
                    return
                msg = self.add_msg(
                    self.display_role(role),
                    f"[ERROR @{role}] {result.error}",
                    thread_id,
                    "error",
                )
                self.broadcaster({"type": "message", "message": msg})
                return

            result.text = self._normalize_response(role, result.text, from_role)

            # Phantom claim detection: warn if agent claims mutations without tools.
            # Golem/Ollama reports real tool calls; an empty list is meaningful
            # and must not be accepted as a completed work report.
            # Only in work-like modes: in chat/plan (conversation, introspection)
            # we don't expect tools, and a descriptive reply from Golem shouldn't
            # be flagged as an unreliable report.
            phantom_worklike = task_mode not in {"chat", "plan"}
            phantom_warnings = check_phantom_claims(
                result.text,
                result.executed_tools,
                strict_no_tools=(role == "gol" and phantom_worklike),
            )
            for w in phantom_warnings:
                self._sys(f"[phantom @{role}] {w}")
            if role == "gol" and phantom_warnings:
                visible_text = routing.route_tail_visible_text(result.text) or result.text
                warning_text = (
                    f"[UNRELIABLE REPORT @{role}] "
                    "Golem claimed completed work, but Cabinet saw no tool calls. "
                    "This reply will not be routed as successful.\n\n"
                    f"{visible_text}"
                )
                msg = self.add_msg(self.display_role(role), warning_text, thread_id, "error")
                msg["elapsed"] = round(elapsed, 1)
                msg["run_id"] = result.run_id
                msg["phantom_warnings"] = phantom_warnings
                self.broadcaster({"type": "message", "message": msg})
                return

            # Strip routing JSON tail before showing to user.
            # Agents like Golem return pure routing JSON — showing it raw is noise.
            visible_text = routing.route_tail_visible_text(result.text)
            posted_msg = None
            if visible_text:
                posted_msg = self.add_msg(self.display_role(role), visible_text, thread_id, "message")
                posted_msg["elapsed"] = round(elapsed, 1)
                posted_msg["run_id"] = result.run_id
                self.broadcaster({"type": "message", "message": posted_msg})

            # If no visible text was posted, the thinking indicator won't be
            # removed by the 'message' event — clear it explicitly.
            if not posted_msg:
                self.broadcaster({"type": "agent_stopped", "agent": role})

            if not terminal:
                next_thread = thread_id or (posted_msg["id"] if posted_msg else None)
                self.dispatch_next(role, result.text, next_thread, hops)
        finally:
            lock.release()

    def dispatch_next(self, from_role: str, resp: str, thread_id: str | None, hops: int) -> None:
        """Route agent response to the next participant.

        Model:
        - route:"boss" creates pending; Boss decides Y/N, then the same agent
          continues.
        - route:"gol"|"hux"|"dro" is equivalent to tagging that agent.
          Routing JSON does not suppress other @-mentions in the text.
        - Y/N is only for critical actions.
        """
        if hops >= self.max_hops:
            # Hit the discussion's safety-limit ceiling. Don't cut the thread off
            # silently — force one final synthesis to Boss (surviving proposal +
            # unresolved objections) that dispatches no one further. That way a
            # budget exhausted by the argument still yields an actionable
            # conclusion instead of an abandoned thread.
            self._sys(
                f"exchange limit reached ({self.max_hops}); forcing synthesis to Boss"
            )
            self._start_synthesis(from_role, thread_id, hops)
            return

        route = routing.parse_route(resp)
        if not route:
            # No routing JSON — look for agent @-mentions in the reply text.
            self._dispatch_mentions_in_response(from_role, resp, thread_id, hops)
            return

        target = route.lstrip("@")
        next_msg = routing.extract_message(resp) or f"Continue from @{from_role}."
        route_data = routing.parse_route_data(resp)

        if target == "boss":
            if self._route_needs_approval(route_data, next_msg):
                # Critical route to Boss: approval resumes the same agent.
                self._request_boss_approval(from_role, next_msg, thread_id, hops, "boss_addressed", route_data)
                return
            # Non-critical route to Boss is just a final answer, not Y/N.
            if not routing.route_tail_visible_text(resp):
                msg = self.add_msg(self.display_role(from_role), next_msg, thread_id, "message")
                self.broadcaster({"type": "message", "message": msg})
            self._dispatch_mentions_in_response(from_role, resp, thread_id, hops)
            return

        if target == from_role:
            self._sys(f"self-route violation by @{from_role}; Boss direction required")
            self._dispatch_mentions_in_response(from_role, resp, thread_id, hops)
            return

        if target not in self.adapters:
            self._sys(f"route to unknown @{target} from @{from_role}")
            self._dispatch_mentions_in_response(from_role, resp, thread_id, hops)
            return

        # Agent → agent: JSON route is the same kind of action as @tag.
        # Dispatch it, then scan visible text for other tags without launching
        # the same target twice.
        if not (target == "gol" and self._try_gol_run(next_msg, from_role)):
            self._start_agent_thread(target, next_msg, thread_id, hops, from_role)
        self._dispatch_mentions_in_response(from_role, resp, thread_id, hops, skip_roles={target})

    def _start_agent_thread(
        self,
        target: str,
        message: str,
        thread_id: str | None,
        hops: int,
        from_role: str,
    ) -> None:
        threading.Thread(
            target=self.call_agent,
            args=(target, message, thread_id, hops + 1, True, from_role, self._classify_task_mode(message)),
            daemon=True,
        ).start()

    def _try_gol_run(self, message: str, from_role: str | None) -> bool:
        """Cabinet norm: sages reason, repeatable work goes to the machine tool.
        '@gol run <action_id>' executes as code (project action → WorkRuntime,
        handoff metrics get written) — Golem's LLM is never invoked, so
        confabulation on run-tasks is impossible. Any other text to Golem
        goes to the LLM adapter."""
        if not self.action_starter:
            return False
        action_id = routing.parse_gol_run_message(message)
        if not action_id:
            return False
        try:
            self.action_starter(action_id, from_role)
        except Exception as e:
            self._sys(f"gol run '{action_id}' failed: {e}")
        return True

    def _start_synthesis(self, from_role: str, thread_id: str | None, hops: int) -> None:
        """Final synthesis at the discussion ceiling.

        Launches one terminal turn for the fellow sage: they must return the
        surviving proposal and unresolved objections to Boss, without starting
        a new round or tagging their colleague. It's specifically the
        colleague (not from_role) who synthesizes, so as not to compete with
        the calling turn's still-held role-lock; both sages have the full
        argument history. terminal=True suppresses further dispatch, so a
        second pass at the ceiling is impossible.
        """
        synth_role = {"hux": "dro", "dro": "hux"}.get(from_role)
        if not synth_role or synth_role not in self.adapters:
            return
        synthesis_msg = (
            "CABINET_CEILING_SYNTHESIS: the discussion limit has been reached. "
            "Don't start a new round and don't tag your colleague. Return a final "
            "synthesis to @boss: the surviving proposal (what held up after all "
            "objections) plus a list of unresolved objections as residual risk. "
            "If disagreement remains, state it honestly — don't fabricate agreement."
        )
        threading.Thread(
            target=self.call_agent,
            args=(synth_role, synthesis_msg, thread_id, hops, True, from_role, "plan"),
            kwargs={"terminal": True},
            daemon=True,
        ).start()

    def _dispatch_mentions_in_response(
        self,
        from_role: str,
        resp: str,
        thread_id: str | None,
        hops: int,
        skip_roles: set[str] | None = None,
    ) -> bool:
        """Fallback: if there's no routing JSON, look for agent @-mentions in the text.

        Agents can address each other via @dro/@hux in the body of a reply —
        the same way Boss does. We dispatch everyone mentioned (except
        themselves and @boss). Returns True if anyone was launched.

        We ignore mentions inside code blocks ```...``` and markdown quotes
        (`> `), so that quoting history/code doesn't generate extra hops
        toward max_hops.

        Each one launched gets the block addressed to them (from their tag to
        the next participant's tag), not the whole reply: they see the rest of
        the text in history as context, not as their instruction. If the block
        couldn't be isolated, we pass the full text, as before.
        """
        visible = _strip_code_and_quotes(resp)
        mentions = routing.parse_mentions(visible)
        dispatched = False
        skip_roles = skip_roles or set()
        visible_resp = routing.route_tail_visible_text(resp)
        for mention in mentions:
            tag = mention.lstrip("@")
            if tag == from_role or tag == "boss" or tag in skip_roles or tag not in self.adapters:
                continue
            next_msg = routing.extract_addressed_block(visible_resp, tag) or resp
            if not routing.should_dispatch_addressed_block(next_msg, tag):
                continue
            if tag == "gol" and self._try_gol_run(next_msg, from_role):
                dispatched = True
                continue
            self._start_agent_thread(tag, next_msg, thread_id, hops, from_role)
            dispatched = True
        return dispatched

    def _request_boss_approval(
        self,
        from_role: str,
        message: str,
        thread_id: str | None,
        hops: int,
        kind: str,
        route_data: dict | None = None,
    ) -> None:
        if not self.request_approval:
            self._sys("Boss approval required but no approval handler is configured")
            return
        label = (message or f"@{from_role} asks Boss").strip()
        route_data = route_data or {}
        self.request_approval({
            "from_role": from_role,
            "target_role": from_role,   # after approve, same agent continues
            "message": message,
            "thread_id": thread_id,
            "hops": hops,
            "allow_write_tools": True,
            "kind": kind,
            "label": label[:180],
            "summary": self._approval_summary(from_role, label),
            "reason": self._approval_reason(route_data, label),
        })

    def handle_user_mentions(self, text: str, thread_id: str | None) -> None:
        mentions = routing.parse_mentions(text)
        if "@cabinet" in mentions:
            # @cabinet = both sages, in parallel, independently.
            for sage in ("hux", "dro"):
                if sage in self.adapters:
                    threading.Thread(
                        target=self.call_agent,
                        args=(sage, text, thread_id, 0, True, None, self._classify_task_mode(text)),
                        daemon=True,
                    ).start()
            return

        for mention in mentions:
            tag = mention.lstrip("@")
            if tag in ("hux", "dro", "gol"):
                threading.Thread(
                    target=self.call_agent,
                    args=(tag, text, thread_id, 0, True, None, self._classify_task_mode(text)),
                    daemon=True,
                ).start()

    def continue_approved(self, pending: dict) -> None:
        target = pending.get("target_role")
        if not target:
            self._sys("approval acknowledged")
            return
        approved_message = self._approved_message(pending)
        threading.Thread(
            target=self.call_agent,
            args=(
                target,
                approved_message,
                pending.get("thread_id"),
                int(pending.get("hops") or 0),
                True,
                None,
                "work",
            ),
            daemon=True,
        ).start()

    def stop_agent(self, role: str) -> None:
        """Cancel the running call for role. UI will receive agent_stopped."""
        adapter = self.adapters.get(role)
        if adapter:
            adapter.cancel()

    def stop_all_agents(self) -> None:
        """Cancel every in-flight adapter call during server shutdown."""
        for adapter in self.adapters.values():
            try:
                adapter.cancel()
            except Exception:
                pass

    def _sys(self, text: str) -> None:
        msg = self.add_msg("SYSTEM", f"[dispatcher] {text}", None, "system")
        self.broadcaster({"type": "message", "message": msg})

    def _stage(self, role: str, stage: str, elapsed: int = 0) -> None:
        self.broadcaster({
            "type": "thinking_stage",
            "agent": role,
            "stage": stage,
            "elapsed": elapsed,
        })

    def _route_needs_approval(self, route_data: dict, message: str) -> bool:
        if self._truthy(route_data.get("write_intent")) or self._truthy(route_data.get("arch_decision")):
            return True
        blob = f"{message or ''} {json.dumps(route_data, ensure_ascii=False)}".lower()
        return any(word in blob for word in CRITICAL_WORDS)

    def _approval_summary(self, from_role: str, label: str) -> str:
        actor = self.display_role(from_role)
        one_line = " ".join((label or "").split())
        if len(one_line) > 120:
            one_line = one_line[:117].rstrip() + "..."
        return f"{actor}: {one_line or 'confirm action'}"

    def _approval_reason(self, route_data: dict, label: str) -> str:
        if self._truthy(route_data.get("write_intent")):
            return "edit/write"
        if self._truthy(route_data.get("arch_decision")):
            return "architecture decision"
        low = (label or "").lower()
        for word in CRITICAL_WORDS:
            if word in low:
                return f"critical: {word}"
        return "critical action"

    @staticmethod
    def _truthy(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}
        return bool(value)

    def _classify_task_mode(self, text: str) -> str:
        t = (text or "").lower()
        if any(word in t for word in ("full", "full run", "all tests", "full tests")):
            return "full"
        if any(word in t for word in ("review", "code check", "check", "look at", "report")):
            return "review"
        if any(word in t for word in ("plan", "discuss", "what do you think", "how does this", "why")):
            return "plan"
        if any(word in t for word in (
            "fix", "implement", "do", "add", "make",
            "delete", "commit",
        )):
            return "work"
        # Introspection/greeting is conversation, not work: we don't expect
        # tools, and we don't apply the phantom check. Checked after the
        # work verbs so that "hi, do X" still counts as work.
        if any(word in t for word in (
            "describe yourself", "tell me about yourself", "who are you",
            "introduce yourself", "hello", "greetings",
        )):
            return "chat"
        return "work"

    def _mode_prefixed_message(
        self,
        message: str,
        task_mode: str,
        from_role: str | None = None,
        hops: int = 0,
        role: str | None = None,
    ) -> str:
        limits = {"plan": DISCUSS_HISTORY_MSGS, "review": NORMAL_HISTORY_MSGS}
        history_limit = limits.get(task_mode, NORMAL_HISTORY_MSGS)
        from_line = f"CABINET_FROM: {from_role}\n" if from_role else ""
        # Turn budget is shown only to sages: the critical argument is finite, and
        # every turn costs subscription tokens. We don't send this to Golem
        # (narrow 8192 context).
        budget_line = ""
        if role in ("hux", "dro"):
            budget_line = (
                f"CABINET_TURN_BUDGET: exchange {hops + 1} of {self.max_hops}. "
                "This is a safety-limit ceiling, not a goal — most decisions need only 2-4 exchanges. "
                "Empty agreement (\"heard you, agreed\") wastes a turn from a finite budget. "
                "If an objection isn't resolved at the limit, return an honest disagreement to Boss; "
                "don't fabricate agreement for the sake of the budget. The end of an argument must name "
                "what changed because of the argument; if nothing changed, write: "
                "\"decision unchanged; high risk of this being ritual\".\n"
            )
        return (
            f"CABINET_TASK_MODE: {task_mode}\n"
            f"CABINET_HISTORY_LIMIT: {history_limit}\n"
            f"{budget_line}"
            f"{from_line}\n"
            f"{message}"
        )

    def _approved_message(self, pending: dict) -> str:
        label = pending.get("label") or ""
        message = pending.get("message") or label or "Continue the approved request."
        comment = (pending.get("boss_comment") or "").strip()
        access = (
            "Boss approved this pending request. Continue as a normal workspace-capable "
            "agent. Stay inside the approved task, verify changes, and commit when files change."
        )
        parts = [access, f"\nApproved item:\n{label}", f"\nCurrent task:\n{message}"]
        if comment:
            parts.append(f"\nNick's comment: {comment}")
        return "\n".join(parts)

    def _normalize_response(self, role: str, text: str, from_role: str | None = None) -> str:
        if role != "gol":
            return text
        route_data = routing.parse_route_data(text)
        has_route_json = "route" in route_data
        route_val = routing.parse_route(text)
        # Contract sage → gol → sage: Golem always returns the turn to the calling sage.
        # We intercept both missing JSON and an explicit route:"boss" from the sending sage.
        if from_role not in ("hux", "dro"):
            if route_val or has_route_json:
                return text
            target = "boss"
        else:
            target = from_role

        if from_role in ("hux", "dro") and route_val == f"@{target}":
            visible = routing.route_tail_visible_text(text) if has_route_json else (text or "").rstrip()
            retagged = self._retarget_visible_tag(visible, target)
            if retagged == visible:
                return text
            route_data.update({
                "route": target,
                "write_intent": False,
                "arch_decision": False,
                "message": route_data.get("message") or f"Returning from @gol to @{target}.",
            })
            tail = json.dumps(route_data, ensure_ascii=False)
            return retagged.rstrip() + f"\n\n```json\n{tail}\n```"

        if from_role not in ("hux", "dro") and (route_val or has_route_json):
            return text

        if not route_val and not has_route_json:
            self._sys(f"@gol missed required routing JSON; returning to @{target}")
        else:
            self._sys(f"@gol routed to @boss but was dispatched by @{from_role}; redirecting to @{target}")
        route_data.update({
            "route": target,
            "write_intent": False,
            "arch_decision": False,
            "message": route_data.get("message") or f"Returning from @gol to @{target}.",
        })
        visible = routing.route_tail_visible_text(text) if has_route_json else (text or "").rstrip()
        visible = self._retarget_visible_tag(visible, target)
        tail = json.dumps(route_data, ensure_ascii=False)
        return visible.rstrip() + f"\n\n```json\n{tail}\n```"

    @staticmethod
    def _retarget_visible_tag(visible: str, target: str) -> str:
        text = (visible or "").rstrip()
        if not text:
            return f"@{target} —"
        retagged = re.sub(r"^(\s*)@(hux|dro|boss)\b", rf"\1@{target}", text, count=1, flags=re.IGNORECASE)
        if retagged != text:
            return retagged
        return f"@{target} — {text.lstrip()}"

    def _history_with_unread_context(self, role: str) -> list[dict]:
        history = self.history_provider()
        with self._seen_lock:
            last_seen = self._last_seen_id_by_role.get(role, 0)

        # Exclude meta types from unread index:
        # - "context": synthetic index blocks (would cause recursion)
        # - "system" / "approval": approval labels contain instruction-like text that
        #   agents (especially Golem) misread as completed facts. They are visible
        #   in full history but should not appear in the unread summary.
        _EXCLUDED_TYPES = {"context", "system", "approval"}
        unread = [
            item for item in history
            if _msg_id_value(item) > last_seen and item.get("type") not in _EXCLUDED_TYPES
        ]
        if not unread:
            return history

        visible = unread[-MAX_UNREAD_ITEMS:]
        hidden = max(0, len(unread) - len(visible))
        lines = []
        if hidden:
            lines.append(f"[{hidden} older unread messages omitted from this index; full history is still above]")
        for item in visible:
            ts = item.get("timestamp", "")
            msg_role = item.get("role", "?")
            text = _shorten(item.get("text", ""))
            lines.append(f"- id={item.get('id')} [{ts}] {msg_role}: {text}")

        context = {
            "id": f"context-unread-{role}-{int(time.time())}",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "role": "CABINET_CONTEXT",
            "type": "context",
            "thread_id": None,
            "text": (
                f"UNREAD FOR @{role} SINCE YOUR LAST TURN.\n"
                "These messages were written while you were not running. "
                "Treat them as active conversation context. Do not answer only this block; "
                "answer Boss's current request.\n\n"
                + "\n".join(lines)
            ),
        }
        return history + [context]

    def _mark_seen(self, role: str, history: list[dict]) -> None:
        max_seen = 0
        for item in history:
            if item.get("type") == "context":
                continue
            max_seen = max(max_seen, _msg_id_value(item))
        if not max_seen:
            return
        with self._seen_lock:
            self._last_seen_id_by_role[role] = max(
                self._last_seen_id_by_role.get(role, 0),
                max_seen,
            )
            snapshot = dict(self._last_seen_id_by_role)
        self._save_seen(snapshot)

    def _save_seen(self, snapshot: dict[str, int]) -> None:
        if not self._seen_file:
            return
        try:
            storage.save_seen_file(self._seen_file, snapshot)
        except Exception as e:
            self._sys(f"seen-file save skipped: {e}")

    def reset_seen(self) -> None:
        """Clear per-role last_seen. Called when chat history is cleared:
        message ids restart from 1, so stale counters would mark everything
        as already read."""
        with self._seen_lock:
            self._last_seen_id_by_role = {}
        self._save_seen({})
