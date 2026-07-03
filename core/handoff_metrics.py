"""Structured metrics for sage-to-executor handoffs."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from core.work_store import now_epoch, now_ts


DIRECT_COST_VALUES = {"small", "medium", "large"}
RESULT_VALUES = {"clean", "failed", "rescued", "cancelled"}
TOKEN_FIELDS = {
    "tokens_sage_pre",
    "tokens_sage_verify",
    "tokens_sage_rescue",
}


def make_handoff_id(prefix: str = "handoff") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def normalize_handoff(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a compact handoff contract, or None when no handoff was supplied."""
    if not isinstance(raw, dict) or not raw:
        return None
    item = dict(raw)
    item["handoff_id"] = str(item.get("handoff_id") or make_handoff_id()).strip()
    item["template_id"] = str(item.get("template_id") or "manual_workruntime").strip()
    if item.get("action_id") is not None:
        item["action_id"] = str(item.get("action_id")).strip()
    if item.get("expected_files_changed") is None:
        item["expected_files_changed"] = []
    elif not isinstance(item.get("expected_files_changed"), list):
        item["expected_files_changed"] = [str(item.get("expected_files_changed"))]
    cost = item.get("estimated_direct_cost")
    if cost is not None:
        cost = str(cost).strip().lower()
        if cost not in DIRECT_COST_VALUES:
            raise ValueError("estimated_direct_cost must be small, medium, or large")
        item["estimated_direct_cost"] = cost
    for field in TOKEN_FIELDS:
        if item.get(field) is None:
            continue
        try:
            item[field] = int(item[field])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc
    return item


class HandoffMetricsStore:
    """Append-only JSONL store for WorkRuntime handoff accounting."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "handoffs.jsonl"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            item = dict(event)
            item.setdefault("ts", now_ts())
            item.setdefault("ts_epoch", now_epoch())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=True) + "\n")

    def record_started(self, handoff: dict[str, Any], work_meta: dict[str, Any]) -> None:
        self.append({
            "type": "handoff_started",
            "handoff_id": handoff["handoff_id"],
            "template_id": handoff.get("template_id"),
            "action_id": handoff.get("action_id"),
            "task_ref": handoff.get("task_ref"),
            "expected_files_changed": handoff.get("expected_files_changed", []),
            "started_by": handoff.get("started_by") or work_meta.get("owner"),
            "executor": handoff.get("executor") or work_meta.get("executor"),
            "work_id": work_meta.get("work_id"),
            "created_from": work_meta.get("created_from"),
            "tokens_sage_pre": handoff.get("tokens_sage_pre"),
            "estimated_direct_cost": handoff.get("estimated_direct_cost"),
        })

    def record_finished(
        self,
        handoff: dict[str, Any],
        work_meta: dict[str, Any],
        *,
        result: str,
        followup_actor: str | None = None,
        followup_same_handoff: bool | None = None,
    ) -> None:
        if result not in RESULT_VALUES:
            raise ValueError(f"invalid handoff result: {result}")
        started = work_meta.get("created_at_epoch")
        finished = work_meta.get("heartbeat_epoch") or now_epoch()
        wall_time = None
        if started is not None:
            try:
                wall_time = max(0.0, float(finished) - float(started))
            except (TypeError, ValueError):
                wall_time = None
        self.append({
            "type": "handoff_finished",
            "handoff_id": handoff["handoff_id"],
            "template_id": handoff.get("template_id"),
            "action_id": handoff.get("action_id"),
            "task_ref": handoff.get("task_ref"),
            "work_id": work_meta.get("work_id"),
            "result": result,
            "followup_actor": followup_actor,
            "followup_same_handoff": followup_same_handoff,
            "tokens_sage_verify": handoff.get("tokens_sage_verify"),
            "tokens_sage_rescue": handoff.get("tokens_sage_rescue"),
            "wall_time": wall_time,
            "status": work_meta.get("status"),
            "error": work_meta.get("error"),
        })


def default_metrics_store_for_work_root(work_root: Path) -> HandoffMetricsStore:
    root = Path(work_root)
    if root.name == "work" and root.parent.name == ".cabinet":
        metrics_root = root.parent / "metrics"
    else:
        metrics_root = root.parent / ".cabinet" / "metrics"
    return HandoffMetricsStore(metrics_root)
