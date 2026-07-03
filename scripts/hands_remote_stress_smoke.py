from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.ollama import OllamaAdapter
from core.work_runtime import WorkRuntime
from core.work_store import WorkStore


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _run_capture(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as exc:
        return {"cmd": cmd, "error": repr(exc)}


def _http_json(url: str, timeout: int = 10) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"error": repr(exc)}


def _ssh_cmd(args: argparse.Namespace, remote_command: str) -> list[str] | None:
    if not args.ssh_target:
        return None
    cmd = ["ssh"]
    if args.ssh_key:
        cmd += ["-i", args.ssh_key, "-o", "IdentitiesOnly=yes"]
    cmd += [args.ssh_target, remote_command]
    return cmd


def _capture_runtime_snapshot(args: argparse.Namespace, label: str) -> dict:
    snapshot = {
        "label": label,
        "ollama_api_ps": _http_json(args.endpoint.rstrip("/") + "/api/ps"),
    }
    gpu_cmd = _ssh_cmd(
        args,
        (
            'powershell -NoProfile -Command "'
            "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu "
            "--format=csv,noheader,nounits"
            '"'
        ),
    )
    if gpu_cmd:
        snapshot["nvidia_smi"] = _run_capture(gpu_cmd, timeout=30)
    return snapshot


def _source_sizes(count: int) -> list[int]:
    base = [250, 800, 1600, 3200, 5200, 6800, 9000, 12000, 16000, 20000]
    if count <= len(base):
        return base[:count]
    extra = [base[-1] + 4000 * idx for idx in range(1, count - len(base) + 1)]
    return base + extra


def _write_source_files(workspace: Path, count: int) -> list[str]:
    file_names: list[str] = []
    for idx, size in enumerate(_source_sizes(count), start=1):
        name = f"source_{idx:02d}.txt"
        file_names.append(name)
        chunk = f"{name}: " + ("x" * max(0, size - len(name) - 3))
        (workspace / name).write_text(chunk, encoding="utf-8")
    return file_names


def _write_direct_task(workspace: Path, file_names: list[str]) -> None:
    task = (
        "Read each source file with read_file, one file per tool call. "
        "Then write result.txt with one line per source file containing the file name and the observed size. "
        "Then commit result.txt with git_commit. "
        "Do not use bash_run for this task. Source files: "
        + ", ".join(file_names)
    )
    (workspace / "task.txt").write_text(task, encoding="utf-8")


def _write_direct_final_write_task(workspace: Path, file_names: list[str]) -> None:
    task = (
        "Read all source files with read_file, one file per tool call. "
        "Do not write or append result.txt until all source files have been read. "
        "After all reads are complete, call write_file exactly once to create result.txt with one line per "
        "source file containing the file name and observed size. "
        "Do not use append_file. Do not use bash_run. Then commit result.txt with git_commit. "
        "Source files: "
        + ", ".join(file_names)
    )
    (workspace / "task.txt").write_text(task, encoding="utf-8")


def _write_workruntime_task(workspace: Path, file_names: list[str]) -> None:
    worker = r'''
from __future__ import annotations

import json
import time
from pathlib import Path


def main() -> int:
    root = Path.cwd()
    names = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    rows = []
    total = len(names)
    for idx, name in enumerate(names, start=1):
        path = root / name
        text = path.read_text(encoding="utf-8", errors="replace")
        rows.append({"file": name, "chars": len(text), "bytes": path.stat().st_size})
        print(json.dumps({"type": "progress", "current": idx, "total": total}), flush=True)
        time.sleep(0.2)
    out = root / "work_result.json"
    out.write_text(json.dumps({"count": total, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"type": "artifact", "path": str(out), "label": "work result"}), flush=True)
    print(json.dumps({"type": "result", "summary": f"processed {total} files"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''.lstrip()
    (workspace / "worker.py").write_text(worker, encoding="utf-8")
    (workspace / "sources.json").write_text(json.dumps(file_names, ensure_ascii=False), encoding="utf-8")
    task = (
        "Use WorkRuntime for this task. Do not read source files directly. "
        "First start the worker with start_work using this exact argv command: [\"python\", \"worker.py\"]. "
        "Use cwd as the current workspace. Then poll work_status until the work status is succeeded. "
        "Then read work_result.json, write result.txt with one line per processed source file in the form "
        "'source_XX.txt: <chars>', and commit result.txt with git_commit. "
        "If the work fails, read work_logs and report the observed failure. "
        "Source count: "
        + str(len(file_names))
    )
    (workspace / "task.txt").write_text(task, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Remote Golem multi-step stress smoke.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11435")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--files", type=int, default=8)
    parser.add_argument(
        "--scenario",
        choices=["direct", "direct_guard", "direct_final_write", "workruntime"],
        default="direct",
    )
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--ssh-target", default="", help="Optional SSH target for remote nvidia-smi snapshots.")
    parser.add_argument("--ssh-key", default="", help="Optional SSH key for --ssh-target.")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary workspace after the run.")
    parser.add_argument("--workspace", default="", help="Use an explicit workspace path instead of a temp directory.")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve() if args.workspace else Path(tempfile.mkdtemp(prefix="golem_stress_"))
    if args.workspace:
        workspace.mkdir(parents=True, exist_ok=True)
        if any(workspace.iterdir()):
            raise SystemExit(f"workspace must be empty: {workspace}")
    passed = False
    try:
        _run(["git", "init"], workspace)
        _run(["git", "config", "user.email", "golem-stress@test.local"], workspace)
        _run(["git", "config", "user.name", "Golem Stress"], workspace)

        file_names = _write_source_files(workspace, args.files)
        if args.scenario == "workruntime":
            _write_workruntime_task(workspace, file_names)
            work_runtime = WorkRuntime(WorkStore(workspace / ".cabinet" / "work"))
        elif args.scenario == "direct_guard":
            _write_direct_task(workspace, file_names)
            work_runtime = WorkRuntime(WorkStore(workspace / ".cabinet" / "work"))
        elif args.scenario == "direct_final_write":
            _write_direct_final_write_task(workspace, file_names)
            work_runtime = None
        else:
            _write_direct_task(workspace, file_names)
            work_runtime = None

        runtime_before = _capture_runtime_snapshot(args, "before")

        adapter = OllamaAdapter(
            workspace=workspace,
            allowed_roots=[workspace],
            base_url=args.endpoint,
            work_runtime=work_runtime,
        )
        adapter.DEFAULT_OPTIONS = dict(adapter.DEFAULT_OPTIONS, num_ctx=args.num_ctx)
        result = adapter.call(
            system_prompt=(
                "You are Golem, the Cabinet hands agent. Use tools for every execution fact. "
                "Do not repeat an identical tool call after its result is available. "
                "When the requested files are read, write result.txt, commit it, and return concise Russian status with routing JSON."
            ),
            history=[],
            user_message="@gol execute the task in task.txt",
            timeout=args.timeout,
            allow_write_tools=True,
        )

        git_status = _run(["git", "status", "--short"], workspace)
        git_head = _run(["git", "rev-parse", "--short", "HEAD"], workspace)
        result_file = workspace / "result.txt"
        runtime_after = _capture_runtime_snapshot(args, "after")
        summary = {
            "workspace": str(workspace),
            "scenario": args.scenario,
            "source_count": len(file_names),
            "requested_num_ctx": args.num_ctx,
            "error": result.error,
            "elapsed": result.elapsed,
            "executed_tools": result.executed_tools,
            "duplicate_tool_loop": result.metrics.get("duplicate_tool_loop"),
            "stopped_reason": result.metrics.get("stopped_reason"),
            "partial_empty_response": result.metrics.get("partial_empty_response"),
            "workruntime_required": result.metrics.get("workruntime_required"),
            "num_ctx": result.metrics.get("num_ctx"),
            "max_prompt_context_ratio": result.metrics.get("max_prompt_context_ratio"),
            "context_pressure": result.metrics.get("context_pressure"),
            "ollama_steps": result.metrics.get("ollama_steps"),
            "tool_journal": result.metrics.get("tool_journal"),
            "observed_report": result.metrics.get("observed_report"),
            "result_exists": result_file.exists(),
            "result_size": result_file.stat().st_size if result_file.exists() else 0,
            "git_status": git_status.stdout.strip(),
            "git_head": git_head.stdout.strip() if git_head.returncode == 0 else "",
            "text_head": result.text[:2000],
            "runtime_snapshots": [runtime_before, runtime_after],
        }
        (workspace / "stress_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

        passed = (
            result.error is None
            and not result.metrics.get("duplicate_tool_loop")
            and not result.metrics.get("context_pressure")
            and result_file.exists()
            and any(str(t).startswith("git_commit:success") for t in result.executed_tools)
        )
        if args.scenario == "workruntime":
            passed = passed and any(str(t).startswith("start_work") for t in result.executed_tools)
        elif args.scenario == "direct_guard":
            guard_engaged = (
                result.metrics.get("workruntime_required")
                or any(str(t).startswith(("start_work", "run_work")) for t in result.executed_tools)
            )
            passed = (
                result.error is None
                and guard_engaged
                and not result.metrics.get("partial_empty_response")
                and not result.metrics.get("context_pressure")
            )
        elif args.scenario == "direct_final_write":
            passed = (
                result.error is None
                and result.metrics.get("workruntime_required")
                and result.metrics.get("stopped_reason") == "workruntime_required"
                and not result.metrics.get("partial_empty_response")
                and len(result.executed_tools) <= 1
            )
        return 0 if passed else 1
    finally:
        if args.workspace or args.keep or not passed:
            pass
        else:
            try:
                shutil.rmtree(workspace)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
