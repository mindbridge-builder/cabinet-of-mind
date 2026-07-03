"""Background process runtime for Cabinet work items."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from core.handoff_metrics import (
    HandoffMetricsStore,
    default_metrics_store_for_work_root,
    normalize_handoff,
)
from core.work_store import TERMINAL_STATUSES, WorkStore


class WorkRuntime:
    def __init__(
        self,
        store: WorkStore,
        broadcaster: Callable[[dict], None] | None = None,
        metrics_store: HandoffMetricsStore | None = None,
    ):
        self.store = store
        self.broadcaster = broadcaster or (lambda payload: None)
        self.metrics_store = metrics_store or default_metrics_store_for_work_root(store.root)
        self._lock = threading.RLock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelling: set[str] = set()

    def start_process(
        self,
        *,
        title: str,
        command: list[str],
        cwd: str | Path,
        owner: str = "system",
        executor: str = "local_process",
        created_from: str | None = None,
        env: dict[str, str] | None = None,
        handoff: dict | None = None,
    ) -> dict:
        cwd_path = Path(cwd)
        handoff = normalize_handoff(handoff)
        meta = self.store.create(
            title=title,
            owner=owner,
            executor=executor,
            created_from=created_from,
            command=command,
            cwd=str(cwd_path),
            handoff=handoff,
        )
        work_id = meta["work_id"]
        if handoff:
            self.store.append_event(work_id, {
                "type": "handoff_started",
                "handoff_id": handoff["handoff_id"],
                "template_id": handoff.get("template_id"),
                "action_id": handoff.get("action_id"),
            })
            self.metrics_store.record_started(handoff, meta)
        stdout_file = open(self.store.stdout_path(work_id), "a", encoding="utf-8", buffering=1)
        stderr_file = open(self.store.stderr_path(work_id), "a", encoding="utf-8", buffering=1)
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd_path),
                env={**os.environ, **(env or {})},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            stdout_file.close()
            stderr_file.close()
            self.store.append_event(work_id, {"type": "failed_to_start", "error": str(exc)})
            self.store.finish(work_id, "failed", error=str(exc))
            self._record_handoff_finished(work_id, "failed")
            self._broadcast_update(work_id)
            return self.store.get(work_id) or meta

        with self._lock:
            self._procs[work_id] = proc
        self.store.mark_running(work_id, pid=proc.pid)
        self.store.append_event(work_id, {"type": "started", "pid": proc.pid, "command": command})
        self._broadcast_update(work_id)

        stdout_thread = threading.Thread(
            target=self._read_stream,
            args=(work_id, proc.stdout, stdout_file, "stdout"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stream,
            args=(work_id, proc.stderr, stderr_file, "stderr"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        threading.Thread(
            target=self._heartbeat_process,
            args=(work_id, proc),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._wait_process,
            args=(work_id, proc, [stdout_thread, stderr_thread]),
            daemon=True,
        ).start()
        return self.store.get(work_id) or meta

    def cancel(self, work_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(work_id)
        meta = self.store.get(work_id)
        if not meta or meta.get("status") in TERMINAL_STATUSES:
            return False
        with self._lock:
            self._cancelling.add(work_id)
        if proc and proc.poll() is None:
            proc.terminate()
            self.store.append_event(work_id, {"type": "cancel_requested", "pid": proc.pid})
        else:
            self.store.append_event(work_id, {"type": "cancel_requested", "pid": meta.get("pid")})
        self.store.finish(work_id, "cancelled", error="cancelled by user")
        self._record_handoff_finished(work_id, "cancelled")
        self._broadcast_update(work_id)
        return True

    def _read_stream(self, work_id: str, stream, target_file, stream_name: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                target_file.write(line)
                self._handle_line(work_id, line, stream_name)
            stream.close()
        finally:
            target_file.close()

    def _heartbeat_process(self, work_id: str, proc: subprocess.Popen) -> None:
        while proc.poll() is None:
            self.store.heartbeat(work_id)
            self._broadcast_update(work_id)
            time.sleep(15)

    def _handle_line(self, work_id: str, line: str, stream_name: str) -> None:
        self.store.heartbeat(work_id)
        stripped = line.strip()
        if not stripped:
            return
        event = self._parse_progress_line(stripped)
        if event:
            self._apply_event(work_id, event)
            return
        self.store.append_event(work_id, {"type": stream_name, "text": stripped[:1000]})
        self._broadcast_update(work_id)

    def _apply_event(self, work_id: str, event: dict) -> None:
        event_type = event.get("type") or "event"
        self.store.append_event(work_id, event)
        if event_type == "progress":
            progress = {k: v for k, v in event.items() if k != "type"}
            self.store.heartbeat(work_id, progress=progress)
        elif event_type == "artifact":
            path = str(event.get("path") or "")
            if path:
                self.store.add_artifact(work_id, path, event.get("label"))
            self.store.heartbeat(work_id)
        elif event_type == "result":
            summary = event.get("summary")
            fields = {}
            if summary:
                fields["summary"] = str(summary)
            if fields:
                self.store.update(work_id, **fields)
            self.store.heartbeat(work_id)
        else:
            self.store.heartbeat(work_id)
        self._broadcast_update(work_id)

    def _wait_process(self, work_id: str, proc: subprocess.Popen, reader_threads: list[threading.Thread]) -> None:
        rc = proc.wait()
        for thread in reader_threads:
            thread.join(timeout=2)
        with self._lock:
            self._procs.pop(work_id, None)
            cancelling = work_id in self._cancelling
        meta = self.store.get(work_id)
        if cancelling or (meta and meta.get("status") == "cancelled"):
            if meta and meta.get("status") != "cancelled":
                self.store.finish(work_id, "cancelled", error="cancelled by user")
                self._record_handoff_finished(work_id, "cancelled")
            with self._lock:
                self._cancelling.discard(work_id)
            self._broadcast_update(work_id)
            return
        if rc == 0:
            summary = (self.store.get(work_id) or {}).get("summary")
            self.store.append_event(work_id, {"type": "finished", "returncode": rc})
            self.store.finish(work_id, "succeeded", summary=summary)
            self._record_handoff_finished(work_id, "clean")
        else:
            self.store.append_event(work_id, {"type": "failed", "returncode": rc})
            self.store.finish(work_id, "failed", error=f"process exited {rc}")
            self._record_handoff_finished(work_id, "failed")
        self._broadcast_update(work_id)

    def _record_handoff_finished(self, work_id: str, result: str) -> None:
        meta = self.store.get(work_id)
        handoff = (meta or {}).get("handoff")
        if not isinstance(handoff, dict):
            return
        self.store.append_event(work_id, {
            "type": "handoff_finished",
            "handoff_id": handoff.get("handoff_id"),
            "result": result,
        })
        self.metrics_store.record_finished(handoff, meta or {}, result=result)

    def _broadcast_update(self, work_id: str) -> None:
        meta = self.store.get(work_id)
        if meta:
            self.broadcaster({"type": "work_update", "work": meta})

    @staticmethod
    def _parse_progress_line(line: str) -> dict | None:
        if not line.startswith("{") or not line.endswith("}"):
            return None
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(item, dict):
            return None
        if item.get("type") in {"started", "progress", "artifact", "result", "finished"}:
            return item
        return None
