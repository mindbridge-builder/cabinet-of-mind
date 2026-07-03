"""Persistent work-item state for Cabinet background work."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from core import storage


ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_epoch() -> float:
    return time.time()


def make_work_id(prefix: str = "work") -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


class WorkStore:
    """Small JSON-file backed store for Cabinet work items.

    Each work item has a directory with meta/events/logs. ``state.json`` is a
    compact index for status listings; per-work ``meta.json`` is the source of
    truth for detailed reads.
    """

    def __init__(self, root: Path, stale_after_s: int = 180):
        self.root = Path(root)
        self.state_file = self.root / "state.json"
        self.stale_after_s = stale_after_s
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def work_dir(self, work_id: str) -> Path:
        return self.root / work_id

    def stdout_path(self, work_id: str) -> Path:
        return self.work_dir(work_id) / "stdout.log"

    def stderr_path(self, work_id: str) -> Path:
        return self.work_dir(work_id) / "stderr.log"

    def events_path(self, work_id: str) -> Path:
        return self.work_dir(work_id) / "events.jsonl"

    def meta_path(self, work_id: str) -> Path:
        return self.work_dir(work_id) / "meta.json"

    def create(
        self,
        *,
        title: str,
        owner: str = "system",
        executor: str = "local_process",
        created_from: str | None = None,
        command: list[str] | None = None,
        cwd: str | None = None,
        work_id: str | None = None,
        handoff: dict | None = None,
    ) -> dict:
        with self._lock:
            work_id = work_id or make_work_id(self._prefix_for(title))
            work_dir = self.work_dir(work_id)
            work_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "work_id": work_id,
                "title": title,
                "owner": owner,
                "executor": executor,
                "status": "queued",
                "created_from": created_from,
                "created_at": now_ts(),
                "created_at_epoch": now_epoch(),
                "started_at": None,
                "finished_at": None,
                "heartbeat_at": None,
                "heartbeat_epoch": None,
                "progress": {},
                "artifacts": [],
                "error": None,
                "pid": None,
                "command": command or [],
                "cwd": cwd,
                "stdout": str(self.stdout_path(work_id)),
                "stderr": str(self.stderr_path(work_id)),
            }
            if handoff:
                meta["handoff"] = handoff
                meta["handoff_id"] = handoff.get("handoff_id")
                meta["template_id"] = handoff.get("template_id")
            self._write_meta(meta)
            self.append_event(work_id, {"type": "created", "title": title})
            self._write_state_index()
            return meta

    def get(self, work_id: str) -> dict | None:
        with self._lock:
            return self._read_meta(work_id)

    def list(self, include_terminal: bool = True) -> list[dict]:
        with self._lock:
            self.mark_stale()
            items = []
            for path in sorted(self.root.glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if include_terminal or item.get("status") not in TERMINAL_STATUSES:
                    items.append(item)
            return items

    def update(self, work_id: str, **fields) -> dict | None:
        with self._lock:
            meta = self._read_meta(work_id)
            if not meta:
                return None
            meta.update(fields)
            self._write_meta(meta)
            self._write_state_index()
            return meta

    def mark_running(self, work_id: str, pid: int | None = None) -> dict | None:
        ts = now_ts()
        return self.update(
            work_id,
            status="running",
            started_at=ts,
            heartbeat_at=ts,
            heartbeat_epoch=now_epoch(),
            pid=pid,
        )

    def heartbeat(self, work_id: str, progress: dict | None = None) -> dict | None:
        meta = self._read_meta(work_id)
        if not meta or meta.get("status") not in ACTIVE_STATUSES:
            return meta
        fields = {
            "heartbeat_at": now_ts(),
            "heartbeat_epoch": now_epoch(),
        }
        if progress is not None:
            fields["progress"] = progress
        return self.update(work_id, **fields)

    def add_artifact(self, work_id: str, path: str, label: str | None = None) -> dict | None:
        meta = self._read_meta(work_id)
        if not meta:
            return None
        artifacts = list(meta.get("artifacts") or [])
        item = {"path": path}
        if label:
            item["label"] = label
        if item not in artifacts:
            artifacts.append(item)
        return self.update(work_id, artifacts=artifacts)

    def finish(self, work_id: str, status: str, error: str | None = None, summary: str | None = None) -> dict | None:
        fields = {
            "status": status,
            "finished_at": now_ts(),
            "heartbeat_at": now_ts(),
            "heartbeat_epoch": now_epoch(),
            "error": error,
        }
        if summary is not None:
            fields["summary"] = summary
        return self.update(work_id, **fields)

    def append_event(self, work_id: str, event: dict) -> None:
        with self._lock:
            path = self.events_path(work_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            item = dict(event)
            item.setdefault("ts", now_ts())
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=True) + "\n")

    def read_events(self, work_id: str, tail: int | None = None) -> list[dict]:
        path = self.events_path(work_id)
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows[-tail:] if tail else rows

    def read_log_tail(self, work_id: str, stream: str = "stdout", max_chars: int = 8000) -> str:
        path = self.stdout_path(work_id) if stream == "stdout" else self.stderr_path(work_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]

    def mark_stale(self) -> None:
        now = now_epoch()
        changed = False
        for path in self.root.glob("*/meta.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("status") not in ACTIVE_STATUSES:
                continue
            last = meta.get("heartbeat_epoch") or meta.get("created_at_epoch") or now
            if now - float(last) < self.stale_after_s:
                continue
            meta["status"] = "stale"
            meta["error"] = f"heartbeat stale for {int(now - float(last))}s"
            meta["finished_at"] = now_ts()
            self._write_meta(meta)
            self.append_event(meta["work_id"], {"type": "stale", "error": meta["error"]})
            changed = True
        if changed:
            self._write_state_index()

    def _read_meta(self, work_id: str) -> dict | None:
        path = self.meta_path(work_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_meta(self, meta: dict) -> None:
        storage._atomic_write(
            self.meta_path(meta["work_id"]),
            json.dumps(meta, ensure_ascii=True, indent=2),
        )

    def _write_state_index(self) -> None:
        items = []
        for path in self.root.glob("*/meta.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append(meta)
        items.sort(key=lambda item: item.get("created_at_epoch") or 0, reverse=True)
        storage._atomic_write(
            self.state_file,
            json.dumps(items, ensure_ascii=True, indent=2),
        )

    @staticmethod
    def _prefix_for(title: str) -> str:
        raw = "".join(ch.lower() if ch.isalnum() else "-" for ch in title.strip())
        raw = "-".join(part for part in raw.split("-") if part)
        return (raw or "work")[:32]
