"""Adapter for Claude Code CLI.

Runs `claude.exe` with the prompt via stdin (avoids Windows CreateProcessW
32K argv limit). The CLI uses Claude Pro subscription auth, NOT a separate API key.

The CLI handles tool calling natively (Read, Bash, Edit, etc.). We capture the
final assistant response as stdout and parse it.

Permission mode: `bypassPermissions` — no approval dialogs, full workspace access.
Two-model strategy: opus for write tasks, sonnet for read/analysis.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .base import Adapter, CallResult


# ── Tool-label helpers ─────────────────────────────────────────────────
def _tool_label(name: str, inp: dict | None) -> str:
    """Human-readable label for a Claude Code tool call — used in UI progress."""
    inp = inp or {}
    if name == "Read":
        fp = inp.get("file_path") or inp.get("path") or ""
        return f"📖 {Path(fp).name}" if fp else "📖 reading a file"
    if name in ("Write", "Edit"):
        fp = inp.get("file_path") or inp.get("path") or ""
        return f"✏️ {Path(fp).name}" if fp else "✏️ writing a file"
    if name == "Bash":
        cmd = (inp.get("command") or inp.get("cmd") or "")[:60]
        return f"▶ {cmd}" if cmd else "▶ bash"
    if name in ("Grep", "Glob"):
        pat = inp.get("pattern") or inp.get("glob") or inp.get("include") or ""
        return f"🔍 {str(pat)[:40]}" if pat else "🔍 searching"
    if name == "WebFetch":
        url = inp.get("url") or inp.get("prompt") or ""
        return f"🌐 {str(url)[:50]}" if url else "🌐 web"
    if name == "LS":
        path = inp.get("path") or ""
        return f"📂 {Path(path).name or path}" if path else "📂 ls"
    return f"⚙ {name}"


class _TextDraftStreamer:
    """Accumulates response text from stream-json and sends it to the UI as a draft.

    committed — text of completed assistant messages (between tool calls),
    partial — the current block from stream_event deltas. A full assistant event
    serves as a resync point: a lost delta doesn't corrupt the draft, because
    the completed text replaces the accumulated deltas entirely.
    Emission is throttled so we don't send a WS event on every token.
    """

    EMIT_INTERVAL = 0.3

    def __init__(self, on_progress: Callable[[dict], None], role: str):
        self.on_progress = on_progress
        self.role = role
        self.committed = ""
        self.partial = ""
        self._last_emit = 0.0

    def _emit(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < self.EMIT_INTERVAL:
            return
        sep = "\n\n" if self.committed and self.partial else ""
        text = (self.committed + sep + self.partial).strip()
        if not text:
            return
        self._last_emit = now
        self.on_progress({"type": "agent_text_draft", "agent": self.role, "text": text})

    def block_started(self) -> None:
        if self.partial and not self.partial.endswith("\n\n"):
            self.partial += "\n\n"

    def feed_delta(self, delta: str) -> None:
        self.partial += delta
        self._emit()

    def assistant_text(self, text_blocks: list[str]) -> None:
        joined = "\n\n".join(text_blocks)
        if joined:
            self.committed = (self.committed + "\n\n" if self.committed else "") + joined
        self.partial = ""
        self._emit(force=True)


def _emit_stream_progress(
    line: str,
    on_progress: Callable[[dict], None],
    role: str,
    draft: _TextDraftStreamer | None = None,
) -> None:
    """Parse one line of Claude Code --output-format stream-json and emit tool events."""
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    et = ev.get("type")
    # --include-partial-messages: raw API stream events wrapped in stream_event.
    if et == "stream_event" and draft is not None:
        inner = ev.get("event") or {}
        it = inner.get("type")
        if it == "content_block_start":
            block = inner.get("content_block") or {}
            if block.get("type") == "text":
                draft.block_started()
        elif it == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                draft.feed_delta(delta["text"])
        return
    # assistant messages carry tool_use blocks in their content array
    if et == "assistant":
        msg = ev.get("message") or {}
        content = msg.get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                inp = block.get("input") or {}
                on_progress({
                    "type": "agent_tool_use",
                    "agent": role,
                    "tool": name,
                    "label": _tool_label(name, inp),
                })
        if draft is not None:
            texts = [
                b.get("text") for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ]
            if texts:
                draft.assistant_text(texts)


_ERRORS_LOG = Path(__file__).resolve().parents[1] / "logs" / "errors.jsonl"
_LIMITS_FILE = Path(__file__).resolve().parents[1] / "logs" / "claude_limits.json"


def _save_claude_limits(info: dict) -> None:
    """Persist the latest rate_limit_info from Claude Code stream-json output.

    The file is read back by server.read_latest_claude_limits to populate the
    UI header (Huxley reset time / overage status).
    """
    try:
        _LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {**info, "captured_at": datetime.now().isoformat(timespec="seconds")}
        _LIMITS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def parse_stream_json(stdout: str) -> tuple[str, dict | None, bool]:
    """Parse Claude Code --output-format stream-json output.

    Returns (final_text, rate_limit_info, is_error).
    """
    text = ""
    rate_limit_info: dict | None = None
    is_error = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = ev.get("type")
        if et == "rate_limit_event":
            info = ev.get("rate_limit_info")
            if isinstance(info, dict):
                rate_limit_info = info
        elif et == "result":
            result_text = ev.get("result")
            if isinstance(result_text, str):
                text = result_text
            is_error = bool(ev.get("is_error"))
    return text, rate_limit_info, is_error


def _log_error(adapter: str, run_id: str, rc: int, diagnostic: str, **extra) -> None:
    """Append a full error record to logs/errors.jsonl for postmortem.

    Stderr is preserved in full — the rc=4294967295 abort in the 17:13 session
    truncated to nothing under the old 500-char cap.
    """
    try:
        _ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "adapter": adapter,
            "run_id": run_id,
            "rc": rc,
            "diagnostic": diagnostic,
            **extra,
        }
        with open(_ERRORS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _runtime_error_label(error: str) -> str:
    low = (error or "").lower()
    if any(s in low for s in ("out of usage", "usage limit", "rate limit", "quota", "resets")):
        return "limit"
    if any(s in low for s in ("auth", "login", "unauthorized", "forbidden", "api key")):
        return "auth"
    if any(s in low for s in ("not found", "missing")):
        return "missing"
    return "error"


def _find_claude_exe() -> str:
    """Locate the Claude Code CLI: CABINET_CLAUDE_EXE env override, then PATH,
    then the default npm global install location."""
    override = os.environ.get("CABINET_CLAUDE_EXE")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    return str(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules"
               / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")


CLAUDE_EXE = _find_claude_exe()


class ClaudeCodeAdapter(Adapter):
    """Adapter for Huxley — uses Claude Code CLI via subprocess.

    Two-model strategy for speed:
    - sonnet: fast replies, analysis, simple questions
    - opus:   complex tasks, writes, architecture

    Always bypassPermissions + all tools.
    Auth: Claude Pro subscription (no separate API key needed).
    """

    # Aliases ("sonnet"/"opus"), not full model ids — full ids like
    # "claude-sonnet-4-6" resolve to the 1M-context variant, which needs
    # separate paid usage credits and 429s when the subscription doesn't
    # have them. Aliases resolve to the standard 200k context, covered by
    # the Pro/Max subscription's 5-hour window like any other Claude Code session.
    SONNET = "sonnet"
    OPUS   = "opus"
    DEFAULT_MODEL = OPUS
    TOOLS = "Read,Glob,Grep,WebFetch,Bash,Write,Edit"
    MAX_HISTORY_MSGS = 12   # was 20 — trimmed to reduce tokens and latency
    FAST_HISTORY_MSGS = 8
    _SKIP_TYPES = {"system", "approval", "error"}

    def __init__(self, role: str = "hux", name: str = "Huxley", workspace: Path | None = None,
                 model: str | None = None, tools: str | None = None,
                 claude_exe: str | None = None):
        super().__init__(role, name, workspace or Path.cwd())
        self.model = model or self.DEFAULT_MODEL
        self.tools = tools or self.TOOLS
        self.claude_exe = claude_exe or CLAUDE_EXE

        if not Path(self.claude_exe).exists():
            raise FileNotFoundError(f"claude.exe not found at {self.claude_exe}")
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        """Terminate the running claude.exe subprocess, if any."""
        self._cancelled = True
        with self._proc_lock:
            if self._proc:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    def _task_mode_from(self, user_message: str) -> str:
        """Extract CABINET_TASK_MODE from the message header."""
        for line in (user_message or "").splitlines()[:4]:
            if line.startswith("CABINET_TASK_MODE:"):
                return line.split(":", 1)[1].strip()
        return "work"

    def _model_and_effort(self, user_message: str, allow_write_tools: bool) -> tuple[str, str]:
        """Pick model+effort based on task mode.

        Strategy (saves quota):
        - No write tools  → sonnet + low  (chat/plan/review)
        - write + full    → opus  + medium (complex architecture tasks)
        - write + other   → sonnet + low   (most work tasks)
        Manual !model override respected by caller.
        """
        if not allow_write_tools:
            return self.SONNET, "low"
        if self._task_mode_from(user_message) == "full":
            return self.OPUS, "medium"
        return self.SONNET, "low"

    def _history_limit_for(self, user_message: str) -> int:
        for line in (user_message or "").splitlines()[:4]:
            if line.startswith("CABINET_HISTORY_LIMIT:"):
                try:
                    return max(4, min(60, int(line.split(":", 1)[1].strip())))
                except ValueError:
                    return self.MAX_HISTORY_MSGS
        if (user_message or "").startswith(("CABINET_TASK_MODE: chat", "CABINET_TASK_MODE: plan")):
            return self.FAST_HISTORY_MSGS
        return self.MAX_HISTORY_MSGS

    def _build_prompt(self, system_prompt: str, history: list[dict], user_message: str) -> str:
        """Assemble prompt for the CLI.

        Sends system + trimmed history (last MAX_HISTORY_MSGS, no system/approval
        noise) + unread-context blocks + current message.

        Why trim: full history grows unboundedly. Trimming keeps calls fast,
        while the dispatcher's unread-context block covers the gap.
        """
        parts = [system_prompt]
        history_limit = self._history_limit_for(user_message)
        if history:
            # Separate synthetic context blocks (built by dispatcher) from
            # real messages. Context blocks always go in — they are already
            # a compressed summary. Real messages are trimmed.
            context_items = [h for h in history if h.get("type") == "context"]
            real_items = [
                h for h in history
                if h.get("type") not in self._SKIP_TYPES | {"context"}
            ]
            trimmed = real_items[-history_limit:]
            omitted = len(real_items) - len(trimmed)

            hist_lines = []
            if omitted:
                hist_lines.append(
                    f"[{omitted} older messages omitted — use Read tool if you need them]"
                )
            for item in trimmed:
                ts = item.get("timestamp", "")
                role = item.get("role", "?")
                text = item.get("text", "")
                attachments = item.get("attachments") or []
                if attachments:
                    lines = ["", "Attachments:"]
                    for att in attachments:
                        lines.append(f"- {att.get('name')}: {att.get('path')}")
                    text = (text or "").rstrip() + "\n" + "\n".join(lines)
                hist_lines.append(f"[{ts}] {role}: {text}")
            for item in context_items:
                hist_lines.append(item.get("text", ""))

            parts.append("\n=== HISTORY ===\n" + "\n\n".join(hist_lines))
        parts.append("\n=== MESSAGE ===\n" + user_message + "\n\nReply.")
        return "\n".join(parts)

    def call(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        timeout: int = 3600,
        allow_write_tools: bool = True,
        thread_id: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> CallResult:
        run_id = str(uuid.uuid4())
        prompt = self._build_prompt(system_prompt, history, user_message)
        # Model by task: sonnet for most tasks, opus only for full.
        # If the model was switched manually via !model — respect Boss's choice.
        if self.model == self.DEFAULT_MODEL:
            model, effort = self._model_and_effort(user_message, allow_write_tools)
        else:
            model = self.model
            effort = "medium"
        cmd = [
            self.claude_exe,
            "--model", model,
            "--print",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode", "bypassPermissions",
            "--allowedTools", self.tools,
            "--effort", effort,
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        self._cancelled = False
        t0 = time.time()
        for attempt in range(3):
            with self._proc_lock:
                if self._cancelled:
                    break
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8",
                    cwd=str(self.workspace), env=env,
                )
                self._proc = proc
            # ── Streaming I/O ──────────────────────────────────────────
            # Replaces proc.communicate() to emit real-time tool events.
            stdout_lines: list[str] = []
            stderr_parts: list[str] = []
            timed_out = False
            draft = _TextDraftStreamer(on_progress, self.role) if on_progress else None

            def _write_stdin() -> None:
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except Exception:
                    pass

            def _read_stdout() -> None:
                try:
                    for raw_line in proc.stdout:
                        ln = raw_line.rstrip("\n")
                        stdout_lines.append(ln)
                        if on_progress and ln.startswith("{"):
                            _emit_stream_progress(ln, on_progress, self.role, draft)
                except Exception:
                    pass

            def _read_stderr() -> None:
                try:
                    stderr_parts.append(proc.stderr.read())
                except Exception:
                    pass

            io_threads = [
                threading.Thread(target=_write_stdin, daemon=True),
                threading.Thread(target=_read_stdout, daemon=True),
                threading.Thread(target=_read_stderr, daemon=True),
            ]
            for t in io_threads:
                t.start()

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                timed_out = True
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            except Exception as e:
                if self._cancelled:
                    for t in io_threads:
                        t.join(timeout=1)
                    break
                self.mark_runtime_error(f"error: call failed: {e!r}")
                for t in io_threads:
                    t.join(timeout=1)
                return CallResult(
                    text="", executed_tools=[], successful_fetch_count=0,
                    run_id=run_id, elapsed=time.time() - t0,
                    error=f"call failed: {e!r}", metrics={},
                )
            finally:
                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None

            for t in io_threads:
                t.join(timeout=2)

            stdout = "\n".join(stdout_lines)
            stderr = "".join(stderr_parts)

            if timed_out:
                if self._cancelled:
                    break
                if attempt < 2:
                    continue
                self.mark_runtime_error(f"error: timeout {timeout}s")
                return CallResult(
                    text="", executed_tools=[], successful_fetch_count=0,
                    run_id=run_id, elapsed=time.time() - t0,
                    error=f"timeout {timeout}s", metrics={},
                )

            if self._cancelled:
                break

            elapsed = time.time() - t0
            rc = proc.returncode
            stream_text, rate_limit_info, stream_is_error = parse_stream_json(stdout)
            if rate_limit_info:
                _save_claude_limits(rate_limit_info)
            if rc != 0:
                full_diag = (stderr or stdout or "").strip()
                snippet = full_diag[-1500:] if len(full_diag) > 1500 else full_diag
                _log_error("claude_code_cli", run_id, rc, full_diag, prompt_len=len(prompt))
                # rc=4294967295 (0xFFFFFFFF) = Windows killed the process
                # (OOM, anti-malware, EXCEPTION_ACCESS_VIOLATION). Stderr empty.
                killed_by_os = rc in (4294967295, 0xFFFFFFFF, -1)
                transient = killed_by_os or any(m in snippet.lower() for m in
                                ("api error: terminated", "econnreset", "etimedout",
                                 "socket hang up", "fetch failed", "network"))
                if transient and attempt < 2:
                    time.sleep(3)
                    continue
                err = f"Code CLI rc={rc}: {snippet}"
                self.mark_runtime_error(f"{_runtime_error_label(err)}: {snippet}")
                return CallResult(
                    text="", executed_tools=[], successful_fetch_count=0,
                    run_id=run_id, elapsed=elapsed,
                    error=err, metrics={},
                )
            text = (stream_text or "").strip()
            if stream_is_error and text:
                self.mark_runtime_error(f"{_runtime_error_label(text)}: {text[:200]}")
                return CallResult(
                    text="", executed_tools=[], successful_fetch_count=0,
                    run_id=run_id, elapsed=elapsed,
                    error=text[:500], metrics={},
                )
            if text:
                self.mark_runtime_ok()
            else:
                self.mark_runtime_error("error: empty response")
            return CallResult(
                text=text or "[ERROR] empty response",
                executed_tools=[],
                successful_fetch_count=0,
                run_id=run_id,
                elapsed=elapsed,
                error=None if text else "empty response",
                metrics={"attempt": attempt + 1},
            )

        if self._cancelled:
            return CallResult(
                text="", executed_tools=[], successful_fetch_count=0,
                run_id=run_id, elapsed=time.time() - t0,
                error="cancelled", metrics={},
            )
        self.mark_runtime_error("error: all retries exhausted")
        return CallResult(
            text="", executed_tools=[], successful_fetch_count=0,
            run_id=run_id, elapsed=time.time() - t0,
            error="all retries exhausted", metrics={},
        )

    def healthcheck(self) -> tuple[bool, str]:
        if not Path(self.claude_exe).exists():
            return False, f"claude.exe missing at {self.claude_exe}"
        try:
            r = subprocess.run(
                [self.claude_exe, "--version"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8",
            )
            version = (r.stdout or r.stderr or "").strip().splitlines()[0][:80]
            if r.returncode == 0:
                return True, f"ok model={self.model} cli={version}"
            return False, f"--version rc={r.returncode}: {version}"
        except Exception as e:
            return False, f"--version failed: {e!r}"
