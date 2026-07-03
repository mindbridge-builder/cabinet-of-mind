"""Validators for Cabinet of Mind agent responses.

Catches phantom claims -- responses that assert completed actions
without corresponding tool calls.

Usage:
    warnings = check_phantom_claims(result.text, result.executed_tools)
    if warnings:
        # log or broadcast warning
"""
from __future__ import annotations

import re
from typing import Any

# Patterns that imply a file was written/modified.
_WRITE_CLAIM_RE = re.compile(
    r"(?i)(wrote|written|changed|updated|created|added|rewrote|"
    r"made edits|saved|patch applied|made changes|"
    r"implemented|applied|enabled|configured|set up|fixed)\b.{0,80}"
    r"(file|code|function|html|index|\.py|\.json|\.md|resize|style)"
)

# Patterns that imply a git commit was made.
_COMMIT_CLAIM_RE = re.compile(
    r"(?i)(committed|made a commit|commits? (made|ready|went through|done)|"
    r"git commit|git add.*git commit|commit.*went through)"
)

# Commit/hash claims, including structured lines such as:
#   commit: a964532
#   hash: a964532
_COMMIT_HASH_RE = re.compile(
    r"(?i)(?:\bcommit\b|\bhash\b)[^\n`]{0,40}\b([0-9a-f]{7,40})\b"
)

# Structured or terse final reports that imply workspace work was completed.
_WORK_REPORT_RE = re.compile(
    r"(?i)("
    r"files_changed\s*:|verification\s*:|"
    r"commit\s*:\s*(?!none\b)(?!n/a\b)(?!-\b)|"
    r"commit\s+hash|"
    r"fixed\s+everything|"
    r"fixed\s+the\s+bugs|"
    r"committed|"
    r"wired\s+up|"
    r"polished\s+up|"
    r"rolled\s+back|"
    r"removed\s+from\s+the\s+index"
    r")"
)

# Tools that actually write files (OllamaAdapter names).
_OLLAMA_WRITE_TOOLS = {"write_file", "replace_text", "delete_path", "start_work", "run_work"}

# Tools that can run git (ClaudeCodeAdapter / CodexAdapter via Bash/shell).
_BASH_TOOLS = {"bash", "Bash", "shell_command", "Shell", "functions.shell_command"}

_SELF_REPORT_LINE_RE = re.compile(
    r"(?im)^\s*(files_changed|commit|verification|work_id|exit_code)\s*:.*(?:\n|$)"
)


def check_phantom_claims(
    text: str,
    executed_tools: list[str],
    strict_no_tools: bool = False,
) -> list[str]:
    """Return list of warning strings for phantom claims in *text*.

    A phantom claim is a statement that a mutating action was completed
    when no corresponding tool was actually executed.

    Claude Code CLI and Codex CLI do not expose tool call lists in --print
    mode, so executed_tools is always [] for those adapters. Keep the default
    permissive for them. Use strict_no_tools for adapters like Golem/Ollama
    where an empty tool list is itself meaningful.

    Args:
        text: agent's final response text.
        executed_tools: list of tool names actually called (from CallResult).
        strict_no_tools: when true, completed-work claims with no tools are
            reported instead of skipped.

    Returns:
        List of warning strings. Empty list = no phantoms detected.
    """
    if not executed_tools and not strict_no_tools:
        return []
    warnings: list[str] = []
    tools_lower = {t.lower() for t in executed_tools}

    if strict_no_tools and not executed_tools:
        if (
            _WORK_REPORT_RE.search(text)
            or _WRITE_CLAIM_RE.search(text)
            or _COMMIT_CLAIM_RE.search(text)
            or _COMMIT_HASH_RE.search(text)
        ):
            warnings.append(
                "phantom_no_tools: claims completed workspace work "
                "but executed_tools=[]"
            )

    # Phantom commit: claimed git operation but no successful git tool ran.
    if _COMMIT_CLAIM_RE.search(text):
        has_bash = bool(tools_lower & {t.lower() for t in _BASH_TOOLS})
        has_successful_git_commit = any(t.lower().startswith("git_commit:success") for t in executed_tools)
        if not has_bash and not has_successful_git_commit:
            match = _COMMIT_CLAIM_RE.search(text)
            snippet = (match.group(0) if match else "")[:80]
            warnings.append(
                f"phantom_commit: claims git operation ('{snippet}') "
                f"but executed_tools={executed_tools!r} — no successful git tool ran"
            )

    # Phantom commit hash: claimed a specific hash that git_commit did not
    # report as the new commit. This catches old-hash fabrication after failed
    # git add/commit attempts.
    claimed_hashes = {m.group(1).lower() for m in _COMMIT_HASH_RE.finditer(text)}
    if claimed_hashes:
        successful_hashes = {
            t.split(":", 2)[2].lower()
            for t in executed_tools
            if t.lower().startswith("git_commit:success:") and len(t.split(":", 2)) == 3
        }
        missing_hashes = claimed_hashes - successful_hashes
        if missing_hashes:
            warnings.append(
                f"phantom_commit_hash: claims commit hash(es) {sorted(missing_hashes)!r} "
                f"but executed_tools={executed_tools!r} did not produce them"
            )

    # Phantom write: claimed file modification but no write tool ran.
    if _WRITE_CLAIM_RE.search(text):
        # write_file:unchanged means the tool ran but content was identical —
        # still a phantom claim because no actual mutation happened.
        effective_write_tools = {t for t in executed_tools if t != "write_file:unchanged"}
        effective_lower = {t.lower() for t in effective_write_tools}
        has_write = bool(
            effective_lower & {t.lower() for t in _OLLAMA_WRITE_TOOLS}
            | effective_lower & {"write", "edit", "bash", "shell_command", "apply_patch"}
        )
        if not has_write:
            match = _WRITE_CLAIM_RE.search(text)
            snippet = (match.group(0) if match else "")[:80]
            warnings.append(
                f"phantom_write: claims file modification ('{snippet}') "
                f"but executed_tools={executed_tools!r} — no write tool ran"
            )

    return warnings


def strip_self_report_facts(text: str) -> str:
    """Remove agent-authored fact lines that Cabinet replaces with observations."""
    return _SELF_REPORT_LINE_RE.sub("", text).strip()


def _short(value: Any, limit: int = 220) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_observed_report(tool_journal: list[dict], context: dict | None = None) -> dict:
    """Summarize observed execution facts from Golem's tool journal.

    This is deliberately based on tool results, not on the model's final text.
    """
    files_changed: list[str] = []
    file_noops: list[str] = []
    commits: list[str] = []
    commands: list[str] = []
    verification: list[str] = []
    work: list[str] = []
    errors: list[str] = []

    for event in tool_journal:
        name = event.get("tool") or ""
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        status = event.get("status")

        if result.get("error"):
            errors.append(f"{name}: {_short(result.get('error'))}")
        elif status == "error" and event.get("error"):
            errors.append(f"{name}: {_short(event.get('error'))}")

        # Write-ahead journal: tool started but impl raised before recording
        # a result, or impl never reached a terminal state at all. The file
        # may have been partially mutated, so do NOT mark it as a benign
        # no-op — surface as an attempted mutation.
        if status in ("error", "started") and name in {
            "write_file", "append_file", "replace_text", "delete_path"
        }:
            path = args.get("path") if isinstance(args, dict) else None
            if path:
                files_changed.append(f"{path} (attempted, status={status})")
            if status == "started":
                errors.append(f"{name}: incomplete (no terminal status)")
            continue

        # git_commit with a non-terminal or error status: surface explicitly
        # in commits, never silently as 'none'.
        if status in ("error", "started") and name == "git_commit":
            if status == "error":
                msg = event.get("error") or "unknown error"
                commits.append(f"attempted (status=error: {_short(msg, 80)})")
            else:
                commits.append("attempted (status=started, incomplete)")
                errors.append("git_commit: incomplete (no terminal status)")
            continue

        if name == "write_file":
            path = result.get("path") or args.get("path")
            if result.get("changed") and path:
                files_changed.append(str(path))
            elif path:
                file_noops.append(f"{path} unchanged")
        elif name == "append_file":
            path = result.get("path") or args.get("path")
            if path and not result.get("error"):
                files_changed.append(str(path))
        elif name == "replace_text":
            path = result.get("path") or args.get("path")
            replaced = int(result.get("replaced") or 0)
            if replaced > 0 and path:
                files_changed.append(f"{path} ({replaced} replacements)")
            elif path:
                file_noops.append(f"{path} no replacements")
        elif name == "delete_path":
            path = result.get("path") or args.get("path")
            if result.get("deleted") and path:
                files_changed.append(f"{path} deleted")
        elif name == "git_commit":
            if result.get("success") and result.get("commit"):
                commits.append(str(result.get("commit")))
            elif result.get("success") and result.get("no_change"):
                commits.append("none (nothing to commit)")
            elif result.get("success"):
                commits.append("success (hash not reported)")
        elif name == "bash_run":
            rc = result.get("returncode")
            cmd = args.get("command") or "bash_run"
            commands.append(f"{_short(cmd, 120)} -> rc={rc}")
            cmd_lower = str(cmd).lower()
            if any(token in cmd_lower for token in ("test", "pytest", "unittest")):
                verification.append(f"{_short(cmd, 120)} -> rc={rc}")
        elif name in {"start_work", "run_work"}:
            wid = result.get("work_id")
            status = result.get("status")
            if wid:
                work.append(f"{wid} status={status}")
        elif name == "work_status":
            meta = result.get("work") if isinstance(result.get("work"), dict) else {}
            wid = result.get("work_id") or meta.get("work_id")
            status = meta.get("status") or result.get("status")
            if wid:
                work.append(f"{wid} status={status}")
        elif name == "work_logs":
            wid = result.get("work_id")
            status = result.get("status")
            if wid:
                work.append(f"{wid} logs_read status={status}")

    report = {
        "files_changed": sorted(set(files_changed)),
        "file_noops": sorted(set(file_noops)),
        "commits": commits,
        "commands": commands,
        "verification": verification,
        "work": work,
        "errors": errors,
        "tool_count": len(tool_journal),
    }
    if context:
        report["context"] = context
    return report


def format_observed_report(report: dict) -> str:
    """Render Cabinet's observed facts in the structured report slot."""
    files = report.get("files_changed") or []
    noops = report.get("file_noops") or []
    commits = report.get("commits") or []
    verification = report.get("verification") or []
    commands = report.get("commands") or []
    work = report.get("work") or []
    errors = report.get("errors") or []
    context = report.get("context") if isinstance(report.get("context"), dict) else {}

    lines = [
        "CABINET OBSERVED REPORT",
        f"files_changed: {', '.join(files) if files else 'none observed'}",
        f"commit: {', '.join(commits) if commits else 'none observed'}",
        f"verification: {', '.join(verification) if verification else 'none observed'}",
    ]
    if work:
        lines.append(f"work: {', '.join(work)}")
    if commands and not verification:
        lines.append(f"commands: {', '.join(commands[-3:])}")
    if noops:
        lines.append(f"file_noops: {', '.join(noops)}")
    if errors:
        lines.append(f"errors: {', '.join(errors)}")
    if context.get("context_pressure"):
        ratio = context.get("max_prompt_context_ratio")
        ratio_text = f"{ratio:.2f}" if isinstance(ratio, (int, float)) else "unknown"
        lines.append(f"context_pressure: true (max_prompt_context_ratio={ratio_text})")
    lines.append(f"tool_count: {report.get('tool_count', 0)}")
    return "\n".join(lines)
