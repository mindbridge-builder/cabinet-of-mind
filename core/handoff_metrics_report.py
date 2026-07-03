"""Aggregate WorkRuntime handoff JSONL metrics."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            events.append(item)
    return events


def build_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    handoffs: dict[str, dict[str, Any]] = {}
    for event in events:
        handoff_id = str(event.get("handoff_id") or "").strip()
        if not handoff_id:
            continue
        record = handoffs.setdefault(handoff_id, {"handoff_id": handoff_id})
        record.update({k: v for k, v in event.items() if v is not None})
        if event.get("type") == "handoff_started":
            record["started"] = True
        elif event.get("type") == "handoff_finished":
            record["finished"] = True

    records = list(handoffs.values())
    total = len(records)
    unknown_cost = sum(1 for item in records if not item.get("estimated_direct_cost"))
    result_counts = Counter(str(item.get("result") or "unfinished") for item in records)
    cost_counts = Counter(str(item.get("estimated_direct_cost") or "unknown") for item in records)

    return {
        "total_handoffs": total,
        "clean": result_counts.get("clean", 0),
        "failed": result_counts.get("failed", 0),
        "rescued": result_counts.get("rescued", 0),
        "cancelled": result_counts.get("cancelled", 0),
        "unfinished": result_counts.get("unfinished", 0),
        "unknown_cost_count": unknown_cost,
        "unknown_cost_rate": (unknown_cost / total) if total else 0.0,
        "cost_distribution": dict(sorted(cost_counts.items())),
        "by_template": _group(records, "template_id"),
        "by_action": _group(records, "action_id"),
    }


def _group(records: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        buckets[str(item.get(field) or "unknown")].append(item)
    result: dict[str, dict[str, Any]] = {}
    for key, items in sorted(buckets.items()):
        result_counts = Counter(str(item.get("result") or "unfinished") for item in items)
        costs = Counter(str(item.get("estimated_direct_cost") or "unknown") for item in items)
        wall_times = [
            float(item["wall_time"])
            for item in items
            if isinstance(item.get("wall_time"), (int, float))
        ]
        unknown_cost = costs.get("unknown", 0)
        result[key] = {
            "total": len(items),
            "clean": result_counts.get("clean", 0),
            "failed": result_counts.get("failed", 0),
            "rescued": result_counts.get("rescued", 0),
            "cancelled": result_counts.get("cancelled", 0),
            "unfinished": result_counts.get("unfinished", 0),
            "unknown_cost_count": unknown_cost,
            "unknown_cost_rate": (unknown_cost / len(items)) if items else 0.0,
            "cost_distribution": dict(sorted(costs.items())),
            "wall_time_total": sum(wall_times),
            "wall_time_avg": (sum(wall_times) / len(wall_times)) if wall_times else None,
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate Cabinet handoff metrics JSONL.")
    parser.add_argument(
        "--metrics",
        default=".cabinet/metrics/handoffs.jsonl",
        help="Path to handoffs.jsonl",
    )
    parser.add_argument("--output", help="Optional path for report JSON")
    args = parser.parse_args(argv)

    report = build_report(load_events(Path(args.metrics)))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"type": "artifact", "path": args.output, "label": "handoff_metrics_report"}), flush=True)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
