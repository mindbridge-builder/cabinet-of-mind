"""Storage primitives for Cabinet of Mind.

Copied as-is from C:\\cabinet\\storage.py — proven atomic-write + JSONL log
pattern. Only the archive filename prefix differs (CABINET_LOG vs CABINET_LOG).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path


def log_json_line(item: dict) -> str:
    # ensure_ascii=True: all non-ASCII (Cyrillic etc.) stored as \uXXXX escapes.
    # Prevents mojibake when PowerShell reads the JSONL via shell.
    return json.dumps(item, ensure_ascii=True)


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically via a unique-named tmp file + os.replace.

    UUID-named tmp avoids WinError 32 contention when two threads write to the
    same target at once. os.replace is retried with back-off for the rare case
    where another process holds a handle on the destination.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"_atomic_write: could not replace {path} after 10 attempts")


def save_log_file(log_file: Path, messages: list[dict], saved_count: int, full: bool = False) -> int:
    if full or not log_file.exists():
        text = "".join(log_json_line(item) + "\n" for item in messages)
        _atomic_write(log_file, text)
        return len(messages)

    new_items = messages[saved_count:]
    if not new_items:
        return saved_count

    with open(log_file, "a", encoding="utf-8") as f:
        for item in new_items:
            f.write(log_json_line(item) + "\n")
    return len(messages)


def parse_jsonl_log_text(text: str, max_messages: int | None = None) -> list[dict]:
    restored = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if not {"id", "role", "text", "timestamp"}.issubset(item):
            continue
        restored.append(item)
    if max_messages is not None:
        restored = restored[-max_messages:]
    return restored


def archive_log_if_needed(
    log_file: Path,
    archive_dir: Path,
    max_bytes: int,
    max_messages: int,
    archive_prefix: str = "CABINET_LOG",
) -> tuple[bool, list[dict], Path | None]:
    if not log_file.exists():
        return False, [], None

    text = log_file.read_text(encoding="utf-8", errors="replace")
    all_messages = parse_jsonl_log_text(text)
    if log_file.stat().st_size < max_bytes and len(all_messages) <= max_messages:
        return False, all_messages, None

    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = archive_dir / f"{archive_prefix}_{stamp}.jsonl"
    dest.write_text(text, encoding="utf-8")

    recent = all_messages[-max_messages:]
    save_log_file(log_file, recent, 0, full=True)
    return True, recent, dest


def load_log_file(log_file: Path, max_messages: int) -> list[dict]:
    if not log_file.exists():
        return []
    text = log_file.read_text(encoding="utf-8", errors="replace")
    return parse_jsonl_log_text(text, max_messages)


def save_pending_file(pending_file: Path, pending_items: list[dict]) -> None:
    _atomic_write(
        pending_file,
        json.dumps(pending_items, ensure_ascii=True, indent=2),
    )


def load_pending_file(pending_file: Path) -> list[dict]:
    if not pending_file.exists():
        return []
    try:
        restored = json.loads(pending_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    return restored if isinstance(restored, list) else []


def save_seen_file(seen_file: Path, seen_by_role: dict[str, int]) -> None:
    _atomic_write(seen_file, json.dumps(seen_by_role, ensure_ascii=True, indent=2))


def load_seen_file(seen_file: Path) -> dict[str, int]:
    if not seen_file.exists():
        return {}
    try:
        restored = json.loads(seen_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(restored, dict):
        return {}
    result: dict[str, int] = {}
    for role, value in restored.items():
        try:
            result[str(role)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def pop_pending_item(
    pending_items: list[dict],
    thread_id: str | None = None,
    pending_id: str | None = None,
) -> dict | None:
    if not pending_items:
        return None

    idx = 0
    if pending_id:
        for i, item in enumerate(pending_items):
            if str(item.get("id")) == str(pending_id):
                idx = i
                break
        else:
            return None
    elif thread_id:
        for i, item in enumerate(pending_items):
            if str(item.get("thread_id")) == str(thread_id):
                idx = i
                break

    return pending_items.pop(idx)
