"""Adapter for Ollama (Golem / @gol / hands).

Direct POST to /api/chat with native tool calling.

Tools exposed: read_file, list_files, search_text, file_info, and write tools
inside allowed roots. Critical actions are handled by the chat-level Y/N
protocol before the agent proceeds.

Strips `thinking` field from history per Gemma 4 / Qwen 3.6 spec to avoid
multi-turn drift.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import Adapter, CallResult
from core.hands_delegation import GolemDelegationError, parse_known_files_patch_delegation
from core.hands_full_file_patch import run_known_files_patch, run_search_replace_patch
from core import routing as _routing
from core.validators import build_observed_report, format_observed_report, strip_self_report_facts
from core.work_templates import WorkTemplateError, compile_work_template

_TAG_RE = re.compile(r"^@(hux|dro|boss)\b", re.IGNORECASE)
_QWEN_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _tool_label(name: str, inp: dict | None) -> str:
    """Human-readable label for a Golem tool call."""
    inp = inp or {}
    if name == "read_file":
        fp = inp.get("path") or ""
        return f"📖 {Path(fp).name}" if fp else "📖 reading a file"
    if name in ("write_file", "append_file", "replace_text"):
        fp = inp.get("path") or ""
        return f"✏️ {Path(fp).name}" if fp else "✏️ writing a file"
    if name == "bash_run":
        cmd = (inp.get("command") or "")[:60]
        return f"▶ {cmd}" if cmd else "▶ bash"
    if name == "search_text":
        q = inp.get("query") or ""
        return f"🔍 {str(q)[:40]}" if q else "🔍 searching"
    if name == "list_files":
        p = inp.get("path") or ""
        return f"📂 {Path(p).name or p}" if p else "📂 ls"
    if name == "git_commit":
        msg = inp.get("message") or ""
        return f"🔖 {str(msg)[:40]}" if msg else "🔖 git commit"
    return f"⚙ {name}"


OLLAMA_URL = "http://127.0.0.1:11434"


def _strip_thinking(message: dict) -> dict:
    """Remove thinking field per Qwen/Gemma spec."""
    if "thinking" not in message:
        return message
    return {k: v for k, v in message.items() if k != "thinking"}


def _parse_xml_tool_calls(content: str) -> list[dict]:
    """Parse Ollama's XML-like fallback tool-call format from message content."""
    calls: list[dict] = []
    for match in re.finditer(r"<function=([A-Za-z_][\w]*)>\s*(.*?)\s*</function>", content or "", re.DOTALL):
        name = match.group(1)
        body = match.group(2)
        args: dict[str, str] = {}
        for param in re.finditer(r"<parameter=([A-Za-z_][\w]*)>\s*(.*?)\s*</parameter>", body, re.DOTALL):
            args[param.group(1)] = param.group(2).strip()
        calls.append({"function": {"name": name, "arguments": args}})
    return calls


def _tool_call_from_obj(obj: Any) -> dict | None:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):
        func = obj["function"]
        name = func.get("name")
        args = func.get("arguments") or {}
    else:
        name = obj.get("name")
        args = obj.get("arguments") or {}
    if isinstance(name, str) and isinstance(args, (dict, str)):
        return {"function": {"name": name, "arguments": args}}
    return None


def _parse_qwen_tool_call_blocks(content: str) -> list[dict]:
    """Parse Qwen/Ollama <tool_call>{...}</tool_call> blocks."""
    calls: list[dict] = []
    decoder = json.JSONDecoder()
    for block in _QWEN_TOOL_CALL_RE.findall(content or ""):
        text = block.strip()
        pos = 0
        while pos < len(text):
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                break
            try:
                obj, end = decoder.raw_decode(text[pos:])
            except json.JSONDecodeError:
                break
            call = _tool_call_from_obj(obj)
            if call:
                calls.append(call)
            pos += end
    return calls


def _parse_json_tool_calls(content: str) -> list[dict]:
    """Parse models that print a single OpenAI-style tool JSON in content."""
    text = content or ""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        call = _tool_call_from_obj(obj)
        if call:
            return [call]
    return []


def _read_text_safe(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            head = text[: max_chars // 2]
            tail = text[-(max_chars - len(head)):]
            return f"{head}\n\n[TRUNCATED {len(text) - max_chars} chars]\n\n{tail}"
        return text
    except Exception as e:
        return f"[error reading file: {e!r}]"


def _read_text_limited(path: Path, max_chars: int) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"error reading file: {e!r}"}
    original_chars = len(text)
    if original_chars <= max_chars:
        return {
            "content": text,
            "truncated": False,
            "original_chars": original_chars,
            "returned_chars": original_chars,
        }
    head = text[: max_chars // 2]
    tail = text[-(max_chars - len(head)):]
    marker = f"\n\n[TRUNCATED {original_chars - max_chars} chars]\n\n"
    content = head + marker + tail
    return {
        "content": content,
        "truncated": True,
        "original_chars": original_chars,
        "returned_chars": len(content),
    }


def _sanitize_tool_journal_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sanitize_tool_journal_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_tool_journal_value(v) for v in value[:50]]
    if isinstance(value, str):
        return value[:4000]
    return value


def _with_observed_report(final: str, tool_journal: list[dict], context: dict | None = None) -> tuple[str, dict]:
    report = build_observed_report(tool_journal, context=context)
    route_data = _routing.parse_route_data(final)
    visible = _routing.route_tail_visible_text(final).strip() or final.strip()
    visible = strip_self_report_facts(visible)
    observed = format_observed_report(report)
    parts = [part for part in (visible, observed) if part]
    if "route" in route_data:
        return "\n\n".join(parts) + "\n" + json.dumps(route_data, ensure_ascii=False), report
    return "\n\n".join(parts), report


class OllamaAdapter(Adapter):
    """Adapter for Golem — the hands. Any local coder model served by Ollama.

    The model is whatever you can run: set agents.gol.model in
    CABINET_REALITY.json, or the CABINET_HANDS_MODEL env var, or build the
    golem:hands alias from models/golem.hands.Modelfile.

    Sages dispatch concrete hands instructions; write tools are role-enabled.
    """

    DEFAULT_MODEL = "golem:hands"
    DEFAULT_NUM_CTX = 16384      # fits a 24 GB consumer GPU; tune to yours
    KNOWN_FILES_PATCH_NUM_CTX = 32768
    KNOWN_FILES_PATCH_TEMPERATURE = 0.4
    DEFAULT_KEEP_ALIVE = "30m"
    CONTEXT_PRESSURE_RATIO = 0.85
    MAX_EMPTY_RETRIES_AFTER_TOOLS = 3
    MAX_COMMIT_RETRIES_AFTER_WRITE = 3
    MAX_DETAILED_TOOL_RESULTS = 3
    MAX_DIRECT_BATCH_READS = 5
    MAX_DIRECT_BATCH_APPENDS = 3
    MAX_DIRECT_BATCH_WRITES = 3
    MAX_DIRECT_BATCH_TOOLS = 8
    MAX_DIRECT_BATCH_SPEC_FILES = 5
    COMPACT_KEEP_CONTENT_CHARS = 1000
    GIT_TIMEOUT = 300
    BASH_TIMEOUT_DEFAULT = 300
    BASH_TIMEOUT_MAX = 1800
    DEFAULT_OPTIONS = {
        "num_ctx": DEFAULT_NUM_CTX,
        "temperature": 0,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0,
        "presence_penalty": 0,
        "repeat_penalty": 1.1,
    }
    STEP_TIMEOUT = 300   # max seconds per single Ollama HTTP request
    MAX_OUTPUT_CHARS = 6000
    MAX_TOOL_RESULT_CONTENT_CHARS = 6000
    MAX_HISTORY_MSGS = 20        # trim history like ClaudeCodeAdapter
    FAST_HISTORY_MSGS = 8
    _SKIP_TYPES = {"system", "approval", "error"}

    def __init__(self, role: str = "gol", name: str = "Golem", workspace: Path | None = None,
                 model: str | None = None, base_url: str | None = None,
                 allowed_roots: list[Path] | None = None,
                 work_runtime: Any | None = None):
        super().__init__(role, name, workspace or Path.cwd())
        self.model = model or os.environ.get("CABINET_HANDS_MODEL") or self.DEFAULT_MODEL
        self.base_url = base_url or OLLAMA_URL
        self.allowed_roots = allowed_roots or [self.workspace]
        self.work_runtime = work_runtime
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Stop the Ollama call loop after the current HTTP step finishes."""
        self._cancelled.set()

    # ── Path safety ──────────────────────────────────────────────────────
    def _is_allowed_path(self, path: Path) -> bool:
        try:
            p = path.resolve()
        except Exception:
            return False
        for root in self.allowed_roots:
            try:
                root_resolved = Path(root).resolve()
                p.relative_to(root_resolved)
                return True
            except ValueError:
                continue
        return False

    def _is_protected_path(self, path: Path) -> bool:
        """Golem must never WRITE/DELETE its own configuration — model profile,
        prompts, reality/routing contracts. Reads stay allowed. Closes the
        file-tool self-modification path (Golem edited its own Modelfile on
        2026-07-01); bash_run mutations are tracked separately.
        """
        try:
            p = path.resolve()
        except Exception:
            return True  # unresolvable → refuse, fail safe
        name = p.name.lower()
        if name.endswith(".modelfile") or name in {
            "cabinet_reality.json",
            "cabinet_reality.local.json",
            "routing_contract.json",
            "gol_hands.md",
            "cabinet_bootstrap.md",
        }:
            return True
        ws = self.workspace.resolve()
        for sub in ("models", "prompts"):
            try:
                p.relative_to(ws / sub)
                return True
            except ValueError:
                continue
        return False

    def _project_manifest(self) -> dict:
        manifest = self.workspace / "project.json"
        if not manifest.exists():
            return {}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _project_action_files(self) -> list[Path]:
        raw_actions_dir = self._project_manifest().get("actions_dir")
        if not raw_actions_dir:
            return []
        actions_dir = Path(str(raw_actions_dir))
        if not actions_dir.is_absolute():
            actions_dir = self.workspace / actions_dir
        if not self._is_allowed_path(actions_dir) or not actions_dir.exists():
            return []
        return sorted(actions_dir.glob("*.json"))

    def _load_project_actions(self) -> dict[str, dict]:
        actions: dict[str, dict] = {}
        for path in self._project_action_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                action_id = str(entry.get("id") or "").strip()
                if action_id:
                    action = dict(entry)
                    action["_manifest"] = str(path)
                    actions[action_id] = action
        return actions

    def _expand_action_value(self, value: Any, variables: dict[str, str]) -> Any:
        if isinstance(value, str):
            expanded = value
            for key, replacement in variables.items():
                expanded = expanded.replace("${" + key + "}", replacement)
            return expanded
        if isinstance(value, list):
            return [self._expand_action_value(item, variables) for item in value]
        if isinstance(value, dict):
            return {key: self._expand_action_value(item, variables) for key, item in value.items()}
        return value

    @staticmethod
    def _action_items(action: dict) -> list[dict]:
        raw_items = action.get("items")
        if isinstance(raw_items, list) and raw_items:
            return [item for item in raw_items if isinstance(item, dict)]
        if isinstance(action.get("command"), list) or action.get("template"):
            return [action]
        return []

    def _resolve_action_cwd(self, raw_cwd: str | None, variables: dict[str, str]) -> Path:
        expanded = self._expand_action_value(raw_cwd or "${project_root}", variables)
        cwd = Path(str(expanded))
        if not cwd.is_absolute():
            cwd = self.workspace / cwd
        return cwd

    def _resolve_start_work_action(self, action_id: str) -> dict:
        action = self._load_project_actions().get(action_id)
        if not action:
            return {"error": f"unknown project action: {action_id}"}
        items = self._action_items(action)
        if not items:
            return {"error": f"project action has no runnable command: {action_id}"}
        if len(items) > 1:
            return {"error": f"project action has multiple items; start by action runner, not start_work: {action_id}"}
        variables = {
            "cabinet_root": str(self.workspace),
            "project_root": str(self.workspace),
            "python": sys.executable,
        }
        try:
            expanded = self._compile_action_item(items[0], variables)
        except WorkTemplateError as exc:
            return {"error": f"invalid template for project action {action_id}: {exc}"}
        command = expanded.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            return {"error": f"invalid command for project action: {action_id}"}
        cwd = self._resolve_action_cwd(expanded.get("cwd"), variables)
        if not self._is_allowed_path(cwd):
            return {"error": f"action cwd outside allowed roots: {cwd}"}
        return {
            "action_id": action_id,
            "title": str(expanded.get("title") or action.get("title") or action_id),
            "command": command,
            "cwd": cwd,
            "env": expanded.get("env") if isinstance(expanded.get("env"), dict) else None,
            "created_from": f"project_action:{action_id}",
            "handoff": self._action_handoff(action_id, expanded, variables),
        }

    def _compile_action_item(self, item: dict, variables: dict[str, str]) -> dict:
        if item.get("template"):
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            compiled = compile_work_template(str(item.get("template")), params, variables)
            merged = {**compiled, **{k: v for k, v in item.items() if k not in {"template", "params"}}}
            merged.setdefault("template_id", compiled["template_id"])
            if "handoff" in compiled and "handoff" not in merged:
                merged["handoff"] = compiled["handoff"]
            return merged
        expanded = self._expand_action_value(item, variables)
        expanded.setdefault("template_id", "manual_command")
        return expanded

    def _action_handoff(self, action_id: str, item: dict, variables: dict[str, str]) -> dict | None:
        raw = item.get("handoff")
        if not isinstance(raw, dict):
            return None
        handoff = self._expand_action_value(raw, variables)
        if not isinstance(handoff, dict):
            return None
        handoff.setdefault("action_id", action_id)
        if item.get("template_id"):
            handoff.setdefault("template_id", item.get("template_id"))
        return handoff

    def _validate_batch_work_command(self, command: list[str], cwd_path: Path) -> str | None:
        if not command:
            return "batch WorkRuntime command is empty"
        executable = Path(command[0]).name.lower()
        if executable.startswith("python") and len(command) >= 2:
            if command[1] == "-m":
                module = command[2] if len(command) >= 3 else ""
                if not module:
                    return "batch WorkRuntime python -m command is missing a module"
                module_path = cwd_path / Path(*module.split("."))
                candidates = [
                    module_path.with_suffix(".py"),
                    module_path / "__main__.py",
                    module_path / "__init__.py",
                ]
                if any(path.exists() and self._is_allowed_path(path) for path in candidates):
                    return None
                return (
                    "batch WorkRuntime command must use a manifest action_id or an existing worker module; "
                    f"module not found in workspace: {module}"
                )
            script = cwd_path / command[1]
            if script.exists() and self._is_allowed_path(script):
                return None
            return (
                "batch WorkRuntime command must use a manifest action_id or an existing worker script; "
                f"script not found in workspace: {command[1]}"
            )
        first = Path(command[0])
        if first.is_absolute() or "\\" in command[0] or "/" in command[0]:
            path = first if first.is_absolute() else cwd_path / first
            if path.exists() and self._is_allowed_path(path):
                return None
        return (
            "batch WorkRuntime command must be manifest-backed or reference an existing worker file/module; "
            "refusing guessed command"
        )

    def _resolve_tool_path(self, raw_path, base: Path | None = None, strip_root: bool = False) -> Path:
        raw = str(raw_path or "")
        if strip_root and raw and not Path(raw).drive:
            raw = raw.lstrip("/\\")
        path = Path(raw)
        if not path.is_absolute() and not path.drive:
            path = (base or self.workspace) / raw
        return path.resolve()

    # ── Tool implementations ─────────────────────────────────────────────
    def _tool_read_file(self, args: dict) -> dict:
        path_str = args.get("path") or ""
        path = self._resolve_tool_path(path_str)
        if not self._is_allowed_path(path):
            return {"type": "read_file", "error": f"path outside allowed roots: {path_str}"}
        if not path.exists():
            return {"type": "read_file", "exists": False, "path": str(path)}
        if not path.is_file():
            return {"type": "read_file", "error": "not a file", "path": str(path)}
        max_chars = int(args.get("max_chars") or self.MAX_OUTPUT_CHARS)
        read = _read_text_limited(path, max_chars)
        if read.get("error"):
            return {"type": "read_file", "error": read["error"], "path": str(path)}
        return {
            "type": "read_file",
            "path": str(path),
            "size": path.stat().st_size,
            "content": read["content"],
            "truncated": read["truncated"],
            "original_chars": read["original_chars"],
            "returned_chars": read["returned_chars"],
        }

    def _tool_list_files(self, args: dict) -> dict:
        path_str = args.get("path") or str(self.workspace)
        path = self._resolve_tool_path(path_str)
        if not self._is_allowed_path(path):
            return {"type": "list_files", "error": "outside allowed roots"}
        if not path.exists() or not path.is_dir():
            return {"type": "list_files", "error": "not a directory"}
        recursive = bool(args.get("recursive", False))
        glob = args.get("glob") or "*"
        items = []
        iterator = path.rglob(glob) if recursive else path.glob(glob)
        for entry in iterator:
            items.append({
                "path": str(entry),
                "kind": "dir" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
            })
            if len(items) >= 200:
                break
        return {"type": "list_files", "path": str(path), "count": len(items), "files": items}

    def _tool_search_text(self, args: dict) -> dict:
        query = (args.get("query") or "").strip()
        path_str = args.get("path") or str(self.workspace)
        path = self._resolve_tool_path(path_str)
        if not query:
            return {"type": "search_text", "error": "empty query"}
        if not self._is_allowed_path(path):
            return {"type": "search_text", "error": "outside allowed roots"}
        glob = args.get("glob") or "*"
        max_results = int(args.get("max_results") or 50)
        results = []
        candidates = [path] if path.is_file() else path.rglob(glob)
        lower = query.lower()
        for f in candidates:
            if not f.is_file():
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for ln, line in enumerate(txt.splitlines(), start=1):
                if lower in line.lower():
                    results.append({"path": str(f), "line": ln, "text": line.strip()[:200]})
                    if len(results) >= max_results:
                        return {"type": "search_text", "results": results, "truncated": True}
        return {"type": "search_text", "results": results, "truncated": False}

    def _tool_file_info(self, args: dict) -> dict:
        """File info: exists FIRST, then secret check (closes Codex 2026-05-06 finding)."""
        path_str = args.get("path") or ""
        path = self._resolve_tool_path(path_str)
        if not self._is_allowed_path(path):
            return {"type": "file_info", "error": "outside allowed roots"}
        if not path.exists():
            return {"type": "file_info", "exists": False, "path": str(path)}
        return {
            "type": "file_info",
            "exists": True,
            "path": str(path),
            "kind": "dir" if path.is_dir() else "file",
            "size": path.stat().st_size if path.is_file() else None,
        }

    def _tool_write_file(self, args: dict) -> dict:
        path_str = args.get("path") or ""
        content = args.get("content")
        path = self._resolve_tool_path(path_str)
        if content is None:
            return {"type": "write_file", "error": "missing content"}
        if not self._is_allowed_path(path):
            return {"type": "write_file", "error": "outside allowed roots"}
        if self._is_protected_path(path):
            return {"type": "write_file", "error": "protected self-config: Golem cannot modify its own model/prompt/contract files"}
        old_text = path.read_text(encoding="utf-8") if path.exists() else None
        new_text = str(content)
        changed = old_text != new_text
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
        result: dict = {
            "type": "write_file",
            "path": str(path),
            "bytes": path.stat().st_size,
            "changed": changed,
        }
        if not changed:
            result["warning"] = "content identical to existing file — no bytes were modified"
        return result

    def _tool_append_file(self, args: dict) -> dict:
        path_str = args.get("path") or ""
        content = args.get("content")
        path = self._resolve_tool_path(path_str)
        if content is None:
            return {"type": "append_file", "error": "missing content"}
        if not self._is_allowed_path(path):
            return {"type": "append_file", "error": "outside allowed roots"}
        if self._is_protected_path(path):
            return {"type": "append_file", "error": "protected self-config: Golem cannot modify its own model/prompt/contract files"}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(str(content))
        return {"type": "append_file", "path": str(path), "bytes": path.stat().st_size}

    def _tool_replace_text(self, args: dict) -> dict:
        path_str = args.get("path") or ""
        old = args.get("old")
        new = args.get("new")
        path = self._resolve_tool_path(path_str)
        if old is None or new is None:
            return {"type": "replace_text", "error": "missing old/new"}
        if not self._is_allowed_path(path):
            return {"type": "replace_text", "error": "outside allowed roots"}
        if self._is_protected_path(path):
            return {"type": "replace_text", "error": "protected self-config: Golem cannot modify its own model/prompt/contract files"}
        if not path.exists() or not path.is_file():
            return {"type": "replace_text", "error": "file does not exist", "path": str(path)}
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(str(old))
        if count == 0:
            return {"type": "replace_text", "path": str(path), "replaced": 0}
        text = text.replace(str(old), str(new), int(args.get("count") or count))
        path.write_text(text, encoding="utf-8")
        return {"type": "replace_text", "path": str(path), "replaced": count}

    def _tool_delete_path(self, args: dict) -> dict:
        path_str = args.get("path") or ""
        recursive = bool(args.get("recursive", False))
        path = self._resolve_tool_path(path_str)
        if not self._is_allowed_path(path):
            return {"type": "delete_path", "error": "outside allowed roots"}
        if self._is_protected_path(path):
            return {"type": "delete_path", "error": "protected self-config: Golem cannot modify its own model/prompt/contract files"}
        if not path.exists():
            return {"type": "delete_path", "exists": False, "path": str(path)}
        if path.is_file():
            path.unlink()
            return {"type": "delete_path", "path": str(path), "deleted": True}
        if path.is_dir():
            if not recursive:
                return {"type": "delete_path", "error": "directory requires recursive=true", "path": str(path)}
            return {
                "type": "delete_path",
                "error": "recursive directory deletion is blocked; ask Boss and use a human-controlled command",
                "path": str(path),
            }
        return {"type": "delete_path", "error": "unsupported path type", "path": str(path)}

    def _tool_git_commit(self, args: dict) -> dict:
        """Stage explicit files and commit. Defaults to workspace; accepts any allowed_root via `cwd`.

        Requires a non-empty `files` list — never stages everything with -A.
        Golem commits only the files she actually wrote, not workspace-wide state.
        """
        import subprocess
        message = (args.get("message") or "Golem: write task complete").strip() or "Golem: automated commit"
        files: list = args.get("files") or []
        if not files:
            return {
                "type": "git_commit",
                "error": "files list is required — provide the exact paths you modified. Never commit -A.",
            }
        cwd_str = args.get("cwd") or str(self.workspace)
        cwd_path = self._resolve_tool_path(cwd_str)
        if not self._is_allowed_path(cwd_path):
            return {"type": "git_commit", "error": f"cwd outside allowed roots: {cwd_str}"}
        # Validate each file path (resolved relative to cwd) is inside allowed roots.
        # Strip leading slashes/backslashes from non-drive paths: Golem often emits
        # "/docs/x.md"; on Windows that joins as C:\docs\x.md, which falls outside cwd.
        normalized_files: list[str] = []
        for f in files:
            f_str = str(f)
            p = self._resolve_tool_path(f_str, base=cwd_path, strip_root=True)
            if Path(f_str).is_absolute() or Path(f_str).drive:
                normalized_files.append(f_str)
            else:
                normalized_files.append(f_str.lstrip("/\\"))
            if not self._is_allowed_path(p):
                return {"type": "git_commit", "error": f"path outside allowed roots: {f}"}
        files = normalized_files
        safe_dir = str(cwd_path).replace("\\", "/")
        git_base = ["git", "-c", f"safe.directory={safe_dir}"]
        try:
            r_add = subprocess.run(
                git_base + ["add", "--"] + [str(f) for f in files],
                capture_output=True, text=True,
                cwd=str(cwd_path), timeout=self.GIT_TIMEOUT,
            )
            if r_add.returncode != 0:
                return {"type": "git_commit", "error": f"git add rc={r_add.returncode}: {r_add.stderr[:300]}"}
            r_commit = subprocess.run(
                git_base + ["commit", "-m", message],
                capture_output=True, text=True,
                cwd=str(cwd_path), timeout=self.GIT_TIMEOUT,
            )
            out = r_commit.stdout.strip()
            err = r_commit.stderr.strip()
            if r_commit.returncode != 0:
                if "nothing to commit" in out or "nothing to commit" in err:
                    return {"type": "git_commit", "success": True, "no_change": True, "output": "nothing to commit"}
                return {"type": "git_commit", "error": f"git commit rc={r_commit.returncode}: {err[:300]}"}
            r_hash = subprocess.run(
                git_base + ["rev-parse", "--short", "HEAD"],
                capture_output=True, text=True,
                cwd=str(cwd_path), timeout=self.GIT_TIMEOUT,
            )
            commit_hash = r_hash.stdout.strip() if r_hash.returncode == 0 else ""
            result = {"type": "git_commit", "success": True, "output": out[:300]}
            if commit_hash:
                result["commit"] = commit_hash
            return result
        except Exception as e:
            return {"type": "git_commit", "error": f"subprocess failed: {e!r}"}

    def _tool_bash_run(self, args: dict) -> dict:
        """Run a shell command and return stdout/stderr.

        cwd defaults to workspace. Any allowed_root can be used as cwd.
        timeout defaults to 300s, max 1800s.
        """
        import subprocess
        command = (args.get("command") or "").strip()
        if not command:
            return {"type": "bash_run", "error": "empty command"}
        cwd_str = args.get("cwd") or str(self.workspace)
        cwd_path = self._resolve_tool_path(cwd_str)
        if not self._is_allowed_path(cwd_path):
            return {"type": "bash_run", "error": f"cwd outside allowed roots: {cwd_str}"}
        timeout = min(int(args.get("timeout") or self.BASH_TIMEOUT_DEFAULT), self.BASH_TIMEOUT_MAX)
        if os.name == "nt":
            run_args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
            use_shell = False
        else:
            run_args = command
            use_shell = True
        try:
            r = subprocess.run(
                run_args, shell=use_shell, capture_output=True, text=True,
                cwd=str(cwd_path), timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            return {
                "type": "bash_run",
                "returncode": r.returncode,
                "stdout": r.stdout[:4000],
                "stderr": r.stderr[:2000],
            }
        except subprocess.TimeoutExpired:
            return {"type": "bash_run", "error": f"timeout after {timeout}s"}
        except Exception as e:
            return {"type": "bash_run", "error": str(e)}

    def _tool_start_work(self, args: dict) -> dict:
        """Start a long-running process through Cabinet work runtime."""
        if self.work_runtime is None:
            return {"type": "start_work", "error": "work runtime is not configured"}
        action_id = str(args.get("action_id") or "").strip()
        if action_id:
            resolved = self._resolve_start_work_action(action_id)
            if resolved.get("error"):
                return {"type": "start_work", "error": resolved["error"], "action_id": action_id}
            title = resolved["title"]
            command = resolved["command"]
            cwd_path = resolved["cwd"]
            env = resolved.get("env")
            created_from = resolved["created_from"]
            handoff = resolved.get("handoff")
        else:
            title = (args.get("title") or "Golem work").strip()
            command = args.get("command") or []
            cwd_str = args.get("cwd") or str(self.workspace)
            cwd_path = self._resolve_tool_path(cwd_str)
            if not self._is_allowed_path(cwd_path):
                return {"type": "start_work", "error": f"cwd outside allowed roots: {cwd_str}"}
            env = args.get("env") if isinstance(args.get("env"), dict) else None
            created_from = args.get("created_from") or "ollama_tool:start_work"
            handoff = args.get("handoff") if isinstance(args.get("handoff"), dict) else None
        if isinstance(command, str):
            return {"type": "start_work", "error": "command must be an array of strings, not a shell string"}
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            return {"type": "start_work", "error": "command must be a non-empty array of strings"}
        if args.get("batch_redirect") and not action_id:
            contract_error = self._validate_batch_work_command(command, cwd_path)
            if contract_error:
                return {
                    "type": "start_work",
                    "error": contract_error,
                    "blocked": True,
                    "workruntime_contract": "batch_worker_command",
                    "command": command,
                    "cwd": str(cwd_path),
                }
        try:
            meta = self.work_runtime.start_process(
                title=title,
                command=command,
                cwd=cwd_path,
                owner=self.role,
                executor="golem_work_runtime",
                created_from=created_from,
                env=env,
                handoff=handoff,
            )
        except Exception as e:
            return {"type": "start_work", "error": str(e)}
        return {
            "type": "start_work",
            "work_id": meta.get("work_id"),
            "status": meta.get("status"),
            "title": meta.get("title"),
            "cwd": meta.get("cwd"),
            "stdout": meta.get("stdout"),
            "stderr": meta.get("stderr"),
            "command": meta.get("command"),
            "action_id": action_id or None,
        }

    def _tool_work_status(self, args: dict) -> dict:
        if self.work_runtime is None:
            return {"type": "work_status", "error": "work runtime is not configured"}
        work_id = (args.get("work_id") or "").strip()
        if not work_id:
            return {"type": "work_status", "error": "work_id is required"}
        meta = self.work_runtime.store.get(work_id)
        if not meta:
            return {"type": "work_status", "exists": False, "work_id": work_id}
        return {"type": "work_status", "exists": True, "work": meta}

    def _tool_work_logs(self, args: dict) -> dict:
        if self.work_runtime is None:
            return {"type": "work_logs", "error": "work runtime is not configured"}
        work_id = (args.get("work_id") or "").strip()
        if not work_id:
            return {"type": "work_logs", "error": "work_id is required"}
        max_chars = int(args.get("max_chars") or 4000)
        meta = self.work_runtime.store.get(work_id)
        return {
            "type": "work_logs",
            "exists": bool(meta),
            "work_id": work_id,
            "status": (meta or {}).get("status"),
            "stdout": self.work_runtime.store.read_log_tail(work_id, "stdout", max_chars),
            "stderr": self.work_runtime.store.read_log_tail(work_id, "stderr", max_chars),
        }

    def _tool_cancel_work(self, args: dict) -> dict:
        if self.work_runtime is None:
            return {"type": "cancel_work", "error": "work runtime is not configured"}
        work_id = (args.get("work_id") or "").strip()
        if not work_id:
            return {"type": "cancel_work", "error": "work_id is required"}
        return {"type": "cancel_work", "work_id": work_id, "cancelled": self.work_runtime.cancel(work_id)}

    TOOL_IMPLS = {
        "read_file": "_tool_read_file",
        "list_files": "_tool_list_files",
        "search_text": "_tool_search_text",
        "file_info": "_tool_file_info",
        "write_file": "_tool_write_file",
        "append_file": "_tool_append_file",
        "replace_text": "_tool_replace_text",
        "delete_path": "_tool_delete_path",
        "git_commit": "_tool_git_commit",
        "bash_run": "_tool_bash_run",
        "start_work": "_tool_start_work",
        "run_work": "_tool_start_work",
        "work_status": "_tool_work_status",
        "work_logs": "_tool_work_logs",
        "cancel_work": "_tool_cancel_work",
    }

    def _tool_schemas(self, allow_write_tools: bool = False) -> list[dict]:
        schemas = [
            {"type": "function", "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file inside allowed roots.",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"},
                                              "max_chars": {"type": "integer"}},
                               "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "list_files",
                "description": "List files under an allowed directory.",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"},
                                              "recursive": {"type": "boolean"},
                                              "glob": {"type": "string"}}}}},
            {"type": "function", "function": {
                "name": "search_text",
                "description": "Search literal text in files under allowed directory.",
                "parameters": {"type": "object",
                               "properties": {"query": {"type": "string"},
                                              "path": {"type": "string"},
                                              "glob": {"type": "string"},
                                              "max_results": {"type": "integer"}},
                               "required": ["query"]}}},
            {"type": "function", "function": {
                "name": "file_info",
                "description": "Metadata for a file or directory inside allowed roots.",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"}},
                               "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "bash_run",
                "description": (
                    "Run a shell command and return stdout/stderr. "
                    "Use only for short diagnostics and simple read-only checks. "
                    "Do not use for long runs, important commands, file-changing batches, mail/data processing, or tests; use start_work with an argv command array instead. "
                    "Use cwd to set working directory (must be inside allowed roots). "
                    "Default cwd is the workspace. timeout max 1800s."
                ),
                "parameters": {"type": "object",
                               "properties": {
                                   "command": {"type": "string", "description": "Shell command to run"},
                                   "cwd": {"type": "string", "description": "Working directory (optional)"},
                                   "timeout": {"type": "integer", "description": "Timeout in seconds (max 1800)"}},
                               "required": ["command"]}}},
            {"type": "function", "function": {
                "name": "start_work",
                "description": (
                    "Start a long-running background process through Cabinet work runtime. "
                    "Use this instead of bash_run for long runs, important commands, mailbox/classifier runs, tests/batches, file-changing jobs, or anything expected to take over 60 seconds. "
                    "Prefer action_id for manifest-backed project actions. If no action_id is available, command must be an argv array, not a shell string. "
                    "Omit cwd to use the current workspace. Do not invent placeholder paths such as /path/to/workspace. "
                    "Do not invent worker script or module names; run only a command explicitly provided by the task or backed by an existing/created file. "
                    "Returns a work_id; later call work_status/work_logs with that id."
                ),
                "parameters": {"type": "object",
                               "properties": {
                                   "title": {"type": "string", "description": "Short human-readable work title"},
                                   "action_id": {"type": "string", "description": "Optional project manifest action id to run instead of a raw command"},
                                   "command": {"type": "array",
                                               "items": {"type": "string"},
                                               "description": "Command argv array, e.g. ['python','-m','project.long_job']"},
                                   "cwd": {"type": "string", "description": "Optional working directory inside allowed roots. Omit it to use the current workspace; never use placeholder paths."},
                                   "env": {"type": "object", "description": "Optional environment overrides"}},
                               "required": []}}},
            {"type": "function", "function": {
                "name": "run_work",
                "description": (
                    "Alias for start_work. Starts a long-running background process and returns work_id. "
                    "Prefer action_id for manifest-backed project actions. Omit cwd to use the current workspace; never invent placeholder paths or worker module names."
                ),
                "parameters": {"type": "object",
                               "properties": {
                                   "title": {"type": "string"},
                                   "action_id": {"type": "string", "description": "Optional project manifest action id to run instead of a raw command"},
                                   "command": {"type": "array", "items": {"type": "string"}},
                                   "cwd": {"type": "string", "description": "Optional working directory inside allowed roots. Omit to use workspace."},
                                   "env": {"type": "object"}},
                               "required": []}}},
            {"type": "function", "function": {
                "name": "work_status",
                "description": "Read current status/progress/artifacts for a Cabinet work_id.",
                "parameters": {"type": "object",
                               "properties": {"work_id": {"type": "string"}},
                               "required": ["work_id"]}}},
            {"type": "function", "function": {
                "name": "work_logs",
                "description": "Read stdout/stderr tail for a Cabinet work_id.",
                "parameters": {"type": "object",
                               "properties": {
                                   "work_id": {"type": "string"},
                                   "max_chars": {"type": "integer"}},
                               "required": ["work_id"]}}},
            {"type": "function", "function": {
                "name": "cancel_work",
                "description": "Cancel a running Cabinet work item.",
                "parameters": {"type": "object",
                               "properties": {"work_id": {"type": "string"}},
                               "required": ["work_id"]}}},
        ]
        if allow_write_tools:
            schemas.extend([
                {"type": "function", "function": {
                    "name": "git_commit",
                    "description": (
                        "Stage ONLY the specified files and commit. "
                        "Always list the exact file paths you wrote — never all files. "
                        "Call this after every file write, before routing back."
                    ),
                    "parameters": {"type": "object",
                                   "properties": {
                                       "files": {"type": "array",
                                                 "items": {"type": "string"},
                                                 "description": "Exact file paths to stage (relative to cwd, or absolute inside an allowed root). Must be non-empty."},
                                       "message": {"type": "string",
                                                   "description": "Commit message (short, imperative)"},
                                       "cwd": {"type": "string",
                                               "description": "Optional repository root. Defaults to workspace. Must be inside an allowed root."}},
                                   "required": ["files", "message"]}}},
                {"type": "function", "function": {
                    "name": "write_file",
                    "description": "Write a UTF-8 text file inside allowed roots.",
                    "parameters": {"type": "object",
                                   "properties": {"path": {"type": "string"},
                                                  "content": {"type": "string"}},
                                   "required": ["path", "content"]}}},
                {"type": "function", "function": {
                    "name": "append_file",
                    "description": "Append text to the end of a UTF-8 file inside allowed roots. Creates the file if it does not exist.",
                    "parameters": {"type": "object",
                                   "properties": {"path": {"type": "string"},
                                                  "content": {"type": "string"}},
                                   "required": ["path", "content"]}}},
                {"type": "function", "function": {
                    "name": "replace_text",
                    "description": "Replace exact text in a UTF-8 file inside allowed roots.",
                    "parameters": {"type": "object",
                                   "properties": {"path": {"type": "string"},
                                                  "old": {"type": "string"},
                                                  "new": {"type": "string"},
                                                  "count": {"type": "integer"}},
                                   "required": ["path", "old", "new"]}}},
                {"type": "function", "function": {
                    "name": "delete_path",
                    "description": "Delete a file inside allowed roots. Recursive directory deletion is blocked.",
                    "parameters": {"type": "object",
                                   "properties": {"path": {"type": "string"},
                                                  "recursive": {"type": "boolean"}},
                                   "required": ["path"]}}},
            ])
        return schemas

    # ── Ollama API ───────────────────────────────────────────────────────
    def _post_chat(self, payload: dict, timeout: int) -> dict:
        url = self.base_url.rstrip("/") + "/api/chat"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _history_limit_for(self, user_message: str) -> int:
        for line in (user_message or "").splitlines()[:4]:
            if line.startswith("CABINET_HISTORY_LIMIT:"):
                try:
                    return max(4, min(40, int(line.split(":", 1)[1].strip())))
                except ValueError:
                    return self.MAX_HISTORY_MSGS
        if (user_message or "").startswith(("CABINET_TASK_MODE: chat", "CABINET_TASK_MODE: plan")):
            return self.FAST_HISTORY_MSGS
        return self.MAX_HISTORY_MSGS

    def _build_messages(self, system_prompt: str, history: list[dict], user_message: str) -> list[dict]:
        msgs = [{"role": "system", "content": system_prompt}]
        history_limit = self._history_limit_for(user_message)

        # Trim history same as ClaudeCodeAdapter: keep context blocks (unread
        # summary) always, trim real messages to last MAX_HISTORY_MSGS, skip noise.
        context_items = [h for h in history if h.get("type") == "context"]
        real_items = [h for h in history if h.get("type") not in self._SKIP_TYPES | {"context"}]
        trimmed = real_items[-history_limit:]
        omitted = len(real_items) - len(trimmed)

        if omitted:
            msgs.append({"role": "user",
                         "content": f"[{omitted} older messages omitted — full history on disk]"})

        for item in trimmed + context_items:
            ts = item.get("timestamp", "")
            role_name = item.get("role", "?")
            text = item.get("text", "")
            attachments = item.get("attachments") or []
            if attachments:
                lines = ["", "Attachments:"]
                for att in attachments:
                    lines.append(f"- {att.get('name')}: {att.get('path')}")
                text = (text or "").rstrip() + "\n" + "\n".join(lines)
            if item.get("type") == "context":
                ollama_role = "user"
            else:
                ollama_role = "user" if role_name == "BOSS" else "assistant"
            msgs.append({"role": ollama_role, "content": f"[{ts}] {role_name}: {text}"})

        if msgs and msgs[-1]["role"] == "assistant":
            msgs.append({"role": "user", "content": "=== HISTORY START ==="})
        msgs.append({"role": "user", "content": "=== MESSAGE ===\n" + user_message})
        return msgs

    def _return_target_for(self, user_message: str) -> str:
        for line in (user_message or "").splitlines()[:6]:
            if line.startswith("CABINET_FROM:"):
                from_role = line.split(":", 1)[1].strip().lower()
                if from_role in {"hux", "dro"}:
                    return from_role
        return "boss"

    def _known_files_patch_generate(self, prompt: str, timeout: int) -> dict:
        options = dict(self.DEFAULT_OPTIONS)
        options["num_ctx"] = self.KNOWN_FILES_PATCH_NUM_CTX
        # v4: retry-until-green needs sampling diversity; base Modelfile stays
        # deterministic (temp 0) for every other path.
        options["temperature"] = self.KNOWN_FILES_PATCH_TEMPERATURE
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are Golem's code generator inside a deterministic Cabinet harness. "
                        "Return only the exact requested search/replace patch blocks."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "keep_alive": self.DEFAULT_KEEP_ALIVE,
            "options": options,
            "think": False,
        }
        response = self._post_chat(payload, timeout)
        message = response.get("message") or {}
        return {
            "content": message.get("content") or "",
            "prompt_eval_count": response.get("prompt_eval_count"),
            "eval_count": response.get("eval_count"),
            "num_ctx": options["num_ctx"],
            "done_reason": response.get("done_reason"),
        }

    def _known_files_patch_response(
        self,
        user_message: str,
        result,
        elapsed: float,
        error: str | None = None,
    ) -> CallResult:
        target = self._return_target_for(user_message)
        route = target if target in {"hux", "dro"} else ""
        lines = [f"@{target} - known-files patch: {result.status}."]
        lines.append(f"reason: {result.reason}")
        if result.attempts_used:
            lines.append(f"attempts: {result.attempts_used}")
        if result.attempt == "repair":
            lines.append("repair: used bounded repair attempt")
        if result.verification is not None:
            lines.append(
                f"verification: `{result.verification.command}` -> rc={result.verification.returncode}"
            )
        if result.commit:
            lines.append(f"commit: {result.commit}")
        if result.escalation_path:
            lines.append(f"escalation: {result.escalation_path}")
        lines.append(
            json.dumps(
                {
                    "route": route,
                    "write_intent": False,
                    "arch_decision": False,
                    "message": f"known-files patch {result.status}",
                },
                ensure_ascii=False,
            )
        )
        executed_tools = ["known_files_patch"]
        if result.commit:
            executed_tools.append(f"git_commit:success:{result.commit}")
        elif result.status == "escalated":
            executed_tools.append("known_files_patch:escalated")
        return CallResult(
            text="\n".join(lines),
            executed_tools=executed_tools,
            successful_fetch_count=0,
            run_id=str(uuid.uuid4()),
            elapsed=elapsed,
            error=error,
            metrics={
                "known_files_patch": {
                    "status": result.status,
                    "reason": result.reason,
                    "attempt": result.attempt,
                    "attempts": result.attempts,
                    "attempts_used": result.attempts_used,
                    "commit": result.commit,
                    "escalation_path": result.escalation_path,
                    "model_prompt_tokens": result.model_prompt_tokens,
                    "model_eval_tokens": result.model_eval_tokens,
                    "model_eval_tokens_max": result.model_eval_tokens_max,
                    "model_num_ctx": result.model_num_ctx,
                    "model_done_reason": result.model_done_reason,
                    "model_context_total_tokens": result.model_context_total_tokens,
                    "model_context_shift_suspected": result.model_context_shift_suspected,
                }
            },
        )

    def _maybe_run_known_files_patch(
        self,
        user_message: str,
        timeout: int,
    ) -> CallResult | None:
        try:
            delegation = parse_known_files_patch_delegation(user_message)
        except GolemDelegationError as exc:
            target = self._return_target_for(user_message)
            route = target if target in {"hux", "dro"} else ""
            text = (
                f"@{target} - typed patch delegation rejected: {exc}\n"
                + json.dumps(
                    {
                        "route": route,
                        "write_intent": False,
                        "arch_decision": False,
                        "message": "typed patch delegation rejected",
                    },
                    ensure_ascii=False,
                )
            )
            return CallResult(
                text=text,
                executed_tools=[],
                successful_fetch_count=0,
                run_id=str(uuid.uuid4()),
                elapsed=0,
                error=None,
                metrics={"known_files_patch": {"status": "rejected", "reason": str(exc)}},
            )
        if delegation is None:
            return None

        t0 = time.time()
        result = run_search_replace_patch(
            workspace_root=self.workspace,
            delegation=delegation,
            generate=lambda prompt: self._known_files_patch_generate(prompt, timeout),
            model=self.model,
            base_url=self.base_url,
            commit_message=f"golem: {delegation.scope[:60]}\n\nCabinet-Author: @gol",
        )
        return self._known_files_patch_response(user_message, result, time.time() - t0)

    def _tool_result_summary(self, name: str | None, result: dict) -> str:
        kind = name or result.get("type") or "tool"
        if kind == "bash_run":
            rc = result.get("returncode")
            stdout = (result.get("stdout") or "").strip()
            stderr = (result.get("stderr") or "").strip()
            parts = [f"bash_run rc={rc}"]
            if stdout:
                parts.append("stdout:\n" + stdout[-2000:])
            if stderr:
                parts.append("stderr:\n" + stderr[-1000:])
            if result.get("error"):
                parts.append(f"error: {result.get('error')}")
            return "\n".join(parts)
        compact = dict(result)
        if "content" in compact and isinstance(compact["content"], str):
            compact["content"] = compact["content"][:1200]
        if "files" in compact and isinstance(compact["files"], list):
            compact["files"] = compact["files"][:20]
        if "results" in compact and isinstance(compact["results"], list):
            compact["results"] = compact["results"][:20]
        return json.dumps(compact, ensure_ascii=False)[:2000]

    def _tool_result_for_prompt(self, result: Any) -> Any:
        """Bound tool payloads before returning them to the model context."""
        if isinstance(result, dict):
            bounded = dict(result)
            content = bounded.get("content")
            if isinstance(content, str) and len(content) > self.MAX_TOOL_RESULT_CONTENT_CHARS:
                head_len = self.MAX_TOOL_RESULT_CONTENT_CHARS // 2
                tail_len = self.MAX_TOOL_RESULT_CONTENT_CHARS - head_len
                bounded["content"] = (
                    content[:head_len]
                    + f"\n\n[TOOL RESULT TRUNCATED {len(content) - self.MAX_TOOL_RESULT_CONTENT_CHARS} chars]\n\n"
                    + content[-tail_len:]
                )
                bounded["truncated"] = True
                bounded["original_chars"] = bounded.get("original_chars") or len(content)
                bounded["returned_chars"] = len(bounded["content"])
            if isinstance(bounded.get("files"), list) and len(bounded["files"]) > 50:
                original_count = len(bounded["files"])
                bounded["files"] = bounded["files"][:50]
                bounded["truncated"] = True
                bounded["original_count"] = original_count
                bounded["returned_count"] = len(bounded["files"])
            if isinstance(bounded.get("results"), list) and len(bounded["results"]) > 50:
                original_count = len(bounded["results"])
                bounded["results"] = bounded["results"][:50]
                bounded["truncated"] = True
                bounded["original_count"] = original_count
                bounded["returned_count"] = len(bounded["results"])
            return bounded
        if isinstance(result, str) and len(result) > self.MAX_TOOL_RESULT_CONTENT_CHARS:
            return (
                result[: self.MAX_TOOL_RESULT_CONTENT_CHARS // 2]
                + f"\n\n[TOOL RESULT TRUNCATED {len(result) - self.MAX_TOOL_RESULT_CONTENT_CHARS} chars]\n\n"
                + result[-(self.MAX_TOOL_RESULT_CONTENT_CHARS // 2):]
            )
        return result

    def _compact_tool_history(self, messages: list[dict]) -> None:
        tool_indexes = [i for i, msg in enumerate(messages) if msg.get("role") == "tool"]
        for idx in tool_indexes[:-self.MAX_DETAILED_TOOL_RESULTS]:
            msg = messages[idx]
            try:
                payload = json.loads(msg.get("content") or "{}")
            except json.JSONDecodeError:
                payload = {"raw_chars": len(msg.get("content") or "")}
            if isinstance(payload, dict) and payload.get("compacted"):
                continue
            summary: dict[str, Any] = {
                "type": payload.get("type") if isinstance(payload, dict) else "tool",
                "tool_name": msg.get("tool_name"),
                "compacted": True,
            }
            if isinstance(payload, dict):
                for key in (
                    "path",
                    "size",
                    "exists",
                    "changed",
                    "deleted",
                    "replaced",
                    "truncated",
                    "original_chars",
                    "returned_chars",
                    "returncode",
                    "success",
                    "commit",
                    "no_change",
                    "work_id",
                    "status",
                    "error",
                ):
                    if key in payload:
                        summary[key] = payload[key]
                if isinstance(payload.get("content"), str):
                    if len(payload["content"]) <= self.COMPACT_KEEP_CONTENT_CHARS:
                        summary["content"] = payload["content"]
                    else:
                        summary["content_omitted_chars"] = len(payload["content"])
                if isinstance(payload.get("files"), list):
                    summary["files_count"] = len(payload["files"])
                if isinstance(payload.get("results"), list):
                    summary["results_count"] = len(payload["results"])
            msg["content"] = json.dumps(summary, ensure_ascii=False)

    def _record_ollama_step(self, resp: dict, step: int, num_ctx: int, elapsed: float) -> dict:
        prompt_eval_count = resp.get("prompt_eval_count")
        eval_count = resp.get("eval_count")
        ratio = None
        if isinstance(prompt_eval_count, (int, float)) and num_ctx:
            ratio = float(prompt_eval_count) / float(num_ctx)
        return {
            "step": step,
            "num_ctx": num_ctx,
            "prompt_eval_count": prompt_eval_count,
            "eval_count": eval_count,
            "prompt_context_ratio": ratio,
            "context_pressure": bool(ratio is not None and ratio >= self.CONTEXT_PRESSURE_RATIO),
            "done_reason": resp.get("done_reason"),
            "elapsed": elapsed,
        }

    def _context_metrics(self, ollama_steps: list[dict]) -> dict:
        ratios = [
            step.get("prompt_context_ratio")
            for step in ollama_steps
            if isinstance(step.get("prompt_context_ratio"), (int, float))
        ]
        return {
            "ollama_steps": ollama_steps,
            "max_prompt_context_ratio": max(ratios) if ratios else None,
            "context_pressure": any(step.get("context_pressure") for step in ollama_steps),
            "context_pressure_threshold": self.CONTEXT_PRESSURE_RATIO,
            "num_ctx": self.DEFAULT_OPTIONS.get("num_ctx"),
        }

    def _needs_commit_after_write(self, executed_tools: list[str]) -> bool:
        mutating = any(
            tool in {"write_file", "append_file", "replace_text", "delete_path"}
            for tool in executed_tools
        )
        has_git_commit = any(tool.startswith("git_commit:") for tool in executed_tools)
        return mutating and not has_git_commit

    def _detect_direct_batch_task(self, result: dict) -> dict | None:
        if result.get("type") != "read_file" or result.get("error"):
            return None
        content = str(result.get("content") or "")
        path = str(result.get("path") or "")
        lowered = content.lower()
        source_file_count = len(set(re.findall(r"\bsource_\d{2}\.txt\b", lowered)))
        explicit_batch_language = (
            "source files:" in lowered
            or "read each source file" in lowered
            or "read all source files" in lowered
            or "one file per tool call" in lowered
        )
        if (
            explicit_batch_language
            and source_file_count >= self.MAX_DIRECT_BATCH_SPEC_FILES
        ):
            return {
                "path": path,
                "source_file_count": source_file_count,
                "reason": "explicit source-file batch task",
            }
        return None

    def _workruntime_guard_pressure(
        self,
        executed_tools: list[str],
        next_tools: list[str],
        batch_task: dict | None = None,
        workruntime_states: dict[str, str] | None = None,
    ) -> dict | None:
        if not self.work_runtime and not batch_task:
            return None
        tool_names = [tool.split(":", 1)[0] for tool in executed_tools]
        work_started = any(tool in {"start_work", "run_work"} for tool in tool_names)
        states = workruntime_states or {}
        work_succeeded = bool(states) and all(status == "succeeded" for status in states.values())
        # A freestyle loop is caught for mutations too, not just read/append: write_file/
        # replace_text/delete_path in a loop is exactly the unreliable self-orchestrated
        # loop a 14B model produces, and it needs to be cut over to WorkRuntime. A single
        # focused edit + commit (writes < cap) passes through — Golem stays full hands.
        _loop_tools = {"read_file", "append_file", "write_file", "replace_text", "delete_path"}
        _mutation_loop_tools = {"write_file", "replace_text", "delete_path"}
        direct_tools = tool_names + [tool for tool in next_tools if tool in _loop_tools]
        direct_reads = direct_tools.count("read_file")
        direct_appends = direct_tools.count("append_file")
        direct_writes = sum(direct_tools.count(tool) for tool in _mutation_loop_tools)
        direct_total = direct_reads + direct_appends + direct_writes
        unsafe_batch_tools = {
            "read_file",
            "append_file",
            "write_file",
            "replace_text",
            "delete_path",
            "bash_run",
            "git_commit",
        }
        batch_task_unsafe_tool = (
            bool(batch_task)
            and not work_succeeded
            and any(tool in unsafe_batch_tools for tool in next_tools)
        )
        batch_task_direct_loop = (
            bool(batch_task)
            and any(tool in _loop_tools for tool in next_tools)
            and direct_total >= 2
        )
        if work_started and not batch_task_unsafe_tool:
            return None
        if (
            batch_task_unsafe_tool
            or batch_task_direct_loop
            or direct_reads >= self.MAX_DIRECT_BATCH_READS
            or direct_appends >= self.MAX_DIRECT_BATCH_APPENDS
            or direct_writes >= self.MAX_DIRECT_BATCH_WRITES
            or direct_total >= self.MAX_DIRECT_BATCH_TOOLS
        ):
            return {
                "direct_reads": direct_reads,
                "direct_appends": direct_appends,
                "direct_writes": direct_writes,
                "direct_total": direct_total,
                "batch_task_detected": bool(batch_task),
                "batch_task": batch_task,
                "batch_task_unsafe_tool": batch_task_unsafe_tool,
                "workruntime_states": dict(states),
                "max_direct_batch_reads": self.MAX_DIRECT_BATCH_READS,
                "max_direct_batch_appends": self.MAX_DIRECT_BATCH_APPENDS,
                "max_direct_batch_writes": self.MAX_DIRECT_BATCH_WRITES,
                "max_direct_batch_tools": self.MAX_DIRECT_BATCH_TOOLS,
            }
        return None

    def _workruntime_required_prompt(self, pressure: dict) -> str:
        return (
            "SYSTEM: WorkRuntime is mandatory for this batch-style task. "
            "You are entering a repeated direct tool loop "
            f"(read_file={pressure.get('direct_reads')}, append_file={pressure.get('direct_appends')}, "
            f"write_file/replace_text/delete_path={pressure.get('direct_writes')}). "
            "Stop using repeated read_file/append_file/write_file calls for the batch. "
            "Create or use a worker and call start_work/run_work with an argv command array, "
            "omit cwd unless the real workspace path is known, "
            "and do not invent missing worker scripts or module names. "
            "wait with work_status until succeeded or failed, read only a short result/artifact if needed, "
            "then git_commit the observed changed files. "
            "Do not claim completion until WorkRuntime status and git/tool evidence exist."
        )

    def _partial_workruntime_required_response(
        self,
        user_message: str,
        executed_count: int,
        tool_summaries: list[str],
        pressure: dict,
    ) -> str:
        target = self._return_target_for(user_message)
        route = target if target in {"hux", "dro"} else ""
        tail = "\n\n".join(tool_summaries[-3:]).strip() or "tool output unavailable"
        text = (
            f"@{target} - Golem was stopped after {executed_count} tool calls because this is a "
            "batch-style task and WorkRuntime is required.\n\n"
            "Cabinet detected a repeated direct tool loop instead of start_work/work_status: "
            f"read_file={pressure.get('direct_reads')}, append_file={pressure.get('direct_appends')}. "
            "This is a partial observed result, not a completion report.\n\n"
            "Last tool results:\n"
            f"{tail}\n\n"
            "files_changed: none\n"
            "commit: none\n"
            "verification: stopped because WorkRuntime is required for batch-style work"
        )
        route_data = {
            "route": route,
            "write_intent": False,
            "arch_decision": False,
            "message": "Golem direct batch loop stopped; WorkRuntime is required.",
        }
        return text + "\n" + json.dumps(route_data, ensure_ascii=False)

    def _record_workruntime_status(self, states: dict[str, str], name: str, result: dict) -> None:
        if name in {"start_work", "run_work"}:
            work_id = result.get("work_id")
            if work_id:
                states[str(work_id)] = str(result.get("status") or "unknown")
            elif result.get("error"):
                states["__workruntime__"] = "error"
            return
        if name == "work_status":
            meta = result.get("work") if isinstance(result.get("work"), dict) else {}
            work_id = result.get("work_id") or meta.get("work_id")
            if work_id:
                states[str(work_id)] = str(meta.get("status") or "missing")

    def _workruntime_postcondition_failure(self, states: dict[str, str]) -> dict | None:
        if not states:
            return None
        not_succeeded = {work_id: status for work_id, status in states.items() if status != "succeeded"}
        if not_succeeded:
            return {"work_statuses": dict(states), "not_succeeded": not_succeeded}
        return None

    def _workruntime_postcondition_prompt(self, failure: dict) -> str:
        return (
            "SYSTEM: WorkRuntime postcondition is not satisfied. "
            f"Observed work statuses: {json.dumps(failure.get('work_statuses') or {}, ensure_ascii=False)}. "
            "Do not call git_commit and do not report success unless the relevant work_status is succeeded. "
            "If work is still running, call work_status again. If work failed, report the observed failure "
            "and do not claim completion."
        )

    def _partial_workruntime_postcondition_response(
        self,
        user_message: str,
        executed_count: int,
        tool_summaries: list[str],
        failure: dict,
    ) -> str:
        target = self._return_target_for(user_message)
        route = target if target in {"hux", "dro"} else ""
        tail = "\n\n".join(tool_summaries[-3:]).strip() or "tool output unavailable"
        statuses = json.dumps(failure.get("work_statuses") or {}, ensure_ascii=False)
        text = (
            f"@{target} - Golem was stopped after {executed_count} tool calls because WorkRuntime "
            "postconditions were not satisfied.\n\n"
            f"Observed work statuses: {statuses}. "
            "This is a partial observed result, not a completion report.\n\n"
            "Last tool results:\n"
            f"{tail}\n\n"
            "files_changed: none\n"
            "commit: none\n"
            "verification: blocked because WorkRuntime did not report succeeded"
        )
        route_data = {
            "route": route,
            "write_intent": False,
            "arch_decision": False,
            "message": "Golem stopped because WorkRuntime postconditions were not satisfied.",
        }
        return text + "\n" + json.dumps(route_data, ensure_ascii=False)

    def _partial_timeout_response(
        self,
        user_message: str,
        timeout: int,
        executed_count: int,
        tool_summaries: list[str],
        reason: str,
    ) -> str:
        target = self._return_target_for(user_message)
        route = target if target in {"hux", "dro"} else ""
        tail = "\n\n".join(tool_summaries[-3:]).strip() or "tool output unavailable"
        text = (
            f"@{target} — Golem executed {executed_count} tool calls, "
            f"but didn't manage to produce a final reply: {reason}.\n\n"
            "Latest tool results:\n"
            f"{tail}\n\n"
            "files_changed: none\n"
            "commit: none\n"
            "verification: partial tool results returned after timeout"
        )
        route_data = {
            "route": route,
            "write_intent": False,
            "arch_decision": False,
            "message": f"Partial result after timeout {timeout}s and {executed_count} tool calls.",
        }
        return text + "\n" + json.dumps(route_data, ensure_ascii=False)

    def _partial_empty_response(
        self,
        user_message: str,
        executed_count: int,
        empty_retries: int,
        tool_summaries: list[str],
    ) -> str:
        target = self._return_target_for(user_message)
        route = target if target in {"hux", "dro"} else ""
        tail = "\n\n".join(tool_summaries[-3:]).strip() or "tool output unavailable"
        text = (
            f"@{target} — Golem executed {executed_count} tool calls, "
            f"but Ollama returned an empty response after {empty_retries} retries.\n\n"
            "This is a partial observed result, not a final task-completion report.\n\n"
            "Latest tool results:\n"
            f"{tail}\n\n"
            "files_changed: none\n"
            "commit: none\n"
            "verification: partial tool results returned after empty model response"
        )
        route_data = {
            "route": route,
            "write_intent": False,
            "arch_decision": False,
            "message": f"Partial result after empty Ollama response and {executed_count} tool calls.",
        }
        return text + "\n" + json.dumps(route_data, ensure_ascii=False)

    def call(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        timeout: int = 3600,
        allow_write_tools: bool = True,  # Golem is the hands/executor, write is always on by role
        thread_id: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> CallResult:
        known_files_result = self._maybe_run_known_files_patch(user_message, timeout)
        if known_files_result is not None:
            return known_files_result

        run_id = str(uuid.uuid4())
        messages = self._build_messages(system_prompt, history, user_message)
        executed_tools: list[str] = []
        tool_journal: list[dict] = []
        executed_count = 0
        successful_fetches = 0
        tool_summaries: list[str] = []
        tool_call_counts: dict[str, int] = {}
        ollama_steps: list[dict] = []

        self._cancelled.clear()
        t0 = time.time()
        last_msg: dict = {}
        xml_retry_used = False
        empty_retry_count = 0
        commit_retry_count = 0
        workruntime_guard_retry_used = False
        workruntime_postcondition_retry_used = False
        workruntime_states: dict[str, str] = {}
        direct_batch_task: dict | None = None
        while True:
            if self._cancelled.is_set():
                return CallResult(
                    text="", executed_tools=executed_tools, successful_fetch_count=successful_fetches,
                    run_id=run_id, elapsed=time.time() - t0,
                    error="cancelled", metrics={"tool_journal": tool_journal, **self._context_metrics(ollama_steps)},
                )
            if time.time() - t0 > timeout:
                if executed_count:
                    context_metrics = self._context_metrics(ollama_steps)
                    partial_text = self._partial_timeout_response(
                        user_message,
                        timeout,
                        executed_count,
                        tool_summaries,
                        f"timeout {timeout}s",
                    )
                    final_text, observed_report = _with_observed_report(
                        partial_text, tool_journal, context=context_metrics,
                    )
                    return CallResult(
                        text=final_text,
                        executed_tools=executed_tools,
                        successful_fetch_count=successful_fetches,
                        run_id=run_id,
                        elapsed=time.time() - t0,
                        error=None,
                        metrics={
                            "tool_calls": executed_count,
                            "partial_timeout": True,
                            "tool_journal": tool_journal,
                            "observed_report": observed_report,
                            **context_metrics,
                        },
                    )
                return CallResult(
                    text="", executed_tools=executed_tools, successful_fetch_count=successful_fetches,
                    run_id=run_id, elapsed=time.time() - t0,
                    error=f"timeout {timeout}s after {executed_count} tool calls",
                    metrics={
                        "tool_calls": executed_count,
                        "tool_journal": tool_journal,
                        **self._context_metrics(ollama_steps),
                    },
                )
            self._compact_tool_history(messages)
            num_ctx = int(self.DEFAULT_OPTIONS.get("num_ctx") or self.DEFAULT_NUM_CTX)
            payload = {
                "model": self.model,
                "messages": messages,
                "tools": self._tool_schemas(allow_write_tools=allow_write_tools),
                "stream": False,
                "keep_alive": self.DEFAULT_KEEP_ALIVE,
                "options": dict(self.DEFAULT_OPTIONS),
                "think": False,
            }
            remaining = max(1, int(timeout - (time.time() - t0)))
            step_timeout = min(self.STEP_TIMEOUT, remaining)
            step_no = len(ollama_steps) + 1
            step_t0 = time.time()
            try:
                resp = self._post_chat(payload, step_timeout)
                ollama_steps.append(self._record_ollama_step(resp, step_no, num_ctx, time.time() - step_t0))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="ignore")
                if e.code >= 500 and "XML syntax error" in detail and not xml_retry_used:
                    xml_retry_used = True
                    messages.append({
                        "role": "user",
                        "content": (
                            "The previous Ollama tool-call attempt failed inside the runtime "
                            "with an XML syntax error. Retry once. If you call a tool, emit one "
                            "complete valid function call; otherwise answer normally with the "
                            "required routing JSON."
                        ),
                    })
                    continue
                return CallResult(
                    text="", executed_tools=executed_tools, successful_fetch_count=0,
                    run_id=run_id, elapsed=time.time() - t0,
                    error=f"Ollama HTTP {e.code}: {detail[:300]}",
                    metrics={"tool_journal": tool_journal, **self._context_metrics(ollama_steps)},
                )
            except Exception as e:
                if executed_count:
                    context_metrics = self._context_metrics(ollama_steps)
                    partial_text = self._partial_timeout_response(
                        user_message,
                        timeout,
                        executed_count,
                        tool_summaries,
                        f"Ollama follow-up failed: {e!r}",
                    )
                    final_text, observed_report = _with_observed_report(
                        partial_text, tool_journal, context=context_metrics,
                    )
                    return CallResult(
                        text=final_text,
                        executed_tools=executed_tools,
                        successful_fetch_count=successful_fetches,
                        run_id=run_id,
                        elapsed=time.time() - t0,
                        error=None,
                        metrics={
                            "tool_calls": executed_count,
                            "partial_timeout": True,
                            "tool_journal": tool_journal,
                            "observed_report": observed_report,
                            **context_metrics,
                        },
                    )
                return CallResult(
                    text="", executed_tools=executed_tools, successful_fetch_count=0,
                    run_id=run_id, elapsed=time.time() - t0,
                    error=f"Ollama request failed: {e!r}",
                    metrics={"tool_journal": tool_journal, **self._context_metrics(ollama_steps)},
                )

            msg = resp.get("message") or {}
            last_msg = msg
            tool_calls = msg.get("tool_calls") or []
            parsed_tool_calls = False
            if not tool_calls:
                tool_calls = _parse_qwen_tool_call_blocks(msg.get("content") or "")
                parsed_tool_calls = bool(tool_calls)
            if not tool_calls:
                tool_calls = _parse_xml_tool_calls(msg.get("content") or "")
                parsed_tool_calls = bool(tool_calls)
            if not tool_calls:
                tool_calls = _parse_json_tool_calls(msg.get("content") or "")
                parsed_tool_calls = bool(tool_calls)

            if not tool_calls:
                final = (msg.get("content") or "").strip()
                if (
                    not final
                    and executed_count
                    and empty_retry_count < self.MAX_EMPTY_RETRIES_AFTER_TOOLS
                ):
                    empty_retry_count += 1
                    messages.append({
                        "role": "user",
                        "content": (
                            "SYSTEM: Your previous assistant message was empty after tool results. "
                            "Continue from the observed tool results. If more work is required, call "
                            "the next needed tool. If the task is complete, return a concise final "
                            "answer with the required addressee tag and routing JSON."
                        ),
                    })
                    continue
                if not final and executed_count:
                    context_metrics = self._context_metrics(ollama_steps)
                    partial = self._partial_empty_response(
                        user_message,
                        executed_count,
                        empty_retry_count,
                        tool_summaries,
                    )
                    final_text, observed_report = _with_observed_report(
                        partial,
                        tool_journal,
                        context=context_metrics,
                    )
                    return CallResult(
                        text=final_text,
                        executed_tools=executed_tools,
                        successful_fetch_count=successful_fetches,
                        run_id=run_id,
                        elapsed=time.time() - t0,
                        error=None,
                        metrics={
                            "prompt_eval_count": resp.get("prompt_eval_count"),
                            "eval_count": resp.get("eval_count"),
                            "done_reason": resp.get("done_reason"),
                            "partial_empty_response": True,
                            "empty_response_retries": empty_retry_count,
                            "tool_calls": executed_count,
                            "tool_journal": tool_journal,
                            "observed_report": observed_report,
                            **context_metrics,
                        },
                    )
                workruntime_failure = self._workruntime_postcondition_failure(workruntime_states)
                if final and workruntime_failure:
                    if not workruntime_postcondition_retry_used:
                        workruntime_postcondition_retry_used = True
                        messages.append(_strip_thinking(msg))
                        messages.append({
                            "role": "user",
                            "content": self._workruntime_postcondition_prompt(workruntime_failure),
                        })
                        continue
                    context_metrics = self._context_metrics(ollama_steps)
                    partial = self._partial_workruntime_postcondition_response(
                        user_message,
                        executed_count,
                        tool_summaries,
                        workruntime_failure,
                    )
                    final_text, observed_report = _with_observed_report(
                        partial,
                        tool_journal,
                        context=context_metrics,
                    )
                    return CallResult(
                        text=final_text,
                        executed_tools=executed_tools,
                        successful_fetch_count=successful_fetches,
                        run_id=run_id,
                        elapsed=time.time() - t0,
                        error=None,
                        metrics={
                            "prompt_eval_count": resp.get("prompt_eval_count"),
                            "eval_count": resp.get("eval_count"),
                            "done_reason": resp.get("done_reason"),
                            "workruntime_postcondition_failed": True,
                            "workruntime_postcondition": workruntime_failure,
                            "tool_calls": executed_count,
                            "tool_journal": tool_journal,
                            "observed_report": observed_report,
                            **context_metrics,
                        },
                    )
                if (
                    final
                    and allow_write_tools
                    and commit_retry_count < self.MAX_COMMIT_RETRIES_AFTER_WRITE
                    and self._needs_commit_after_write(executed_tools)
                ):
                    commit_retry_count += 1
                    messages.append(_strip_thinking(msg))
                    messages.append({
                        "role": "user",
                        "content": (
                            "SYSTEM: You used file-mutating tools but did not call git_commit. "
                            "Do not give a final completion report yet. Call git_commit now with "
                            "the exact modified file paths and a short commit message. If commit "
                            "fails, report the tool result."
                        ),
                    })
                    continue
                # Validate that the response starts with a required addressee tag.
                # One retry with a corrective prompt if missing.
                if final and not _TAG_RE.match(final) and not getattr(self, "_tag_retry_used", False):
                    self._tag_retry_used = True
                    messages.append(_strip_thinking(msg))
                    messages.append({
                        "role": "user",
                        "content": (
                            "SYSTEM: Your previous response did not start with a required "
                            "addressee tag (@hux, @dro, or @boss). Rewrite your full response "
                            "starting with the correct tag on the very first line."
                        ),
                    })
                    continue
                self._tag_retry_used = False
                # Validate that the response contains required routing JSON.
                # One retry with a corrective prompt if missing.
                # parse_route returns None for route:"" (valid "show Boss" terminator),
                # so use parse_route_data which returns the dict if routing JSON exists.
                _has_route_json = "route" in _routing.parse_route_data(final)
                if final and not _has_route_json and not getattr(self, "_route_retry_used", False):
                    self._route_retry_used = True
                    messages.append(_strip_thinking(msg))
                    messages.append({
                        "role": "user",
                        "content": (
                            "SYSTEM: Your reply is missing the mandatory routing JSON. "
                            "Add this to the very end of your reply:\n"
                            '{"route":"<hux|dro|boss|>","write_intent":false,"arch_decision":false,"message":"<short report>"}\n'
                            "If a sage called you, return {\"route\":\"hux\"} or {\"route\":\"dro\"}. "
                            "If Boss called you directly — {\"route\":\"\"}. One JSON block, the last line of your reply."
                        ),
                    })
                    continue
                self._route_retry_used = False
                context_metrics = self._context_metrics(ollama_steps)
                final_text, observed_report = _with_observed_report(
                    final or "[ERROR] empty response",
                    tool_journal,
                    context=context_metrics,
                )
                return CallResult(
                    text=final_text,
                    executed_tools=executed_tools,
                    successful_fetch_count=successful_fetches,
                    run_id=run_id,
                    elapsed=time.time() - t0,
                    error=None if final else "empty response",
                    metrics={
                        "prompt_eval_count": resp.get("prompt_eval_count"),
                        "eval_count": resp.get("eval_count"),
                        "done_reason": resp.get("done_reason"),
                        "tool_journal": tool_journal,
                        "observed_report": observed_report,
                        **context_metrics,
                    },
                )

            next_tool_names = []
            for tc in tool_calls:
                func = tc.get("function") or {}
                name = func.get("name")
                if isinstance(name, str):
                    next_tool_names.append(name)
            workruntime_pressure = self._workruntime_guard_pressure(
                executed_tools,
                next_tool_names,
                batch_task=direct_batch_task,
                workruntime_states=workruntime_states,
            )
            if workruntime_pressure:
                if direct_batch_task and not self.work_runtime:
                    context_metrics = self._context_metrics(ollama_steps)
                    partial_text = self._partial_workruntime_required_response(
                        user_message,
                        executed_count,
                        tool_summaries,
                        workruntime_pressure,
                    )
                    final_text, observed_report = _with_observed_report(
                        partial_text, tool_journal, context=context_metrics,
                    )
                    return CallResult(
                        text=final_text,
                        executed_tools=executed_tools,
                        successful_fetch_count=successful_fetches,
                        run_id=run_id,
                        elapsed=time.time() - t0,
                        error=None,
                        metrics={
                            "tool_calls": executed_count,
                            "stopped_reason": "workruntime_required",
                            "workruntime_required": True,
                            "workruntime_guard": workruntime_pressure,
                            "tool_journal": tool_journal,
                            "observed_report": observed_report,
                            **context_metrics,
                        },
                    )
                if not workruntime_guard_retry_used:
                    workruntime_guard_retry_used = True
                    messages.append({
                        "role": "user",
                        "content": self._workruntime_required_prompt(workruntime_pressure),
                    })
                    continue
                context_metrics = self._context_metrics(ollama_steps)
                partial_text = self._partial_workruntime_required_response(
                    user_message,
                    executed_count,
                    tool_summaries,
                    workruntime_pressure,
                )
                final_text, observed_report = _with_observed_report(
                    partial_text, tool_journal, context=context_metrics,
                )
                return CallResult(
                    text=final_text,
                    executed_tools=executed_tools,
                    successful_fetch_count=successful_fetches,
                    run_id=run_id,
                    elapsed=time.time() - t0,
                    error=None,
                    metrics={
                        "tool_calls": executed_count,
                        "stopped_reason": "workruntime_required",
                        "workruntime_required": True,
                        "workruntime_guard": workruntime_pressure,
                        "tool_journal": tool_journal,
                        "observed_report": observed_report,
                        **context_metrics,
                    },
                )

            if parsed_tool_calls:
                messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
            else:
                messages.append(_strip_thinking(msg))
            for tc in tool_calls:
                func = (tc.get("function") or {})
                name = func.get("name")
                args = func.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                if direct_batch_task and name in {"start_work", "run_work"} and isinstance(args, dict):
                    args = dict(args)
                    args["batch_redirect"] = True
                try:
                    tool_sig = json.dumps([name, args], sort_keys=True, ensure_ascii=False, default=str)
                except TypeError:
                    tool_sig = f"{name}:{str(args)[:1000]}"
                tool_call_counts[tool_sig] = tool_call_counts.get(tool_sig, 0) + 1
                if tool_call_counts[tool_sig] > 3:
                    context_metrics = self._context_metrics(ollama_steps)
                    partial_text = self._partial_timeout_response(
                        user_message,
                        timeout,
                        executed_count,
                        tool_summaries,
                        f"repeated identical tool call stopped: {name}",
                    )
                    final_text, observed_report = _with_observed_report(
                        partial_text, tool_journal, context=context_metrics,
                    )
                    return CallResult(
                        text=final_text,
                        executed_tools=executed_tools,
                        successful_fetch_count=successful_fetches,
                        run_id=run_id,
                        elapsed=time.time() - t0,
                        error=None,
                        metrics={
                            "tool_calls": executed_count,
                            "stopped_reason": "repeated_identical_tool_call",
                            "duplicate_tool_loop": True,
                            "tool_journal": tool_journal,
                            "observed_report": observed_report,
                            **context_metrics,
                        },
                    )
                # Emit progress event before executing the tool so UI updates immediately.
                if on_progress and name:
                    try:
                        on_progress({
                            "type": "agent_tool_use",
                            "agent": self.role,
                            "tool": name,
                            "label": _tool_label(name, args),
                        })
                    except Exception:
                        pass
                journal_entry: dict[str, Any] = {
                    "tool": name,
                    "args": _sanitize_tool_journal_value(args),
                    "status": "started",
                }
                tool_journal.append(journal_entry)
                workruntime_commit_failure = (
                    self._workruntime_postcondition_failure(workruntime_states)
                    if name == "git_commit"
                    else None
                )
                if workruntime_commit_failure:
                    result = {
                        "type": "git_commit",
                        "error": (
                            "blocked: WorkRuntime postcondition not satisfied; "
                            f"statuses={json.dumps(workruntime_commit_failure.get('work_statuses') or {}, ensure_ascii=False)}"
                        ),
                        "blocked": True,
                        "workruntime_postcondition": workruntime_commit_failure,
                    }
                elif name in self.TOOL_IMPLS:
                    impl = getattr(self, self.TOOL_IMPLS[name])
                    try:
                        result = impl(args)
                    except Exception as exc:
                        journal_entry["status"] = "error"
                        journal_entry["error"] = repr(exc)
                        raise
                else:
                    result = {"error": f"unknown tool {name}"}
                journal_entry["status"] = "done"
                journal_entry["result"] = _sanitize_tool_journal_value(result)
                if isinstance(result, dict):
                    if not direct_batch_task:
                        direct_batch_task = self._detect_direct_batch_task(result)
                    self._record_workruntime_status(workruntime_states, name, result)
                # Tag mutating tools with outcome details so validators can
                # distinguish real changes from failed/no-op claims.
                if name == "write_file" and isinstance(result, dict) and not result.get("changed", True):
                    executed_tools.append("write_file:unchanged")
                elif name == "git_commit" and isinstance(result, dict):
                    if result.get("success") and result.get("commit"):
                        executed_tools.append(f"git_commit:success:{result['commit']}")
                    elif result.get("success") and result.get("no_change"):
                        executed_tools.append("git_commit:no_change")
                    elif result.get("success"):
                        executed_tools.append("git_commit:success")
                    else:
                        executed_tools.append("git_commit:failed")
                else:
                    executed_tools.append(name)
                if name in {"write_file", "append_file", "replace_text", "delete_path"}:
                    commit_retry_count = 0
                executed_count += 1
                if isinstance(result, dict):
                    tool_summaries.append(self._tool_result_summary(name, result))
                else:
                    tool_summaries.append(str(result)[:2000])
                messages.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": json.dumps(self._tool_result_for_prompt(result), ensure_ascii=False),
                })


    def healthcheck(self) -> tuple[bool, str]:
        try:
            req = urllib.request.Request(self.base_url + "/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            names = {m.get("name") for m in data.get("models", [])}
            if self.model in names:
                return True, f"ok (model {self.model} available)"
            return False, f"model {self.model} not in Ollama (have: {sorted(names)})"
        except Exception as e:
            return False, f"Ollama not reachable: {e!r}"
