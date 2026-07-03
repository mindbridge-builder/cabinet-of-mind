"""Cabinet of Mind — WebSocket + HTTP server for the multi-agent chat.

Agents: @hux (Claude Code CLI), @dro (Codex CLI), @gol (Ollama).
Pending Y/N queue for critical actions. WS token auth optional.

Routes:
  - HTTP / and /index.html → static UI
  - HTTP /health → JSON health
  - WS /?token=... → chat protocol
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

# Ensure adapters/ and core/ are importable when run as `python server.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters.claude_code_cli import ClaudeCodeAdapter
from adapters.codex_cli import CodexAdapter
from adapters.ollama import OllamaAdapter
from core import storage
from core.dispatcher import Dispatcher
from core import routing
from core.work_templates import WorkTemplateError, compile_work_template, template_ids
from core.work_runtime import WorkRuntime
from core.work_store import WorkStore


WORK_DIR = Path(__file__).resolve().parent
REALITY_FILE = WORK_DIR / "CABINET_REALITY.json"
LOCAL_REALITY_FILE = WORK_DIR / "CABINET_REALITY.local.json"


def _load_reality() -> dict:
    data: dict = {}
    try:
        data = json.loads(REALITY_FILE.read_text("utf-8"))
    except Exception:
        data = {}
    try:
        local = json.loads(LOCAL_REALITY_FILE.read_text("utf-8"))
        if isinstance(local, dict):
            data.update(local)
    except Exception:
        pass
    env_project = os.environ.get("CABINET_ACTIVE_PROJECT")
    if env_project is not None:
        data["active_project"] = env_project.strip()
    return data


def _save_reality(data: dict) -> None:
    try:
        LOCAL_REALITY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def _reality_url_port(reality: dict, key: str, default: int) -> int:
    raw = str(reality.get(key) or "")
    try:
        parsed = urllib.parse.urlparse(raw)
        return int(parsed.port or default)
    except (TypeError, ValueError):
        return default


def _reality_allowed_roots(reality: dict) -> list[Path]:
    roots: list[Path] = []
    for raw in reality.get("allowed_roots") or []:
        if isinstance(raw, str) and raw.strip():
            roots.append(Path(raw))
    if not roots:
        roots.append(WORK_DIR)
    project_root = _active_project_root(reality)
    if project_root and project_root not in roots:
        roots.append(project_root)
    return roots


def _active_project_root(reality: dict | None = None) -> Path | None:
    data = reality if reality is not None else _load_reality()
    raw = data.get("active_project") or data.get("project_root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw)


def _load_project_manifest(project_root: Path | None = None) -> dict:
    root = project_root or _project_root()
    manifest = root / "project.json"
    if not manifest.exists():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


_REALITY = _load_reality()
HTTP_PORT = _reality_url_port(_REALITY, "ui", 9390)
WS_PORT = _reality_url_port(_REALITY, "ws", 8381)
ALLOWED_ROOTS = _reality_allowed_roots(_REALITY)
LOG_FILE = WORK_DIR / "CABINET_LOG.jsonl"
PENDING_FILE = WORK_DIR / "pending_queue.json"
SEEN_FILE = WORK_DIR / "agent_seen.json"
WORK_ITEMS_DIR = WORK_DIR / "work"
ATTACHMENTS_DIR = WORK_DIR / "attachments"
LOG_ARCHIVE_DIR = WORK_DIR / "logs" / "archive"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_MAX_MESSAGES = 500
WS_TOKEN_FILE = WORK_DIR / "ws_token.txt"


def _ensure_ws_token(token_file: Path | None = None) -> str:
    """The WS token is on by default: generated on first start and saved
    to ws_token.txt (gitignored via *token*.txt).

    Why, on a local-only server: WebSocket isn't protected by the browser's
    same-origin policy — a script in any open tab could connect to
    ws://localhost:8381 and send commands to the agents. A token in the URL
    closes that vector without changing the UX (runUI opens the browser
    already carrying the token).

    CABINET_WS_TOKEN in the environment overrides the file; an empty value
    deliberately disables authentication.
    """
    env = os.environ.get("CABINET_WS_TOKEN")
    if env is not None:
        return env.strip()
    p = token_file or WS_TOKEN_FILE
    try:
        token = p.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(24)
        try:
            p.write_text(token, encoding="utf-8")
        except OSError:
            pass  # didn't save — token lives until restart, the browser gets it via URL
    return token


WS_TOKEN = _ensure_ws_token()
APPROVAL_ACCESS_LABEL = "with normal workspace access (workspace-write)"

# ── State ────────────────────────────────────────────────────────────────
_history: list[dict] = []
pending_queue: list[dict] = []
_history_lock = threading.RLock()
pending_lock = threading.Lock()
_next_id = 1
_saved_count = 0
_clients: set = set()
_loop: asyncio.AbstractEventLoop | None = None
_codex_limits: dict | None = None
_codex_limits_lock = threading.Lock()
_claude_limits: dict | None = None
_claude_limits_lock = threading.Lock()
CLAUDE_LIMITS_FILE = Path(__file__).resolve().parent / "logs" / "claude_limits.json"


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def next_id() -> str:
    global _next_id
    with _history_lock:
        v = str(_next_id)
        _next_id += 1
    return v


def add_msg(
    role: str,
    text: str,
    thread_id: str | None = None,
    msg_type: str = "message",
    attachments: list[dict] | None = None,
) -> dict:
    msg = {
        "id": next_id(),
        "role": role,
        "text": text,
        "timestamp": ts(),
        "thread_id": thread_id,
        "type": msg_type,
    }
    if attachments:
        msg["attachments"] = attachments
    with _history_lock:
        _history.append(msg)
    save_log()
    return msg


def _safe_attachment_name(name: str, fallback: str) -> str:
    base = Path(name or fallback).name.strip() or fallback
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    return base[:120] or fallback


def _store_attachments(message_id: str, raw_items) -> list[dict]:
    if not isinstance(raw_items, list):
        return []

    saved: list[dict] = []
    target_dir = ATTACHMENTS_DIR / str(message_id)
    for idx, item in enumerate(raw_items[:10], 1):
        if not isinstance(item, dict):
            continue
        data_url = item.get("data")
        if not isinstance(data_url, str) or "," not in data_url:
            continue
        meta, payload = data_url.split(",", 1)
        if ";base64" not in meta:
            continue
        try:
            content = base64.b64decode(payload, validate=True)
        except Exception:
            continue
        if len(content) > 10 * 1024 * 1024:
            continue

        filename = _safe_attachment_name(str(item.get("name") or ""), f"attachment-{idx}")
        path = target_dir / filename
        suffix = 1
        while path.exists():
            stem = Path(filename).stem or "attachment"
            ext = Path(filename).suffix
            path = target_dir / f"{stem}-{suffix}{ext}"
            suffix += 1

        target_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        try:
            rel = path.resolve().relative_to(WORK_DIR.resolve()).as_posix()
        except ValueError:
            rel = "attachments/" + path.name
        saved.append({
            "name": path.name,
            "path": str(path),
            "url": "/" + urllib.parse.quote(rel),
            "size": len(content),
            "content_type": item.get("type") or "application/octet-stream",
        })
    return saved


def _with_attachment_context(text: str, attachments: list[dict]) -> str:
    if not attachments:
        return text
    lines = ["", "The user attached file(s). Read them via Read/read_file before you reply:"]
    for item in attachments:
        lines.append(f"  {item.get('name')} → {item.get('path')}")
    return (text or "").rstrip() + "\n" + "\n".join(lines)


def _format_reset_at(raw) -> str | None:
    try:
        return datetime.fromtimestamp(int(raw)).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return None


def read_latest_codex_limits(sessions_dir: Path | None = None) -> dict | None:
    """Read the latest Codex rate_limits block from local Codex session logs."""
    root = sessions_dir or Path(os.environ.get(
        "CABINET_CODEX_SESSIONS_DIR",
        str(Path.home() / ".codex" / "sessions"),
    ))
    if not root.exists():
        return None

    try:
        files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None

    for path in files[:50]:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") or {}
            limits = payload.get("rate_limits") or event.get("rate_limits")
            if not isinstance(limits, dict):
                continue
            primary = limits.get("primary") or {}
            secondary = limits.get("secondary") or {}
            return {
                "primary_used_percent": primary.get("used_percent"),
                "primary_resets_at": primary.get("resets_at"),
                "primary_reset_label": _format_reset_at(primary.get("resets_at")),
                "secondary_used_percent": secondary.get("used_percent"),
                "secondary_resets_at": secondary.get("resets_at"),
                "secondary_reset_label": _format_reset_at(secondary.get("resets_at")),
                "plan_type": limits.get("plan_type"),
                "rate_limit_reached_type": limits.get("rate_limit_reached_type"),
                "source_file": str(path),
                "source_timestamp": event.get("timestamp"),
            }
    return None


def read_latest_claude_limits(path: Path | None = None) -> dict | None:
    """Read the latest Claude Code rate_limit_event saved by ClaudeCodeAdapter."""
    p = path or CLAUDE_LIMITS_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    resets = data.get("resetsAt")
    overage_resets = data.get("overageResetsAt")
    return {
        "status": data.get("status"),
        "rate_limit_type": data.get("rateLimitType"),
        "is_using_overage": data.get("isUsingOverage"),
        "overage_status": data.get("overageStatus"),
        "resets_at": resets,
        "reset_label": _format_reset_at(resets),
        "overage_resets_at": overage_resets,
        "overage_reset_label": _format_reset_at(overage_resets),
        # Fraction of the limit used (0..1) from rate_limit_event —
        # the same number Claude Desktop shows as "N% used".
        "utilization": data.get("utilization"),
        "surpassed_threshold": data.get("surpassedThreshold"),
        "captured_at": data.get("captured_at"),
    }


CLAUDE_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_claude_usage_cache: tuple[float, dict | None] = (0.0, None)
_CLAUDE_USAGE_TTL = 60.0


def _usage_window(raw: dict | None, label_fmt: str) -> tuple[float | None, str | None]:
    """(percent used, reset label) from a five_hour/seven_day block."""
    if not isinstance(raw, dict):
        return None, None
    pct = raw.get("utilization")
    label = None
    resets = raw.get("resets_at")
    if isinstance(resets, str):
        try:
            label = datetime.fromisoformat(resets).astimezone().strftime(label_fmt)
        except ValueError:
            label = None
    return (float(pct) if pct is not None else None), label


def _parse_claude_usage_payload(data: dict) -> dict | None:
    five_pct, five_label = _usage_window(data.get("five_hour"), "%H:%M")
    week_pct, week_label = _usage_window(data.get("seven_day"), "%H:%M %d.%m")
    if five_pct is None and week_pct is None:
        return None
    return {
        "five_hour_pct": five_pct,
        "five_hour_reset_label": five_label,
        "seven_day_pct": week_pct,
        "seven_day_reset_label": week_label,
    }


def read_claude_usage_oauth(timeout: float = 5.0) -> dict | None:
    """Claude subscription usage — the same OAuth endpoint Claude Desktop uses.

    rate_limit_event from stream-json only carries utilization after warning
    thresholds, so we take the "as in Desktop" percentages from here instead,
    authorizing with Claude Code's local token. The endpoint is undocumented:
    any error quietly yields None, and the UI falls back to rate_limit_event data.
    """
    global _claude_usage_cache
    cached_at, cached = _claude_usage_cache
    if time.time() - cached_at < _CLAUDE_USAGE_TTL:
        return cached
    result = None
    try:
        creds = json.loads(CLAUDE_CREDENTIALS_FILE.read_text(encoding="utf-8"))
        token = (creds.get("claudeAiOauth") or {}).get("accessToken")
        if token:
            req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = _parse_claude_usage_payload(json.loads(resp.read().decode("utf-8")))
    except Exception:
        result = None
    _claude_usage_cache = (time.time(), result)
    return result


def refresh_claude_limits() -> dict | None:
    global _claude_limits
    latest = read_latest_claude_limits()
    usage = read_claude_usage_oauth()
    if usage:
        latest = {**(latest or {}), **usage}
    with _claude_limits_lock:
        _claude_limits = latest
    return latest


def claude_limits_snapshot() -> dict | None:
    with _claude_limits_lock:
        return dict(_claude_limits) if _claude_limits else None


def refresh_codex_limits() -> dict | None:
    global _codex_limits
    latest = read_latest_codex_limits()
    with _codex_limits_lock:
        _codex_limits = latest
    return latest


def codex_limits_snapshot() -> dict | None:
    with _codex_limits_lock:
        return dict(_codex_limits) if _codex_limits else None


def save_log() -> None:
    global _saved_count
    with _history_lock:
        snapshot = list(_history)
        try:
            _saved_count = storage.save_log_file(LOG_FILE, snapshot, _saved_count, full=False)
        except PermissionError as e:
            print(f"[storage] save_log skipped: {e}")


def load_log() -> None:
    global _history, _next_id, _saved_count
    LOG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    storage.archive_log_if_needed(
        LOG_FILE, LOG_ARCHIVE_DIR, LOG_MAX_BYTES, LOG_MAX_MESSAGES,
        archive_prefix="CABINET_LOG",
    )
    restored = storage.load_log_file(LOG_FILE, LOG_MAX_MESSAGES)
    max_id = 0
    for item in restored:
        try:
            max_id = max(max_id, int(item.get("id") or 0))
        except (TypeError, ValueError):
            continue
    with _history_lock:
        _history = restored
        _next_id = max_id + 1
        _saved_count = len(_history)


def history_snapshot() -> list[dict]:
    with _history_lock:
        return list(_history)


def save_pending() -> None:
    with pending_lock:
        snapshot = list(pending_queue)
    try:
        storage.save_pending_file(PENDING_FILE, snapshot)
    except PermissionError as e:
        print(f"[storage] save_pending skipped: {e}")


def load_pending() -> None:
    global pending_queue
    restored = storage.load_pending_file(PENDING_FILE)
    with pending_lock:
        pending_queue = restored


def pending_peek() -> dict | None:
    with pending_lock:
        return pending_queue[0] if pending_queue else None


def _same_pending_scope(left: dict, right: dict) -> bool:
    left_thread = left.get("thread_id")
    right_thread = right.get("thread_id")
    if left_thread and right_thread:
        # Different agents in the same thread — separate pendings, they don't displace each other.
        return (str(left_thread) == str(right_thread)
                and left.get("from_role") == right.get("from_role"))
    return (
        left.get("kind") == right.get("kind")
        and left.get("from_role") == right.get("from_role")
        and left.get("target_role") == right.get("target_role")
    )


def pending_enqueue(item: dict) -> dict:
    with pending_lock:
        item = dict(item)
        item.setdefault("id", next_id())
        item.setdefault("created_at", ts())
        pending_queue[:] = [
            existing for existing in pending_queue
            if not _same_pending_scope(existing, item)
        ]
        pending_queue.append(item)
    save_pending()
    return item


def pending_pop(pending_id: str | None = None) -> dict | None:
    with pending_lock:
        item = storage.pop_pending_item(pending_queue, pending_id=pending_id)
    save_pending()
    return item


def pending_discard_related(reference: dict | None) -> None:
    if not reference:
        return
    with pending_lock:
        pending_queue[:] = [
            item for item in pending_queue
            if not _same_pending_scope(item, reference)
        ]
    save_pending()


def request_approval(item: dict) -> None:
    pending = pending_enqueue(item)
    label = pending.get("label") or "Boss approval required"
    summary = pending.get("summary") or label
    reason = pending.get("reason") or ""
    # Broadcast the UI approval widget.
    broadcast_from_thread({
        "type": "pending_confirm",
        "pending": pending,
        "label": label,
    })
    # Also add a visible system message so Boss sees it in chat history
    # and knows to type Y/yes/approve (or use the button).
    note = add_msg(
        "SYSTEM",
        f"🔒 Confirm: {summary}"
        + (f"\nReason: {reason}" if reason else "")
        + "\nType **Y** or click the Approve button.",
        msg_type="approval",
    )
    broadcast_from_thread({"type": "message", "message": note})


# ── Broadcast ────────────────────────────────────────────────────────────
async def broadcast(payload: dict) -> None:
    if not _clients:
        return
    raw = json.dumps(payload, ensure_ascii=False)
    dead = []
    for ws in list(_clients):
        try:
            await ws.send(raw)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def broadcast_from_thread(payload: dict) -> None:
    if _loop:
        asyncio.run_coroutine_threadsafe(broadcast(payload), _loop)


def work_store() -> WorkStore:
    global _work_store
    if _work_store is None:
        _work_store = WorkStore(WORK_ITEMS_DIR)
    return _work_store


def work_runtime() -> WorkRuntime:
    global _work_runtime
    if _work_runtime is None:
        _work_runtime = WorkRuntime(work_store(), broadcast_from_thread)
    return _work_runtime


def work_snapshot(include_terminal: bool = True) -> list[dict]:
    return work_store().list(include_terminal=include_terminal)


# Completed work cards live in the UI for half an hour, then disappear.
# The full history stays in work/state.json: `status` and `logs <id>` in chat
# always show it.
WORK_UI_TERMINAL_TTL = 30 * 60
# We don't deliberately hide stale: a process without a heartbeat might still
# be alive, and Boss should see that state instead of guessing where the task went.
_UI_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _filter_ui_work(items: list[dict], now: float) -> list[dict]:
    cutoff = now - WORK_UI_TERMINAL_TTL
    visible = []
    for item in items:
        if item.get("status") in _UI_TERMINAL_STATUSES:
            finished = item.get("heartbeat_epoch") or item.get("created_at_epoch") or 0
            if finished < cutoff:
                continue
        visible.append(item)
    return visible


def work_snapshot_ui() -> list[dict]:
    return _filter_ui_work(work_snapshot(include_terminal=True), time.time())


def _is_allowed_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for root in ALLOWED_ROOTS:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _project_root() -> Path:
    root = _active_project_root()
    if root and _is_allowed_path(root):
        return root
    return WORK_DIR


def _work_brief(meta: dict) -> str:
    progress = meta.get("progress") or {}
    progress_text = ""
    if progress:
        current = progress.get("current")
        total = progress.get("total")
        if current is not None and total:
            progress_text = f" {current}/{total}"
        elif progress.get("label"):
            progress_text = f" {progress.get('label')}"
    artifacts = meta.get("artifacts") or []
    artifact_text = f", artifacts={len(artifacts)}" if artifacts else ""
    return (
        f"{meta.get('work_id')} [{meta.get('status')}]{progress_text}"
        f"{artifact_text}: {meta.get('title')}"
    )


def _format_work_status(work_id: str | None = None) -> str:
    if work_id:
        meta = work_store().get(work_id)
        if not meta:
            return f"work not found: {work_id}"
        lines = [_work_brief(meta)]
        if meta.get("summary"):
            lines.append(f"summary: {meta.get('summary')}")
        if meta.get("error"):
            lines.append(f"error: {meta.get('error')}")
        if meta.get("stdout"):
            lines.append(f"stdout: {meta.get('stdout')}")
        if meta.get("stderr"):
            lines.append(f"stderr: {meta.get('stderr')}")
        return "\n".join(lines)

    items = work_snapshot(include_terminal=True)[:10]
    if not items:
        return "no work items"
    return "\n".join(_work_brief(item) for item in items)


def _format_work_logs(work_id: str) -> str:
    meta = work_store().get(work_id)
    if not meta:
        return f"work not found: {work_id}"
    stdout = work_store().read_log_tail(work_id, "stdout", 4000).strip()
    stderr = work_store().read_log_tail(work_id, "stderr", 4000).strip()
    parts = [f"logs {work_id}"]
    if stdout:
        parts.append("stdout:\n" + stdout)
    if stderr:
        parts.append("stderr:\n" + stderr)
    if len(parts) == 1:
        parts.append("no logs yet")
    return "\n\n".join(parts)


def _format_work_artifacts(work_id: str) -> str:
    meta = work_store().get(work_id)
    if not meta:
        return f"work not found: {work_id}"
    artifacts = meta.get("artifacts") or []
    if not artifacts:
        return f"no artifacts for {work_id}"
    return "\n".join(str(item.get("path") or item) for item in artifacts)


def _project_action_files() -> list[Path]:
    project_root = _project_root()
    manifest = _load_project_manifest(project_root)
    raw_actions_dir = manifest.get("actions_dir") if isinstance(manifest, dict) else None
    if not raw_actions_dir:
        return []
    actions_dir = Path(str(raw_actions_dir))
    if not actions_dir.is_absolute():
        actions_dir = project_root / actions_dir
    if not _is_allowed_path(actions_dir) or not actions_dir.exists():
        return []
    return sorted(actions_dir.glob("*.json"))


def _load_project_actions() -> dict[str, dict]:
    actions: dict[str, dict] = {}
    for path in _project_action_files():
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
                copy = dict(entry)
                copy["_manifest"] = str(path)
                actions[action_id] = copy
    return actions


def _format_project_actions() -> str:
    actions = _load_project_actions()
    if not actions:
        return "no project actions\nwork templates: " + ", ".join(template_ids())
    lines = ["project actions:"]
    for action_id, action in sorted(actions.items()):
        title = str(action.get("title") or action_id)
        lines.append(f"{action_id}: {title}")
    lines.append("work templates: " + ", ".join(template_ids()))
    return "\n".join(lines)


def _expand_action_value(value, variables: dict[str, str]):
    if isinstance(value, str):
        expanded = value
        for key, replacement in variables.items():
            expanded = expanded.replace("${" + key + "}", replacement)
        return expanded
    if isinstance(value, list):
        return [_expand_action_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _expand_action_value(item, variables) for key, item in value.items()}
    return value


def _action_items(action: dict) -> list[dict]:
    raw_items = action.get("items")
    if isinstance(raw_items, list) and raw_items:
        return [item for item in raw_items if isinstance(item, dict)]
    if isinstance(action.get("command"), list) or action.get("template"):
        return [action]
    return []


def _resolve_action_cwd(raw_cwd: str | None, variables: dict[str, str]) -> Path:
    expanded = _expand_action_value(raw_cwd or "${project_root}", variables)
    cwd = Path(str(expanded))
    if not cwd.is_absolute():
        cwd = _project_root() / cwd
    return cwd


def _action_handoff(action_id: str, action: dict, item: dict, variables: dict[str, str]) -> dict | None:
    raw = item.get("handoff") if isinstance(item.get("handoff"), dict) else action.get("handoff")
    if not isinstance(raw, dict):
        return None
    handoff = _expand_action_value(raw, variables)
    if not isinstance(handoff, dict):
        return None
    handoff.setdefault("action_id", action_id)
    if item.get("template_id"):
        handoff.setdefault("template_id", item.get("template_id"))
    return handoff


def _compile_action_item(action_id: str, action: dict, item: dict, variables: dict[str, str]) -> dict:
    if item.get("template"):
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        compiled = compile_work_template(str(item.get("template")), params, variables)
        merged = {**compiled, **{k: v for k, v in item.items() if k not in {"template", "params"}}}
        merged.setdefault("template_id", compiled["template_id"])
        if "handoff" in compiled and "handoff" not in merged:
            merged["handoff"] = compiled["handoff"]
        return merged
    expanded = _expand_action_value(item, variables)
    expanded.setdefault("template_id", "manual_command")
    return expanded


def _start_project_action(action_id: str, created_from: str | None = None) -> list[dict]:
    action = _load_project_actions().get(action_id)
    if not action:
        raise ValueError(f"unknown project action: {action_id}")
    project_root = _project_root()
    variables = {
        "cabinet_root": str(WORK_DIR),
        "project_root": str(project_root),
        "python": sys.executable,
    }
    items = _action_items(action)
    if not items:
        raise ValueError(f"project action has no runnable items: {action_id}")
    metas: list[dict] = []
    owner = str(action.get("owner") or "system")
    executor = str(action.get("executor") or "local_process")
    for index, item in enumerate(items, start=1):
        try:
            expanded = _compile_action_item(action_id, action, item, variables)
        except WorkTemplateError as exc:
            raise ValueError(f"invalid template for project action {action_id}: {exc}") from exc
        command = expanded.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ValueError(f"invalid command for project action: {action_id}")
        cwd = _resolve_action_cwd(expanded.get("cwd"), variables)
        if not _is_allowed_path(cwd):
            raise ValueError(f"action cwd outside allowed roots: {cwd}")
        item_title = str(expanded.get("title") or action.get("title") or action_id)
        if len(items) > 1 and item_title == str(action.get("title") or action_id):
            item_title = f"{item_title} #{index}"
        metas.append(work_runtime().start_process(
            title=item_title,
            command=command,
            cwd=cwd,
            owner=str(expanded.get("owner") or owner),
            executor=str(expanded.get("executor") or executor),
            created_from=created_from,
            env=expanded.get("env") if isinstance(expanded.get("env"), dict) else None,
            handoff=_action_handoff(action_id, action, expanded, variables),
        ))
    return metas


# ── Adapters and dispatcher (initialized in main) ────────────────────────
_dispatcher: Dispatcher | None = None
_work_store: WorkStore | None = None
_work_runtime: WorkRuntime | None = None


def _load_prompt(filename: str) -> str:
    p = WORK_DIR / "prompts" / filename
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _load_prompt_bundle(*filenames: str) -> str:
    parts = []
    for filename in filenames:
        text = _load_prompt(filename).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _load_golem_prompt() -> str:
    return _load_prompt_bundle("GOL_HANDS.md", "CABINET_BOOTSTRAP.md")


MODEL_ALIASES: dict[str, str] = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "gpt5": "gpt-5.5",
    "gpt5.5": "gpt-5.5",
    "gpt5.4": "gpt-5.4",
    "gpt5.3": "gpt-5.3-codex",
}

# Allowed models per role: for `!model` and the dropdowns in the UI header.
# The hands (gol) — ANY local coder model your card can run:
# `ollama list` shows what's installed; configure via CABINET_REALITY.json or
# the CABINET_HANDS_MODEL env var. golem:hands is the default alias.
ROLE_MODELS: dict[str, list[str]] = {
    "hux": ["claude-fable-5", "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "dro": ["gpt-5.5", "gpt-5.4", "gpt-5.3-codex"],
    "gol": ["golem:hands"],
}


def _models_snapshot() -> dict:
    """Current model and available options per role — for the UI header."""
    result = {}
    if _dispatcher:
        for role, adapter in _dispatcher.adapters.items():
            current = getattr(adapter, "model", None)
            options = list(ROLE_MODELS.get(role, []))
            if current and current not in options:
                options.insert(0, current)
            result[role] = {"current": current, "options": options}
    return result


def init_dispatcher() -> Dispatcher:
    adapters: dict = {}
    errors = []

    reality = _load_reality()
    agents_cfg = reality.get("agents", {})

    try:
        model = agents_cfg.get("hux", {}).get("model")
        adapters["hux"] = ClaudeCodeAdapter(workspace=WORK_DIR, model=model or None)
    except Exception as e:
        errors.append(f"Huxley (Claude Code CLI) unavailable: {e}")

    try:
        model = agents_cfg.get("dro", {}).get("model")
        adapters["dro"] = CodexAdapter(workspace=WORK_DIR, model=model or None)
    except Exception as e:
        errors.append(f"Diderot (Codex CLI) unavailable: {e}")

    try:
        model = agents_cfg.get("gol", {}).get("model")
        ollama_cfg = reality.get("ollama") or {}
        ollama_endpoint = ollama_cfg.get("endpoint") if isinstance(ollama_cfg, dict) else None
        adapters["gol"] = OllamaAdapter(workspace=WORK_DIR,
                                        allowed_roots=ALLOWED_ROOTS,
                                        model=model or None,
                                        base_url=ollama_endpoint or None,
                                        work_runtime=work_runtime())
    except Exception as e:
        errors.append(f"Golem (Ollama) unavailable: {e}")

    for err in errors:
        print(f"[init] {err}")

    system_prompts = {
        "hux": _load_prompt_bundle("HUX_HEAD.md", "HIGH_RISK_ARCH_GATE.md"),
        "dro": _load_prompt_bundle("DRO_HEAD.md", "HIGH_RISK_ARCH_GATE.md"),
        "gol": _load_golem_prompt(),
    }

    return Dispatcher(
        adapters=adapters,
        system_prompts=system_prompts,
        history_provider=history_snapshot,
        broadcaster=broadcast_from_thread,
        add_msg=add_msg,
        request_approval=request_approval,
        seen_file=SEEN_FILE,
        action_starter=_run_gol_action,
    )


# ── Health ───────────────────────────────────────────────────────────────
def health_check() -> dict:
    codex_limits = codex_limits_snapshot() or refresh_codex_limits()
    claude_limits = refresh_claude_limits()
    project_root = _project_root()
    project_manifest = _load_project_manifest(project_root)
    checks = {
        "history": len(_history),
        "ws": WS_PORT,
        "http": HTTP_PORT,
        "ws_auth": "enabled" if WS_TOKEN else "DISABLED",
        "codex_limits": codex_limits,
        "claude_limits": claude_limits,
        "models": _models_snapshot(),
        "project": {
            "root": str(project_root),
            "id": project_manifest.get("id") if project_manifest else None,
            "name": project_manifest.get("name") if project_manifest else None,
            "required_accounts": project_manifest.get("required_accounts", {}) if project_manifest else {},
        },
    }
    if _dispatcher:
        for role, adapter in _dispatcher.adapters.items():
            ok, msg = adapter.healthcheck()
            if ok and role in {"hux", "dro"}:
                ok, msg = adapter.runtime_health()
            if role == "dro" and codex_limits and codex_limits.get("rate_limit_reached_type"):
                ok = False
                msg = f"limit: {codex_limits.get('rate_limit_reached_type')}"
            if (role == "hux" and claude_limits
                    and claude_limits.get("status") == "rejected"
                    and not claude_limits.get("is_using_overage")):
                ok = False
                reset = claude_limits.get("reset_label")
                msg = f"limit: reset {reset}" if reset else "limit"
            checks[f"adapter_{role}"] = "ok" if ok and msg == "ok" else (
                f"ok: {msg}" if ok else f"FAIL: {msg}"
            )
    return checks


def _clear_history_state() -> None:
    global _history, _saved_count
    with _history_lock:
        _history = []
        _saved_count = 0
    with pending_lock:
        pending_queue.clear()
    storage.save_log_file(LOG_FILE, [], 0, full=True)
    save_pending()
    if _dispatcher:
        _dispatcher.reset_seen()


# ── Approve / Reject helpers ─────────────────────────────────────────────
async def _do_approve(pending: dict, comment: str = "") -> None:
    """Shared approve flow used by button and text-Y handler."""
    await broadcast({"type": "pending_cleared"})
    if not pending:
        return
    pending_discard_related(pending)
    msg = add_msg("BOSS", f"[approved] {pending.get('label') or ''}",
                  thread_id=pending.get("thread_id"), msg_type="approval")
    await broadcast({"type": "message", "message": msg})
    if pending.get("kind") == "clear_history":
        _clear_history_state()
        await broadcast({"type": "cleared"})
        await broadcast({"type": "pending_cleared"})
        return
    target = pending.get("target_role") or pending.get("from_role") or "?"
    note = add_msg("SYSTEM", f"✓ Approved. @{target} continues {APPROVAL_ACCESS_LABEL}.",
                   msg_type="approval")
    await broadcast({"type": "message", "message": note})
    if comment:
        pending["boss_comment"] = comment
    if _dispatcher:
        _dispatcher.continue_approved(pending)


async def _do_reject(pending: dict) -> None:
    """Shared reject flow used by button and text-N handler."""
    await broadcast({"type": "pending_cleared"})
    if not pending:
        return
    pending_discard_related(pending)
    label = pending.get("label") or ""
    from_role = pending.get("from_role") or "?"
    msg = add_msg("BOSS", f"[rejected] {label}",
                  thread_id=pending.get("thread_id"), msg_type="approval")
    await broadcast({"type": "message", "message": msg})
    note = add_msg("SYSTEM", f"✗ Rejected. @{from_role} does not continue.",
                   msg_type="approval")
    await broadcast({"type": "message", "message": note})


# ── WebSocket ────────────────────────────────────────────────────────────
def parse_gol_run(text: str) -> str | None:
    """Golem-the-machine-tool's run mode from a direct message by Boss: requires
    a leading @gol. The canonical parse lives in core.routing.parse_gol_run_message;
    the dispatcher intercepts agent replies with the same parser."""
    s = (text or "").strip()
    if not s.lower().startswith("@gol"):
        return None
    return routing.parse_gol_run_message(s)


def _run_gol_action(action_id: str, from_role: str | None = None) -> None:
    """Launches a project action via the machine path + reports to chat in code (no LLM)."""
    try:
        created = f"gol_run:@{from_role}" if from_role else "gol_run"
        metas = _start_project_action(action_id, created_from=created)
        lines = ["[run mode: deterministic launch, no LLM involved]"]
        lines.extend(_format_work_status(meta["work_id"]) for meta in metas)
    except ValueError as exc:
        lines = [f"[run mode] {exc}", "List of actions: the 'actions' command."]
    note = add_msg("GOLEM", "\n".join(lines), msg_type="message")
    broadcast_from_thread({"type": "message", "message": note})
    broadcast_from_thread({"type": "work_list", "work": work_snapshot_ui()})


async def handle(ws, raw: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    action = data.get("action", "message")

    if action == "init":
        await ws.send(json.dumps(
            {
                "type": "init",
                "messages": history_snapshot(),
                "health": health_check(),
                "pending": pending_peek(),
                "work": work_snapshot_ui(),
            },
            ensure_ascii=False))
        return

    if action == "health":
        await broadcast({"type": "health", "checks": health_check()})
        return

    if action == "clear":
        # Raw WS clears are ignored. UI clear asks Boss once in browser, then
        # creates a normal Y/N pending item before any data is erased.
        if data.get("confirm_text") != "CLEAR":
            return
        request_approval({
            "from_role": "system",
            "target_role": None,
            "message": "Clear Cabinet chat history and pending queue.",
            "thread_id": None,
            "hops": 0,
            "allow_write_tools": False,
            "kind": "clear_history",
            "label": "Clear chat history and the pending queue",
        })
        return

    if action == "stop":
        role = (data.get("role") or "").lower().lstrip("@")
        if _dispatcher and role in _dispatcher.adapters:
            _dispatcher.stop_agent(role)
        return

    if action == "approve":
        pending = pending_pop(data.get("pending_id"))
        comment = (data.get("comment") or "").strip()
        await _do_approve(pending, comment)
        return

    if action == "reject":
        pending = pending_pop(data.get("pending_id"))
        await _do_reject(pending)
        return

    text = (data.get("text") or "").strip()
    raw_attachments = data.get("attachments")
    if not text and not raw_attachments:
        return

    lower_text = text.lower()

    if lower_text == "status" or lower_text.startswith("status "):
        parts = text.split(maxsplit=1)
        note = add_msg(
            "SYSTEM",
            _format_work_status(parts[1].strip() if len(parts) > 1 else None),
            msg_type="message",
        )
        await broadcast({"type": "message", "message": note})
        await broadcast({"type": "work_list", "work": work_snapshot_ui()})
        return

    if lower_text.startswith("logs "):
        work_id = text.split(maxsplit=1)[1].strip()
        note = add_msg("SYSTEM", _format_work_logs(work_id), msg_type="message")
        await broadcast({"type": "message", "message": note})
        return

    if lower_text.startswith("artifacts "):
        work_id = text.split(maxsplit=1)[1].strip()
        note = add_msg("SYSTEM", _format_work_artifacts(work_id), msg_type="message")
        await broadcast({"type": "message", "message": note})
        return

    if lower_text.startswith("cancel "):
        work_id = text.split(maxsplit=1)[1].strip()
        ok = work_runtime().cancel(work_id)
        note = add_msg(
            "SYSTEM",
            f"cancel requested: {work_id}" if ok else f"cannot cancel: {work_id}",
            msg_type="message",
        )
        await broadcast({"type": "message", "message": note})
        await broadcast({"type": "work_list", "work": work_snapshot_ui()})
        return

    if lower_text == "actions":
        note = add_msg("SYSTEM", _format_project_actions(), msg_type="message")
        await broadcast({"type": "message", "message": note})
        return

    if lower_text.startswith("action "):
        action_id = text.split(maxsplit=1)[1].strip()
        try:
            metas = _start_project_action(action_id, created_from=f"command:{action_id}")
            lines = [f"Started project action '{action_id}':"]
            lines.extend(_format_work_status(meta["work_id"]) for meta in metas)
        except ValueError as exc:
            lines = [str(exc)]
        note = add_msg("SYSTEM", "\n".join(lines), msg_type="message")
        await broadcast({"type": "message", "message": note})
        await broadcast({"type": "work_list", "work": work_snapshot_ui()})
        return

    # !model <role> <alias|slug>  — switch model without restart
    if text.startswith("!model "):
        parts = text.split()
        if len(parts) >= 3:
            role = parts[1].lower().lstrip("@")
            raw_model = parts[2].lower()
            resolved = MODEL_ALIASES.get(raw_model, parts[2])
            allowed = ROLE_MODELS.get(role, [])
            if resolved not in allowed:
                note = add_msg(
                    "SYSTEM",
                    f"!model: '{resolved}' not available for @{role}. "
                    f"Allowed: {', '.join(allowed) or '—'}",
                    msg_type="system",
                )
                await broadcast({"type": "message", "message": note})
                return
            if _dispatcher and role in _dispatcher.adapters:
                _dispatcher.adapters[role].model = resolved
                reality = _load_reality()
                reality.setdefault("agents", {}).setdefault(role, {})["model"] = resolved
                _save_reality(reality)
                display = {"hux": "Huxley", "dro": "Diderot", "gol": "Golem", "third": "Third"}.get(role, role)
                note = add_msg("SYSTEM", f"{display} → model: {resolved}", msg_type="system")
                await broadcast({"type": "message", "message": note})
                await broadcast({"type": "health", "checks": health_check()})
            else:
                note = add_msg("SYSTEM", f"!model: role @{role} not found", msg_type="system")
                await broadcast({"type": "message", "message": note})
        return

    if pending_peek():
        is_yes = lower_text in {"y", "yes", "approve", "approved", "ok"}
        is_no = lower_text in {"n", "no", "reject", "rejected", "cancel"}
        if is_yes:
            await _do_approve(pending_pop())
            return
        if is_no:
            await _do_reject(pending_pop())
            return

    msg_id = next_id()
    attachments = _store_attachments(msg_id, raw_attachments)
    msg = {
        "id": msg_id,
        "role": "BOSS",
        "text": text,
        "timestamp": ts(),
        "thread_id": data.get("thread_id"),
        "type": "message",
    }
    if attachments:
        msg["attachments"] = attachments
    with _history_lock:
        _history.append(msg)
    save_log()
    await broadcast({"type": "message", "message": msg})

    thread_id = data.get("thread_id") or msg["id"]

    run_action_id = parse_gol_run(text)
    if run_action_id:
        _run_gol_action(run_action_id)
        return

    if _dispatcher:
        _dispatcher.handle_user_mentions(_with_attachment_context(text, attachments), thread_id)


async def ws_handler(ws):
    if WS_TOKEN:
        try:
            raw_path = ws.request.path
        except AttributeError:
            raw_path = getattr(ws, "path", "/")
        query = urllib.parse.urlparse(raw_path).query
        token = urllib.parse.parse_qs(query).get("token", [""])[0]
        if token != WS_TOKEN:
            await ws.close(1008, "Unauthorized")
            return
    _clients.add(ws)
    try:
        async for raw in ws:
            await handle(ws, raw)
    finally:
        _clients.discard(ws)


# ── HTTP ─────────────────────────────────────────────────────────────────
_HTTP_ALLOWED = {"", "/", "/index.html"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WORK_DIR / "ui"), **kwargs)

    def _send_json(self, payload: dict | list, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].split("#")[0]
        if path == "/health":
            self._send_json(health_check())
            return
        if path == "/api/work":
            self._send_json({"work": work_snapshot(include_terminal=True)})
            return
        if path.startswith("/api/work/"):
            parts = [urllib.parse.unquote(p) for p in path.split("/") if p]
            # parts: api, work, <work_id>, optional suffix
            if len(parts) >= 3:
                work_id = parts[2]
                if len(parts) == 3:
                    meta = work_store().get(work_id)
                    self._send_json(meta or {"error": "not found"}, 200 if meta else 404)
                    return
                if len(parts) == 4 and parts[3] == "events":
                    self._send_json({"events": work_store().read_events(work_id)})
                    return
                if len(parts) == 4 and parts[3] == "logs":
                    self._send_json({
                        "stdout": work_store().read_log_tail(work_id, "stdout"),
                        "stderr": work_store().read_log_tail(work_id, "stderr"),
                    })
                    return
            self._send_json({"error": "not found"}, 404)
            return
        if path not in _HTTP_ALLOWED:
            self.send_error(404)
            return
        if path in ("", "/"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0].split("#")[0]
        if path.startswith("/api/work/") and path.endswith("/cancel"):
            parts = [urllib.parse.unquote(p) for p in path.split("/") if p]
            if len(parts) == 4:
                ok = work_runtime().cancel(parts[2])
                self._send_json({"ok": ok})
                return
        self._send_json({"error": "not found"}, 404)

    def end_headers(self):
        # Prevent browser from caching index.html — always serve latest version.
        if self.path.endswith((".html", "/")):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, format, *args):
        pass


def run_http() -> None:
    # 127.0.0.1: Cabinet is a local tool, we don't expose it to the network.
    ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler).serve_forever()


# ── Main ─────────────────────────────────────────────────────────────────
async def _startup_healthcheck() -> None:
    """Run adapter healthchecks on start. Logs to console; only failed
    adapters appear as a chat message so Boss is not spammed on every launch.
    """
    failed = []
    for role, adapter in _dispatcher.adapters.items():
        ok, status = adapter.healthcheck()
        print(f"  [{role}] {'ok' if ok else 'FAIL'}: {status}")
        if not ok:
            failed.append((role, status))
    if failed:
        lines = ["⚠️ Unavailable:"]
        for role, status in failed:
            lines.append(f"  @{role}: {status}")
        lines.append("Check the config and restart the server.")
        msg = add_msg("SYSTEM", "\n".join(lines), msg_type="system")
        await broadcast({"type": "message", "message": msg})


async def _status_monitor() -> None:
    """Refresh non-chat status fields and push them to the UI every 10 minutes."""
    while True:
        refresh_codex_limits()
        refresh_claude_limits()
        await broadcast({"type": "health", "checks": health_check()})
        await broadcast({"type": "work_list", "work": work_snapshot_ui()})
        await asyncio.sleep(600)


async def main_async() -> None:
    global _loop, _dispatcher
    _loop = asyncio.get_running_loop()

    load_log()
    load_pending()
    _dispatcher = init_dispatcher()
    work_runtime()

    print("CABINET OF MIND")
    print(f"  http://localhost:{HTTP_PORT}")
    print(f"  ws://localhost:{WS_PORT}")
    print(f"  adapters: {list(_dispatcher.adapters.keys())}")

    threading.Thread(target=run_http, daemon=True).start()
    if not os.environ.get("CABINET_NO_BROWSER"):
        try:
            url = f"http://localhost:{HTTP_PORT}"
            if WS_TOKEN:
                url += f"?wstoken={WS_TOKEN}"
            webbrowser.open(url)
        except Exception:
            pass

    async with websockets.serve(ws_handler, "127.0.0.1", WS_PORT):
        await _startup_healthcheck()
        asyncio.create_task(_status_monitor())
        while True:
            await asyncio.sleep(3600)


def save_on_exit() -> None:
    if _dispatcher:
        try:
            _dispatcher.stop_all_agents()
        except Exception:
            pass
    try:
        save_log()
    except Exception:
        pass


if __name__ == "__main__":
    os.chdir(WORK_DIR)
    atexit.register(save_on_exit)
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nstopped.")
