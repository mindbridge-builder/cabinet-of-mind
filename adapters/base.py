"""Abstract Adapter interface for Cabinet of Mind.

Every adapter wraps a specific provider (Claude Code CLI, Codex CLI, Ollama)
and exposes a uniform call() method. The dispatcher does not need to know
which provider is behind which @-role.

Sync subprocess design, battle-tested in earlier builds.
Each call() runs a fresh subprocess with the given prompt as stdin. Returns
the agent's text response, an executed-tool list, and metrics.
"""
from __future__ import annotations

import abc
import dataclasses
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


@dataclasses.dataclass
class CallResult:
    """Result of one full agent call (one model_response cycle).

    Attributes:
        text: final assistant text. Empty string on hard error.
        executed_tools: list of tool names actually called in this run.
        successful_fetch_count: number of web_fetch tools that returned data
            (NOT counting failed/errored fetches).
        run_id: correlation ID; matches run_id in tool_runs.jsonl.
        elapsed: wall-clock seconds for this call.
        error: short error string if call failed, else None.
        metrics: provider-specific metrics dict (prompt_eval_count, etc.).
    """
    text: str
    executed_tools: list[str]
    successful_fetch_count: int
    run_id: str
    elapsed: float
    error: str | None
    metrics: dict


class Adapter(abc.ABC):
    """Abstract base class for all cabinet adapters.

    Subclasses: ClaudeCodeAdapter, CodexAdapter, OllamaAdapter.
    """

    role: str  # "hux" / "dro" / "gol"
    name: str  # "Huxley" / "Diderot" / "Golem"

    def __init__(self, role: str, name: str, workspace: Path):
        self.role = role
        self.name = name
        self.workspace = workspace
        self.runtime_ok = True
        self.runtime_error: str | None = None
        self.runtime_error_time: str | None = None

    def mark_runtime_ok(self) -> None:
        self.runtime_ok = True
        self.runtime_error = None
        self.runtime_error_time = None

    def mark_runtime_error(self, error: str) -> None:
        self.runtime_ok = False
        self.runtime_error = (error or "runtime error").strip()
        self.runtime_error_time = datetime.now().isoformat(timespec="seconds")

    def runtime_health(self) -> tuple[bool, str]:
        if self.runtime_ok:
            return True, "ok"
        return False, self.runtime_error or "runtime error"

    @abc.abstractmethod
    def call(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        timeout: int = 300,
        allow_write_tools: bool = False,
        thread_id: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> CallResult:
        """Run one agent call. Returns CallResult.

        on_progress: optional callback called as the agent works.
        Each call receives a dict with at minimum {"type": "agent_tool_use",
        "agent": role, "tool": name, "label": human_readable_label}.
        """
        ...

    def cancel(self) -> None:
        """Cancel the in-flight call, if any.

        Terminates the subprocess or sets a stop flag. No-op by default.
        Subclasses override for real cancellation.
        """

    @abc.abstractmethod
    def healthcheck(self) -> tuple[bool, str]:
        """Quick check: provider is reachable.

        Returns: (is_alive, status_message).
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} role={self.role} name={self.name}>"
