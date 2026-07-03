"""Adapter for Codex CLI.

Runs `codex.cmd exec` with the prompt via stdin. Uses ChatGPT subscription auth.

Runs with --dangerously-bypass-approvals-and-sandbox — no OS-level DENY ACLs,
full filesystem access, same as Huxley (bypassPermissions). Cabinet Y/N protocol
handles critical action approval externally.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .base import Adapter, CallResult


# ── Tool-label helpers ─────────────────────────────────────────────────
def _tool_label(name: str, inp: dict | None) -> str:
    """Human-readable label for a Codex tool call — mirrors claude_code_cli version."""
    inp = inp or {}
    if name in ("Read", "read_file"):
        fp = inp.get("file_path") or inp.get("path") or ""
        return f"📖 {Path(fp).name}" if fp else "📖 reading a file"
    if name in ("Write", "Edit", "write_file"):
        fp = inp.get("file_path") or inp.get("path") or ""
        return f"✏️ {Path(fp).name}" if fp else "✏️ writing a file"
    if name in ("Bash", "shell", "shell_command"):
        cmd = (inp.get("command") or inp.get("cmd") or "")[:60]
        return f"▶ {cmd}" if cmd else "▶ bash"
    if name in ("Grep", "Glob", "search"):
        pat = inp.get("pattern") or inp.get("glob") or inp.get("query") or ""
        return f"🔍 {str(pat)[:40]}" if pat else "🔍 searching"
    if name in ("WebFetch", "web_search", "web.run"):
        url = inp.get("url") or inp.get("query") or inp.get("prompt") or ""
        return f"🌐 {str(url)[:50]}" if url else "🌐 web"
    return f"⚙ {name}"


class _CodexDraftStreamer:
    """Sends Codex's intermediate agent_message events to the UI as a growing draft.

    Codex emits text as complete messages between tool calls (plus delta events
    in newer versions). Draft = completed messages, separated by a blank line,
    plus the current delta chunk.
    """

    EMIT_INTERVAL = 0.3

    def __init__(self, on_progress: Callable[[dict], None], role: str):
        self.on_progress = on_progress
        self.role = role
        self.messages: list[str] = []
        self.partial = ""
        self._last_emit = 0.0

    def _emit(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < self.EMIT_INTERVAL:
            return
        text = "\n\n".join([*self.messages, self.partial]).strip()
        if not text:
            return
        self._last_emit = now
        self.on_progress({"type": "agent_text_draft", "agent": self.role, "text": text})

    def message(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.messages.append(text)
        self.partial = ""
        self._emit(force=True)

    def delta(self, chunk: str) -> None:
        self.partial += chunk
        self._emit()


def _emit_codex_line_progress(
    line: str,
    on_progress: Callable[[dict], None],
    role: str,
    draft: _CodexDraftStreamer | None = None,
) -> None:
    """Parse one Codex JSONL event line and emit tool events."""
    if not line.startswith("{"):
        return
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    typ = str(ev.get("type") or "")
    if typ in ("function_call", "tool_use", "tool_call"):
        name = str(ev.get("name") or ev.get("tool_name") or "")
        if not name:
            return
        inp = ev.get("input") or ev.get("arguments") or {}
        if isinstance(inp, str):
            try:
                inp = json.loads(inp)
            except Exception:
                inp = {}
        on_progress({
            "type": "agent_tool_use",
            "agent": role,
            "tool": name,
            "label": _tool_label(name, inp),
        })
        return
    if draft is None:
        return
    # Text events show up both at the top level and inside payload/item —
    # the same shapes accepted by the final parser _extract_codex_jsonl.
    candidates = [ev]
    for key in ("payload", "item", "msg"):
        child = ev.get(key)
        if isinstance(child, dict):
            candidates.append(child)
    for obj in candidates:
        obj_typ = str(obj.get("type") or "")
        if obj_typ in ("agent_message", "assistant_message"):
            text = obj.get("text") or obj.get("message")
            if isinstance(text, str) and text.strip():
                draft.message(text)
                return
        if obj_typ == "agent_message_delta":
            chunk = obj.get("delta") or obj.get("text")
            if isinstance(chunk, str) and chunk:
                draft.delta(chunk)
                return


_ERRORS_LOG = Path(__file__).resolve().parents[1] / "logs" / "errors.jsonl"
_WEB_TOOL_NAMES = {"web.run", "web_fetch", "webfetch", "web"}
_DEFAULT_STARTUP_TIMEOUT = 60.0
_DEFAULT_IDLE_TIMEOUT = 300.0
_MAX_HISTORY_ITEM_CHARS = 6000
_MAX_CONTEXT_ITEM_CHARS = 20000
_FATAL_STDERR_PATTERNS = (
    "403 Forbidden",
    "unsupported_country_region_territory",
    "Country, region, or territory not supported",
    "Unable to load site",
    "request_forbidden",
)
_PARTIAL_TIMEOUT_MARKERS = (
    "idle timeout",
    "timeout ",
)


def _clip_text(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED {len(text) - limit} chars]"


def _log_error(adapter: str, run_id: str, rc: int, diagnostic: str, **extra) -> None:
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
    if any(s in low for s in ("not on path", "not found", "missing")):
        return "missing"
    return "error"


def _partial_text_for_watchdog_error(stdout: str, error: str | None) -> tuple[str, list[str], int, dict]:
    """Return parsed partial assistant text for watchdog timeouts only.

    Fatal startup/auth failures are not useful partial answers; timeout output is.
    The dispatcher will display this text but must not route or execute it.
    """
    low = (error or "").lower()
    if not any(marker in low for marker in _PARTIAL_TIMEOUT_MARKERS):
        return "", [], 0, {}
    text, tools, fetch_count, metrics = _extract_codex_jsonl(stdout or "")
    if not text:
        text = (stdout or "").strip()
    return text.strip(), tools, fetch_count, metrics


def _resolve_codex_cmd() -> str | None:
    candidates = [
        os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.ps1"),
        shutil.which("codex.cmd"),
        shutil.which("codex"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort process-tree kill for Codex CLI wrappers on Windows."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass
        try:
            proc.wait(timeout=1)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _communicate_with_startup_watchdog(
    proc: subprocess.Popen,
    input_text: str,
    timeout: float,
    startup_timeout: float,
    idle_timeout: float | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
) -> tuple[str, str, str | None]:
    """Stream subprocess output and fail fast on silent/fatal Codex startup.

    on_stdout_line: optional callback called for each stdout line (stripped).
    """
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    first_output = threading.Event()
    last_output_at = time.time()
    io_lock = threading.Lock()

    def _read(stream, parts: list[str], on_line: Callable[[str], None] | None = None) -> None:
        nonlocal last_output_at
        try:
            while True:
                chunk = stream.readline()
                if not chunk:
                    break
                with io_lock:
                    parts.append(chunk)
                    last_output_at = time.time()
                    first_output.set()
                if on_line:
                    try:
                        on_line(chunk.rstrip("\n"))
                    except Exception:
                        pass
        except Exception:
            pass

    def _write() -> None:
        try:
            if proc.stdin:
                proc.stdin.write(input_text)
                proc.stdin.close()
        except Exception:
            pass

    threads = [
        threading.Thread(target=_read, args=(proc.stdout, stdout_parts, on_stdout_line), daemon=True),
        threading.Thread(target=_read, args=(proc.stderr, stderr_parts), daemon=True),
        threading.Thread(target=_write, daemon=True),
    ]
    for t in threads:
        t.start()

    started = time.time()
    startup_deadline = started + min(max(startup_timeout, 0.1), timeout)
    timeout_deadline = started + timeout
    idle_timeout = idle_timeout if idle_timeout and idle_timeout > 0 else None
    failure: str | None = None

    while proc.poll() is None:
        now = time.time()
        with io_lock:
            output_seen = first_output.is_set()
            stderr_snapshot = "".join(stderr_parts)
            stdout_snapshot = "".join(stdout_parts)
            idle_for = now - last_output_at
        combined_tail = (stderr_snapshot + "\n" + stdout_snapshot)[-4000:]
        for pattern in _FATAL_STDERR_PATTERNS:
            if pattern.lower() in combined_tail.lower():
                failure = f"fatal Codex CLI output: {pattern}"
                _kill_process_tree(proc)
                break
        if failure:
            break
        if not output_seen and now >= startup_deadline:
            failure = f"startup timeout {startup_timeout:g}s without Codex output"
            _kill_process_tree(proc)
            break
        if output_seen and idle_timeout and idle_for >= idle_timeout:
            failure = f"idle timeout {idle_timeout:g}s without Codex output"
            _kill_process_tree(proc)
            break
        if now >= timeout_deadline:
            failure = f"timeout {int(timeout)}s"
            _kill_process_tree(proc)
            break
        time.sleep(0.1)

    if failure:
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        for t in threads:
            t.join(timeout=0.2)
        return "".join(stdout_parts), "".join(stderr_parts), failure

    try:
        proc.wait(timeout=10)
    except Exception:
        _kill_process_tree(proc)

    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream:
                stream.close()
        except Exception:
            pass

    for t in threads:
        t.join(timeout=1)

    return "".join(stdout_parts), "".join(stderr_parts), failure


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("output_text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks).strip()


def _extract_codex_jsonl(stdout: str) -> tuple[str, list[str], int, dict]:
    """Parse `codex exec --json` output.

    Codex CLI event schemas move over time, so this parser accepts the common
    shapes seen in OpenAI/Codex traces: top-level events, nested `payload`, and
    nested `item`. Unknown JSON events are ignored instead of breaking the call.
    """
    events = []
    text_candidates: list[str] = []
    tools: list[str] = []
    call_tools: dict[str, str] = {}
    successful_fetch_count = 0

    def add_tool(name: str | None, call_id: str | None = None) -> None:
        if not name:
            return
        tools.append(name)
        if call_id:
            call_tools[call_id] = name

    def visit(obj) -> None:
        nonlocal successful_fetch_count
        if isinstance(obj, list):
            for child in obj:
                visit(child)
            return
        if not isinstance(obj, dict):
            return

        typ = str(obj.get("type") or "")
        role = str(obj.get("role") or "")
        name = obj.get("name") or obj.get("tool_name")
        call_id = obj.get("call_id") or obj.get("id")

        if typ in {"function_call", "tool_call", "tool_use"}:
            add_tool(str(name) if name else None, str(call_id) if call_id else None)
        elif typ in {"command_execution", "exec_command"}:
            add_tool("shell_command", str(call_id) if call_id else None)

        if typ in {"function_call_output", "tool_call_output", "tool_result"}:
            tool = call_tools.get(str(obj.get("call_id") or ""))
            output = obj.get("output") or obj.get("content")
            if tool and tool.lower() in _WEB_TOOL_NAMES and output and "error" not in str(output).lower():
                successful_fetch_count += 1

        if typ in {"agent_message", "assistant_message"}:
            # codex ≥0.128 uses "text"; older builds used "message"
            candidate = obj.get("text") or obj.get("message")
            if isinstance(candidate, str) and candidate:
                text_candidates.append(candidate.strip())
        if role == "assistant":
            text = _content_text(obj.get("content"))
            if text:
                text_candidates.append(text)
        if typ == "message" and isinstance(obj.get("content"), str):
            text_candidates.append(obj["content"].strip())

        # Recurse into the shapes used by Codex and by stored Codex sessions.
        for key in ("payload", "item", "message", "content", "items", "output"):
            child = obj.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        visit(event)

    seen_tools: list[str] = []
    for tool in tools:
        if tool not in seen_tools:
            seen_tools.append(tool)

    text = next((t for t in reversed(text_candidates) if t), "")
    metrics = {"json_events": len(events), "parsed_tools": seen_tools}
    return text, seen_tools, successful_fetch_count, metrics


class CodexAdapter(Adapter):
    """Adapter for Diderot — uses Codex CLI via subprocess.

    Model: gpt-5.x (configurable). Auth: ChatGPT subscription.
    """

    DEFAULT_MODEL = "gpt-5.5"

    def __init__(self, role: str = "dro", name: str = "Diderot", workspace: Path | None = None,
                 model: str | None = None, codex_cmd: str | None = None,
                 startup_timeout: float | None = None):
        super().__init__(role, name, workspace or Path.cwd())
        self.model = model or self.DEFAULT_MODEL
        self.codex_cmd = codex_cmd or _resolve_codex_cmd()
        if not self.codex_cmd:
            raise FileNotFoundError("codex CLI not found in %APPDATA%\\npm or PATH")
        raw_startup_timeout = os.environ.get("CABINET_CODEX_STARTUP_TIMEOUT")
        raw_idle_timeout = os.environ.get("CABINET_CODEX_IDLE_TIMEOUT")
        if startup_timeout is None and raw_startup_timeout:
            try:
                startup_timeout = float(raw_startup_timeout)
            except ValueError:
                startup_timeout = None
        self.startup_timeout = startup_timeout or _DEFAULT_STARTUP_TIMEOUT
        try:
            self.idle_timeout = float(raw_idle_timeout) if raw_idle_timeout else _DEFAULT_IDLE_TIMEOUT
        except ValueError:
            self.idle_timeout = _DEFAULT_IDLE_TIMEOUT
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._cancelled = False
        self._running_pid: int | None = None
        self._running_started: float | None = None

    def cancel(self) -> None:
        """Terminate the running codex subprocess, if any."""
        self._cancelled = True
        with self._proc_lock:
            if self._proc:
                _kill_process_tree(self._proc)

    def runtime_health(self) -> tuple[bool, str]:
        with self._proc_lock:
            proc = self._proc
            started = self._running_started
            pid = self._running_pid
        if proc and proc.poll() is None:
            elapsed = int(time.time() - started) if started else 0
            if not self.runtime_ok and self.runtime_error:
                return False, self.runtime_error
            return True, f"running pid={pid} elapsed={elapsed}s"
        return super().runtime_health()

    MAX_HISTORY_MSGS = 12   # was 20 — trimmed to reduce tokens and latency
    FAST_HISTORY_MSGS = 8
    _SKIP_TYPES = {"system", "approval", "error"}

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
        """Same trimming strategy as ClaudeCodeAdapter: last 20 real messages
        + all context blocks. Cuts prompt from 10K to ~3K tokens."""
        parts = [system_prompt]
        history_limit = self._history_limit_for(user_message)
        if history:
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
                    f"[{omitted} older messages omitted — use tools if you need them]"
                )
            for item in trimmed:
                ts = item.get("timestamp", "")
                role = item.get("role", "?")
                text = _clip_text(item.get("text", ""), _MAX_HISTORY_ITEM_CHARS)
                attachments = item.get("attachments") or []
                if attachments:
                    lines = ["", "Attachments:"]
                    for att in attachments:
                        lines.append(f"- {att.get('name')}: {att.get('path')}")
                    text = (text or "").rstrip() + "\n" + "\n".join(lines)
                hist_lines.append(f"[{ts}] {role}: {text}")
            for item in context_items:
                hist_lines.append(_clip_text(item.get("text", ""), _MAX_CONTEXT_ITEM_CHARS))
            parts.append("\n=== HISTORY ===\n" + "\n\n".join(hist_lines))
        parts.append("\n=== MESSAGE ===\n" + user_message + "\n\nReply.")
        return "\n".join(parts)

    def call(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        timeout: int = 3600,
        allow_write_tools: bool = False,
        thread_id: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> CallResult:
        run_id = str(uuid.uuid4())
        prompt = self._build_prompt(system_prompt, history, user_message)
        cmd = [self.codex_cmd, "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox"]
        cmd += ["-m", self.model, "-"]
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
                self._running_pid = proc.pid
                self._running_started = time.time()
            try:
                draft = _CodexDraftStreamer(on_progress, self.role) if on_progress else None
                _on_line = (
                    (lambda ln: _emit_codex_line_progress(ln, on_progress, self.role, draft))
                    if on_progress else None
                )
                # idle_timeout must not be shorter than the overall timeout:
                # if we're willing to wait `timeout` seconds total, a long
                # silent stretch (e.g. Codex running a shell command) should
                # not trigger the idle watchdog before the outer deadline does.
                # The startup_timeout (60s) still catches processes that never
                # produce a single line of output.
                effective_idle = max(self.idle_timeout, float(timeout))
                stdout, stderr, watchdog_error = _communicate_with_startup_watchdog(
                    proc,
                    prompt,
                    timeout=timeout,
                    startup_timeout=self.startup_timeout,
                    idle_timeout=effective_idle,
                    on_stdout_line=_on_line,
                )
                if watchdog_error:
                    if self._cancelled:
                        break
                    partial_text, executed_tools, successful_fetch_count, parse_metrics = (
                        _partial_text_for_watchdog_error(stdout, watchdog_error)
                    )
                    _log_error(
                        "codex_cli",
                        run_id,
                        -1,
                        watchdog_error,
                        prompt_len=len(prompt),
                        allow_write=allow_write_tools,
                        partial_text=_clip_text(partial_text, 4000) if partial_text else "",
                    )
                    self.mark_runtime_error(f"error: {watchdog_error}")
                    return CallResult(
                        text=partial_text,
                        executed_tools=executed_tools,
                        successful_fetch_count=successful_fetch_count,
                        run_id=run_id, elapsed=time.time() - t0,
                        error=watchdog_error,
                        metrics={
                            "attempt": attempt + 1,
                            "partial_timeout": bool(partial_text),
                            **parse_metrics,
                        },
                    )
            except FileNotFoundError:
                self.mark_runtime_error("missing: codex CLI not on PATH")
                return CallResult(
                    text="", executed_tools=[], successful_fetch_count=0,
                    run_id=run_id, elapsed=time.time() - t0,
                    error="codex CLI not on PATH", metrics={},
                )
            except Exception as e:
                if self._cancelled:
                    break
                self.mark_runtime_error(f"error: call failed: {e!r}")
                return CallResult(
                    text="", executed_tools=[], successful_fetch_count=0,
                    run_id=run_id, elapsed=time.time() - t0,
                    error=f"call failed: {e!r}", metrics={},
                )
            finally:
                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None
                        self._running_pid = None
                        self._running_started = None

            if self._cancelled:
                break

            elapsed = time.time() - t0
            rc = proc.returncode
            if rc != 0:
                diagnostic = (stderr or stdout or "").strip()
                snippet = diagnostic[-1500:] if len(diagnostic) > 1500 else diagnostic
                _log_error("codex_cli", run_id, rc, diagnostic,
                           prompt_len=len(prompt), allow_write=allow_write_tools)
                transient = any(m in snippet.lower() for m in
                                ("econnreset", "etimedout", "socket hang up",
                                 "fetch failed", "network", "api error: terminated"))
                if transient and attempt < 2:
                    time.sleep(3)
                    continue
                err = f"Codex CLI rc={rc}: {snippet}"
                self.mark_runtime_error(f"{_runtime_error_label(err)}: {snippet}")
                return CallResult(
                    text="", executed_tools=[], successful_fetch_count=0,
                    run_id=run_id, elapsed=elapsed,
                    error=err, metrics={},
                )
            raw_stdout = stdout or ""
            text, executed_tools, successful_fetch_count, parse_metrics = _extract_codex_jsonl(raw_stdout)
            if not text:
                text = raw_stdout.strip()
            if text:
                self.mark_runtime_ok()
            else:
                self.mark_runtime_error("error: empty response")
            return CallResult(
                text=text or "[ERROR] empty response",
                executed_tools=executed_tools,
                successful_fetch_count=successful_fetch_count,
                run_id=run_id,
                elapsed=elapsed,
                error=None if text else "empty response",
                metrics={"attempt": attempt + 1, **parse_metrics},
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
        if not self.codex_cmd or not os.path.exists(self.codex_cmd):
            return False, "codex CLI missing"
        try:
            r = subprocess.run(
                [self.codex_cmd, "--version"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8",
            )
            version = (r.stdout or r.stderr or "").strip().splitlines()[0][:80]
            if r.returncode == 0:
                return True, f"ok model={self.model} cli={version}"
            return False, f"--version rc={r.returncode}: {version}"
        except Exception as e:
            return False, f"--version failed: {e!r}"
