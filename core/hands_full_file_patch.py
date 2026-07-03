"""Production known-files patch harness for Golem."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable

from core.hands_delegation import KnownFilesPatchDelegation


class GolemPatchHarnessError(ValueError):
    """Raised when a patch violates the known-files contract."""


@dataclass(frozen=True)
class SearchReplaceBlock:
    file_path: str
    search: str
    replace: str


@dataclass(frozen=True)
class VerificationResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


@dataclass
class KnownFilesPatchResult:
    status: str
    reason: str
    changed_files: list[str] = field(default_factory=list)
    commit: str | None = None
    verification: VerificationResult | None = None
    escalation_path: str | None = None
    raw_output_path: str | None = None
    repair_raw_output_path: str | None = None
    attempt: str = "initial"
    model_prompt_tokens: int | None = None
    model_eval_tokens: int | None = None
    model_eval_tokens_max: int | None = None
    model_num_ctx: int | None = None
    model_done_reason: str | None = None
    model_context_total_tokens: int | None = None
    model_context_shift_suspected: bool | None = None
    attempts: list = field(default_factory=list)
    attempts_used: int = 0

    @property
    def auto_committed(self) -> bool:
        return self.status == "auto_committed"


FILE_START = "=== FILE: "
FILE_END = "=== END FILE ==="
PATCH_START = "=== PATCH: "
SEARCH_START = "<<<<<<< SEARCH"
SEARCH_SEPARATOR = "======="
REPLACE_END = ">>>>>>> REPLACE"
SEARCH_REPLACE_MAGIC_LINES = {
    SEARCH_START,
    SEARCH_SEPARATOR,
    REPLACE_END,
}
PROTECTED_PREFIXES = (
    "prompts/",
    "roles/",
    "security/",
)
PROTECTED_EXACT = {
    "ROUTING_CONTRACT.json",
    "ROUTING_CONTRACT.md",
}
FORBIDDEN_WRAPPERS = ("```", "<complete file content>", "</complete>")
PROMPT_TOKEN_BUDGET = 7500
TOKEN_CHARS_PER_TOKEN = 3.5


def parse_full_file_response(raw_text: str, expected_files: list[str]) -> dict[str, str]:
    expected = [_normalize_relative_path(path) for path in expected_files]
    _validate_expected_files(expected)
    _reject_known_wrappers(raw_text)

    blocks = _parse_blocks(raw_text)
    paths = [path for path, _content in blocks]
    duplicate_paths = {path for path in paths if paths.count(path) > 1}
    if duplicate_paths:
        raise GolemPatchHarnessError(
            f"duplicate file block(s): {', '.join(sorted(duplicate_paths))}"
        )

    unexpected = sorted(set(paths) - set(expected))
    if unexpected:
        raise GolemPatchHarnessError(f"unexpected file block(s): {', '.join(unexpected)}")
    missing = sorted(set(expected) - set(paths))
    if missing:
        raise GolemPatchHarnessError(f"missing file block(s): {', '.join(missing)}")
    return dict(blocks)


def parse_search_replace_response(
    raw_text: str,
    expected_files: list[str],
) -> dict[str, list[SearchReplaceBlock]]:
    expected = [_normalize_relative_path(path) for path in expected_files]
    _validate_expected_files(expected)
    _reject_known_wrappers(raw_text)

    blocks = _parse_search_replace_blocks(raw_text)
    paths = sorted({block.file_path for block in blocks})
    unexpected = sorted(set(paths) - set(expected))
    if unexpected:
        raise GolemPatchHarnessError(f"unexpected patch block(s): {', '.join(unexpected)}")
    missing = sorted(set(expected) - set(paths))
    if missing:
        raise GolemPatchHarnessError(f"missing patch block(s): {', '.join(missing)}")

    grouped: dict[str, list[SearchReplaceBlock]] = {path: [] for path in expected}
    for block in blocks:
        grouped[block.file_path].append(block)
    return grouped


def apply_search_replace_blocks(
    originals: dict[str, str],
    patches: dict[str, list[SearchReplaceBlock]],
) -> dict[str, str]:
    replacements = dict(originals)
    for file_path, blocks in patches.items():
        if file_path not in replacements:
            raise GolemPatchHarnessError(f"patch target has no original: {file_path}")
        content = replacements[file_path]
        for index, block in enumerate(blocks, start=1):
            if block.search == "":
                raise GolemPatchHarnessError(f"empty search block for {file_path} #{index}")
            matches = content.count(block.search)
            if matches != 1:
                raise GolemPatchHarnessError(
                    f"search block for {file_path} #{index} matched {matches} time(s)"
                )
            content = content.replace(block.search, block.replace, 1)
        replacements[file_path] = content
    return replacements


def run_verification(workspace_root: Path, command: str, timeout: int = 300) -> VerificationResult:
    command = (command or "").strip()
    if not command:
        raise GolemPatchHarnessError("verification command is required")
    if os.name == "nt":
        run_args: str | list[str] = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
        shell = False
    else:
        run_args = command
        shell = True
    completed = subprocess.run(
        run_args,
        cwd=workspace_root,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
        shell=shell,
        encoding="utf-8",
        errors="replace",
    )
    return VerificationResult(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_known_files_patch(
    *,
    workspace_root: Path,
    delegation: KnownFilesPatchDelegation,
    generate: Callable[[str], dict],
    model: str,
    base_url: str,
    commit_message: str = "golem: apply known-files patch",
    output_dir: Path | None = None,
) -> KnownFilesPatchResult:
    root = workspace_root.resolve()
    output_dir = output_dir or root / ".cabinet" / "golem_patch"
    output_dir.mkdir(parents=True, exist_ok=True)

    expected_files = [_normalize_relative_path(path) for path in delegation.files]
    try:
        _validate_expected_files(expected_files)
        _reject_protected_paths(expected_files)
        _ensure_files_exist(root, expected_files)
        _reject_oversized_targets(root, expected_files)
    except GolemPatchHarnessError as exc:
        report = _write_escalation(
            root,
            output_dir,
            delegation,
            stage="preflight",
            reason=str(exc),
            model=model,
            base_url=base_url,
        )
        return KnownFilesPatchResult(
            status="escalated",
            reason=str(exc),
            escalation_path=report,
        )

    originals = _read_originals(root, expected_files)
    baseline_untracked = _git_untracked_files(root)
    initial = _attempt_patch(
        root=root,
        delegation=delegation,
        expected_files=expected_files,
        generate=generate,
        output_dir=output_dir,
        originals=originals,
        baseline_untracked=baseline_untracked,
        model=model,
        base_url=base_url,
        attempt="initial",
    )
    if initial.auto_committed:
        initial.commit = _commit_files(root, expected_files, commit_message)
        if initial.commit:
            initial.changed_files = _changed_files_after_commit(root, expected_files)
            return initial
        _restore_originals(root, originals)
        report = _write_escalation(
            root,
            output_dir,
            delegation,
            stage="commit",
            reason="git commit failed",
            raw_output_path=initial.raw_output_path,
            verification=initial.verification,
            model=model,
            base_url=base_url,
        )
        initial.status = "escalated"
        initial.reason = "git commit failed"
        initial.escalation_path = report
        return initial

    if initial.status != "needs_repair":
        _restore_originals(root, originals)
        return initial

    repair = _attempt_patch(
        root=root,
        delegation=delegation,
        expected_files=expected_files,
        generate=generate,
        output_dir=output_dir,
        originals=originals,
        baseline_untracked=baseline_untracked,
        model=model,
        base_url=base_url,
        attempt="repair",
        previous_verification=initial.verification,
        previous_raw_output_path=initial.raw_output_path,
    )
    if repair.auto_committed:
        repair.commit = _commit_files(root, expected_files, commit_message)
        if repair.commit:
            repair.changed_files = _changed_files_after_commit(root, expected_files)
            return repair
        _restore_originals(root, originals)
        report = _write_escalation(
            root,
            output_dir,
            delegation,
            stage="commit",
            reason="git commit failed after repair",
            raw_output_path=initial.raw_output_path,
            repair_raw_output_path=repair.raw_output_path,
            verification=repair.verification,
            model=model,
            base_url=base_url,
        )
        repair.status = "escalated"
        repair.reason = "git commit failed after repair"
        repair.escalation_path = report
        return repair

    _restore_originals(root, originals)
    return repair


def summarize_verification_failure(
    verification: VerificationResult,
    max_chars: int = 2000,
) -> str:
    """Capped retry feedback: failed test names + a short output tail.

    A full traceback inflates the next prompt past the input budget and kills
    the retry for the same reason v3 pilots failed - cap hard (sage review
    requirement #1).
    """
    output = "\n".join(
        part for part in (verification.stdout or "", verification.stderr or "") if part
    )
    failed = [
        line.strip()
        for line in output.splitlines()
        if line.lstrip().startswith("FAILED") or line.lstrip().startswith("ERROR")
    ]
    names = "\n".join(failed[:10])
    tail = output[-max_chars:]
    summary = f"exit code: {verification.returncode}\n"
    if names:
        summary += f"failed tests:\n{names}\n"
    summary += f"output tail:\n{tail}"
    return summary[: max_chars + 400]


def build_search_replace_prompt(
    root: Path,
    delegation: KnownFilesPatchDelegation,
    feedback: str | None = None,
) -> str:
    files = [_normalize_relative_path(path) for path in delegation.files]
    blocks = [
        f"Current {relative}:\n{(root / relative).read_text(encoding='utf-8', errors='replace')}"
        for relative in files
    ]
    contract = (
        f"{PATCH_START}<file path> ===\n"
        f"{SEARCH_START}\n"
        "<exact lines copied from the current file>\n"
        f"{SEARCH_SEPARATOR}\n"
        "<replacement lines>\n"
        f"{REPLACE_END}"
    )
    feedback_part = (
        f"\nPrevious attempt failed verification:\n{feedback}\n" if feedback else ""
    )
    return (
        "You are Golem inside the Cabinet search/replace patch harness.\n\n"
        "Return one or more patch blocks and nothing else.\n\n"
        f"Required block format:\n{contract}\n\n"
        "Hard rules:\n"
        "- SEARCH must be copied EXACTLY from the current file content below "
        "(same whitespace) and must match exactly once in that file.\n"
        "- Provide at least one patch block for EVERY listed file.\n"
        "- Blocks for the same file must not overlap and must appear in "
        "top-to-bottom file order.\n"
        "- Never put the marker lines themselves inside SEARCH or REPLACE payloads.\n"
        "- Keep patches minimal: do not rewrite unrelated code; never delete "
        "existing tests.\n"
        "- No Markdown fences, no commentary, no extra files.\n\n"
        f"Files: {', '.join(files)}\n\n"
        f"Scope:\n{delegation.scope.strip()}\n\n"
        f"Verification command:\n{delegation.verify.strip()}\n"
        f"{feedback_part}\n"
        + "\n\n".join(blocks)
    )


def run_search_replace_patch(
    *,
    workspace_root: Path,
    delegation: KnownFilesPatchDelegation,
    generate: Callable[[str], dict],
    model: str,
    base_url: str,
    commit_message: str = "golem: apply search/replace patch",
    output_dir: Path | None = None,
    max_attempts: int = 5,
) -> KnownFilesPatchResult:
    """v4 engine: short structured patches + retry-until-green.

    Free local attempts replace the paid first-try metric: up to max_attempts
    dice rolls (temp>0 lives in the adapter), pytest is the referee, the
    honesty stack is unchanged. Strategy per attempt is logged (sage review
    requirement #2): initial -> retry_feedback after a verify failure ->
    fresh after a format break or a failed feedback retry.
    """
    root = workspace_root.resolve()
    output_dir = output_dir or root / ".cabinet" / "golem_patch"
    output_dir.mkdir(parents=True, exist_ok=True)

    expected_files = [_normalize_relative_path(path) for path in delegation.files]
    try:
        _validate_expected_files(expected_files)
        _reject_protected_paths(expected_files)
        _ensure_files_exist(root, expected_files)
        _reject_oversized_targets(root, expected_files)
    except GolemPatchHarnessError as exc:
        report = _write_escalation(
            root, output_dir, delegation,
            stage="preflight", reason=str(exc), model=model, base_url=base_url,
        )
        return KnownFilesPatchResult(
            status="escalated", reason=str(exc), escalation_path=report,
        )

    originals = _read_originals(root, expected_files)
    baseline_untracked = _git_untracked_files(root)
    attempts_log: list[dict] = []
    generated_results: list[dict] = []
    raw_paths: list[str] = []
    feedback: str | None = None

    for n in range(1, max_attempts + 1):
        strategy = "initial" if n == 1 else ("retry_feedback" if feedback else "fresh")
        prompt = build_search_replace_prompt(
            root, delegation, feedback=feedback if strategy == "retry_feedback" else None,
        )
        generated = generate(prompt)
        generated_results.append(generated)
        raw_text = str(generated.get("content") or "")
        raw_path = _write_raw_output(output_dir, raw_text, f"try{n}")
        raw_paths.append(raw_path)

        try:
            patches = parse_search_replace_response(raw_text, expected_files)
            replacements = apply_search_replace_blocks(originals, patches)
            _check_test_preservation(root, replacements)
        except GolemPatchHarnessError as exc:
            attempts_log.append(
                {"n": n, "strategy": strategy, "result": f"format: {exc}"[:180]}
            )
            feedback = None
            continue

        _write_replacements(root, replacements)
        gate_error = _post_apply_contract_error(
            root, expected_files, delegation.max_diff_lines, baseline_untracked,
        )
        if gate_error:
            attempts_log.append(
                {"n": n, "strategy": strategy, "result": f"contract: {gate_error}"[:180]}
            )
            _restore_originals(root, originals)
            feedback = None
            continue

        _remove_python_caches(root)
        verification = run_verification(root, delegation.verify)
        if verification.passed:
            attempts_log.append({"n": n, "strategy": strategy, "result": "pass"})
            commit = _commit_files(root, expected_files, commit_message)
            if commit:
                return KnownFilesPatchResult(
                    status="auto_committed",
                    reason="verification passed",
                    commit=commit,
                    changed_files=_changed_files_after_commit(root, expected_files),
                    verification=verification,
                    raw_output_path=raw_path,
                    attempt=strategy,
                    attempts=attempts_log,
                    attempts_used=n,
                    **_combined_model_metric_fields(generated_results),
                )
            _restore_originals(root, originals)
            report = _write_escalation(
                root, output_dir, delegation,
                stage="commit", reason="git commit failed",
                raw_output_path=raw_path, verification=verification,
                model=model, base_url=base_url,
            )
            return KnownFilesPatchResult(
                status="escalated", reason="git commit failed",
                escalation_path=report, raw_output_path=raw_path,
                attempt=strategy, attempts=attempts_log, attempts_used=n,
                **_combined_model_metric_fields(generated_results),
            )

        attempts_log.append({"n": n, "strategy": strategy, "result": "verify_fail"})
        _restore_originals(root, originals)
        # Alternate strategies: a failed feedback retry falls back to fresh
        # dice; a fresh/initial verify failure earns one capped-feedback shot.
        feedback = None if strategy == "retry_feedback" else summarize_verification_failure(verification)

    reason = f"no green within {max_attempts} attempts"
    report = _write_escalation(
        root, output_dir, delegation,
        stage="retry_budget", reason=reason,
        raw_output_path=raw_paths[-1] if raw_paths else None,
        model=model, base_url=base_url,
    )
    return KnownFilesPatchResult(
        status="escalated", reason=reason, escalation_path=report,
        raw_output_path=raw_paths[-1] if raw_paths else None,
        attempt="exhausted", attempts=attempts_log, attempts_used=max_attempts,
        **_combined_model_metric_fields(generated_results),
    )


def build_generation_prompt(
    root: Path,
    delegation: KnownFilesPatchDelegation,
    target_file: str | None = None,
) -> str:
    files = [_normalize_relative_path(target_file)] if target_file else [
        _normalize_relative_path(path) for path in delegation.files
    ]
    blocks = []
    for relative in files:
        blocks.append(
            f"Current {relative}:\n{(root / relative).read_text(encoding='utf-8', errors='replace')}"
        )
    output_contract = "\n".join(
        [
            f"=== FILE: {relative} ===\n<complete content of {relative}>\n=== END FILE ==="
            for relative in files
        ]
    )
    return (
        "You are Golem inside the Cabinet known-files full-file patch harness.\n\n"
        "Return exactly one complete file block and nothing else.\n\n"
        f"Required output format:\n{output_contract}\n\n"
        "Hard rules:\n"
        "- Do not use Markdown fences.\n"
        "- Do not write placeholder wrapper tags.\n"
        "- Do not omit existing functions, tests, imports, or comments unless the task requires it.\n"
        "- Do not add extra files.\n"
        "- Do not explain the change.\n\n"
        f"Scope:\n{delegation.scope.strip()}\n\n"
        f"Verification command supplied by sage:\n{delegation.verify.strip()}\n\n"
        + "\n\n".join(blocks)
    )


def build_repair_prompt(
    root: Path,
    delegation: KnownFilesPatchDelegation,
    verification: VerificationResult,
    target_file: str | None = None,
) -> str:
    failure = "\n".join(
        part
        for part in (
            f"exit code: {verification.returncode}",
            verification.stdout.strip(),
            verification.stderr.strip(),
        )
        if part
    )
    return (
        "You are repairing a Cabinet known-files full-file patch.\n\n"
        "Return exactly the same complete file blocks and nothing else. "
        "Fix the implementation and tests to satisfy the scope and verification failure.\n\n"
        f"Original scope:\n{delegation.scope.strip()}\n\n"
        f"Verification failure:\n{failure}\n\n"
        + build_generation_prompt(root, delegation, target_file=target_file)
    )


def count_test_functions(source: str) -> int:
    return sum(1 for line in source.splitlines() if line.startswith("def test_"))


def _attempt_patch(
    *,
    root: Path,
    delegation: KnownFilesPatchDelegation,
    expected_files: list[str],
    generate: Callable[[str], dict],
    output_dir: Path,
    originals: dict[str, str],
    baseline_untracked: set[str],
    model: str,
    base_url: str,
    attempt: str,
    previous_verification: VerificationResult | None = None,
    previous_raw_output_path: str | None = None,
) -> KnownFilesPatchResult:
    replacements: dict[str, str] = {}
    generated_results: list[dict] = []
    raw_output_paths: list[str] = []
    try:
        for target_file in expected_files:
            prompt = (
                build_repair_prompt(root, delegation, previous_verification, target_file=target_file)
                if previous_verification is not None
                else build_generation_prompt(root, delegation, target_file=target_file)
            )
            generated = generate(prompt)
            generated_results.append(generated)
            raw_text = str(generated.get("content") or "")
            raw_output_path = _write_raw_output(output_dir, raw_text, attempt, target_file=target_file)
            raw_output_paths.append(raw_output_path)
            replacements.update(parse_full_file_response(raw_text, [target_file]))
        _check_test_preservation(root, replacements)
    except GolemPatchHarnessError as exc:
        raw_output_path = raw_output_paths[-1] if raw_output_paths else None
        report = _write_escalation(
            root,
            output_dir,
            delegation,
            stage=f"{attempt}_format_gate",
            reason=str(exc),
            raw_output_path=previous_raw_output_path or raw_output_path,
            repair_raw_output_path=raw_output_path if attempt == "repair" else None,
            model=model,
            base_url=base_url,
        )
        return KnownFilesPatchResult(
            status="escalated",
            reason=str(exc),
            escalation_path=report,
            raw_output_path=previous_raw_output_path or raw_output_path,
            repair_raw_output_path=raw_output_path if attempt == "repair" else None,
            attempt=attempt,
            **_combined_model_metric_fields(generated_results),
        )

    _write_replacements(root, replacements)
    gate_error = _post_apply_contract_error(
        root,
        expected_files,
        delegation.max_diff_lines,
        baseline_untracked,
    )
    if gate_error:
        report = _write_escalation(
            root,
            output_dir,
            delegation,
            stage=f"{attempt}_contract",
            reason=gate_error,
            raw_output_path=previous_raw_output_path or raw_output_path,
            repair_raw_output_path=raw_output_path if attempt == "repair" else None,
            model=model,
            base_url=base_url,
        )
        _restore_originals(root, originals)
        return KnownFilesPatchResult(
            status="escalated",
            reason=gate_error,
            escalation_path=report,
            raw_output_path=previous_raw_output_path or raw_output_path,
            repair_raw_output_path=raw_output_path if attempt == "repair" else None,
            attempt=attempt,
            **_combined_model_metric_fields(generated_results),
        )

    _remove_python_caches(root)
    verification = run_verification(root, delegation.verify)
    if verification.passed:
        return KnownFilesPatchResult(
            status="auto_committed",
            reason="verification passed",
            verification=verification,
            raw_output_path=previous_raw_output_path or raw_output_path,
            repair_raw_output_path=raw_output_path if attempt == "repair" else None,
            attempt=attempt,
            **_combined_model_metric_fields(generated_results),
        )

    if attempt == "initial":
        return KnownFilesPatchResult(
            status="needs_repair",
            reason="verification failed; bounded repair allowed",
            verification=verification,
            raw_output_path=raw_output_path,
            attempt=attempt,
            **_combined_model_metric_fields(generated_results),
        )

    report = _write_escalation(
        root,
        output_dir,
        delegation,
        stage="repair_verify",
        reason="bounded repair failed verification",
        raw_output_path=previous_raw_output_path,
        repair_raw_output_path=raw_output_path,
        verification=verification,
        model=model,
        base_url=base_url,
    )
    return KnownFilesPatchResult(
        status="escalated",
        reason="bounded repair failed verification",
        verification=verification,
        escalation_path=report,
        raw_output_path=previous_raw_output_path,
        repair_raw_output_path=raw_output_path,
        attempt=attempt,
        **_combined_model_metric_fields(generated_results),
    )


def _model_metric_fields(generated: dict) -> dict:
    prompt_tokens = generated.get("prompt_eval_count")
    eval_tokens = generated.get("eval_count")
    num_ctx = generated.get("num_ctx")
    total_tokens = None
    shift_suspected = None
    if isinstance(prompt_tokens, int) and isinstance(eval_tokens, int):
        total_tokens = prompt_tokens + eval_tokens
    if isinstance(total_tokens, int) and isinstance(num_ctx, int):
        shift_suspected = total_tokens > num_ctx
    return {
        "model_prompt_tokens": prompt_tokens,
        "model_eval_tokens": eval_tokens,
        "model_eval_tokens_max": eval_tokens,
        "model_num_ctx": num_ctx,
        "model_done_reason": generated.get("done_reason"),
        "model_context_total_tokens": total_tokens,
        "model_context_shift_suspected": shift_suspected,
    }


def _combined_model_metric_fields(generated_results: list[dict]) -> dict:
    if not generated_results:
        return _model_metric_fields({})
    fields = [_model_metric_fields(generated) for generated in generated_results]
    prompt_values = [
        field["model_prompt_tokens"] for field in fields if isinstance(field["model_prompt_tokens"], int)
    ]
    eval_values = [
        field["model_eval_tokens"] for field in fields if isinstance(field["model_eval_tokens"], int)
    ]
    num_ctx_values = [
        field["model_num_ctx"] for field in fields if isinstance(field["model_num_ctx"], int)
    ]
    total_values = [
        field["model_context_total_tokens"]
        for field in fields
        if isinstance(field["model_context_total_tokens"], int)
    ]
    done_reasons = [
        str(field["model_done_reason"])
        for field in fields
        if field["model_done_reason"] is not None
    ]
    shift_values = [
        field["model_context_shift_suspected"]
        for field in fields
        if isinstance(field["model_context_shift_suspected"], bool)
    ]
    return {
        "model_prompt_tokens": max(prompt_values) if prompt_values else None,
        "model_eval_tokens": sum(eval_values) if eval_values else None,
        "model_eval_tokens_max": max(eval_values) if eval_values else None,
        "model_num_ctx": max(num_ctx_values) if num_ctx_values else None,
        "model_done_reason": ",".join(done_reasons) if done_reasons else None,
        "model_context_total_tokens": max(total_values) if total_values else None,
        "model_context_shift_suspected": any(shift_values) if shift_values else None,
    }


def _parse_blocks(raw_text: str) -> list[tuple[str, str]]:
    lines = (raw_text or "").splitlines(keepends=True)
    blocks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if not (stripped.startswith(FILE_START) and stripped.endswith(" ===")):
            raise GolemPatchHarnessError(f"expected file header, got: {stripped[:80]}")
        path = _normalize_relative_path(stripped[len(FILE_START) : -len(" ===")])
        index += 1
        content_lines: list[str] = []
        while index < len(lines) and lines[index].strip() != FILE_END:
            content_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            raise GolemPatchHarnessError(f"missing end marker for {path}")
        if not content_lines:
            raise GolemPatchHarnessError(f"empty file block: {path}")
        blocks.append((path, "".join(content_lines)))
        index += 1
    if not blocks:
        raise GolemPatchHarnessError("no file blocks found")
    return blocks


def _parse_search_replace_blocks(raw_text: str) -> list[SearchReplaceBlock]:
    lines = (raw_text or "").splitlines(keepends=True)
    blocks: list[SearchReplaceBlock] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if not (stripped.startswith(PATCH_START) and stripped.endswith(" ===")):
            raise GolemPatchHarnessError(f"expected patch header, got: {stripped[:80]}")
        file_path = _normalize_relative_path(stripped[len(PATCH_START) : -len(" ===")])
        index += 1
        if index >= len(lines) or lines[index].strip() != SEARCH_START:
            raise GolemPatchHarnessError(f"missing search marker for {file_path}")
        index += 1
        search_lines: list[str] = []
        while index < len(lines) and lines[index].strip() != SEARCH_SEPARATOR:
            search_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            raise GolemPatchHarnessError(f"missing search/replace separator for {file_path}")
        index += 1
        replace_lines: list[str] = []
        while index < len(lines) and lines[index].strip() != REPLACE_END:
            replace_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            raise GolemPatchHarnessError(f"missing replace marker for {file_path}")
        search = "".join(search_lines)
        replace = "".join(replace_lines)
        if not search:
            raise GolemPatchHarnessError(f"empty search block for {file_path}")
        _reject_search_replace_magic_lines(file_path, "search", search)
        _reject_search_replace_magic_lines(file_path, "replace", replace)
        blocks.append(SearchReplaceBlock(file_path=file_path, search=search, replace=replace))
        index += 1
    if not blocks:
        raise GolemPatchHarnessError("no patch blocks found")
    return blocks


def _reject_search_replace_magic_lines(file_path: str, block_name: str, text: str) -> None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in SEARCH_REPLACE_MAGIC_LINES or stripped.startswith(PATCH_START):
            raise GolemPatchHarnessError(
                f"{block_name} block for {file_path} contains reserved patch marker: {stripped}"
            )


def _normalize_relative_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        raise GolemPatchHarnessError("empty path")
    if normalized.startswith("/") or ":" in normalized:
        raise GolemPatchHarnessError(f"absolute path is not allowed: {path}")
    parts = Path(normalized).parts
    if ".." in parts:
        raise GolemPatchHarnessError(f"path traversal is not allowed: {path}")
    return "/".join(parts)


def _validate_expected_files(paths: list[str]) -> None:
    if not paths:
        raise GolemPatchHarnessError("expected files are required")
    if len(set(paths)) != len(paths):
        raise GolemPatchHarnessError("expected files contain duplicates")


def _reject_known_wrappers(raw_text: str) -> None:
    for marker in FORBIDDEN_WRAPPERS:
        if marker in raw_text:
            raise GolemPatchHarnessError(f"forbidden wrapper marker: {marker}")


def _reject_protected_paths(paths: list[str]) -> None:
    protected = [path for path in paths if is_protected_path(path)]
    if protected:
        raise GolemPatchHarnessError(
            f"protected path(s) cannot be auto-committed: {', '.join(protected)}"
        )


def is_protected_path(path: str) -> bool:
    normalized = _normalize_relative_path(path)
    if normalized in PROTECTED_EXACT:
        return True
    if normalized.lower().startswith("routing_contract"):
        return True
    lowered = normalized.lower()
    return lowered.startswith(PROTECTED_PREFIXES) or "/security/" in lowered


def _ensure_files_exist(root: Path, paths: list[str]) -> None:
    missing = [path for path in paths if not (root / path).is_file()]
    if missing:
        raise GolemPatchHarnessError(f"declared file(s) do not exist: {', '.join(missing)}")


def _reject_oversized_targets(root: Path, paths: list[str]) -> None:
    oversized: list[str] = []
    for path in paths:
        source = (root / path).read_text(encoding="utf-8", errors="replace")
        estimated_tokens = _estimate_prompt_tokens(source)
        if estimated_tokens > PROMPT_TOKEN_BUDGET:
            oversized.append(f"{path}~{estimated_tokens}")
    if oversized:
        raise GolemPatchHarnessError(
            "file too large — split the task/file; estimated prompt tokens exceed "
            f"budget {PROMPT_TOKEN_BUDGET}: {', '.join(oversized)}"
        )


def _estimate_prompt_tokens(text: str) -> int:
    return int(len(text) / TOKEN_CHARS_PER_TOKEN) + 1


def _read_originals(root: Path, paths: list[str]) -> dict[str, str]:
    return {path: (root / path).read_text(encoding="utf-8", errors="replace") for path in paths}


def _restore_originals(root: Path, originals: dict[str, str]) -> None:
    for path, content in originals.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def _write_replacements(root: Path, replacements: dict[str, str]) -> None:
    for relative_path, content in replacements.items():
        target = (root / relative_path).resolve()
        if not _is_relative_to(target, root):
            raise GolemPatchHarnessError(f"path escapes workspace: {relative_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def _remove_python_caches(root: Path) -> None:
    for cache_dir in root.rglob("__pycache__"):
        if not cache_dir.is_dir():
            continue
        for child in cache_dir.iterdir():
            if child.is_file() and child.suffix == ".pyc":
                child.unlink(missing_ok=True)


def _check_test_preservation(root: Path, replacements: dict[str, str]) -> None:
    for relative, generated in replacements.items():
        if not _is_test_file(relative):
            continue
        current = (root / relative).read_text(encoding="utf-8", errors="replace")
        current_count = count_test_functions(current)
        generated_count = count_test_functions(generated)
        if generated_count < current_count:
            raise GolemPatchHarnessError(
                f"{relative} reduced test functions from {current_count} to {generated_count}"
            )


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return path.startswith("tests/") and name.startswith("test_") and name.endswith(".py")


def _post_apply_contract_error(
    root: Path,
    expected_files: list[str],
    max_diff_lines: int,
    baseline_untracked: set[str] | None = None,
) -> str | None:
    touched = _git_changed_files(root, baseline_untracked=baseline_untracked or set())
    outside = sorted(set(touched) - set(expected_files))
    if outside:
        return f"touched files outside declaration: {', '.join(outside)}"
    protected = [path for path in touched if is_protected_path(path)]
    if protected:
        return f"protected path(s) touched: {', '.join(protected)}"
    changed_lines = _git_numstat_changed_lines(root, expected_files)
    if changed_lines > max_diff_lines:
        return f"diff size {changed_lines} exceeds max_diff_lines={max_diff_lines}"
    return None


def _git_untracked_files(root: Path) -> set[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    files: set[str] = set()
    for line in completed.stdout.splitlines():
        if not line.startswith("?? ") or len(line) < 4:
            continue
        normalized = line[3:].strip().replace("\\", "/")
        if _ignored_status_path(normalized):
            continue
        files.add(normalized)
    return files


def _git_changed_files(root: Path, *, baseline_untracked: set[str] | None = None) -> list[str]:
    baseline_untracked = baseline_untracked or set()
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    files: list[str] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = path.replace("\\", "/")
        if _ignored_status_path(normalized):
            continue
        if line.startswith("?? ") and normalized in baseline_untracked:
            continue
        files.append(normalized)
    return files


def _ignored_status_path(path: str) -> bool:
    return (
        path.startswith(".cabinet/")
        or path.startswith("notes/")
        or path.endswith(".pyc")
        or "__pycache__/" in path
    )


def _git_numstat_changed_lines(root: Path, expected_files: list[str]) -> int:
    completed = subprocess.run(
        ["git", "diff", "--numstat", "--"] + expected_files,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    total = 0
    for line in completed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        for value in parts[:2]:
            if value.isdigit():
                total += int(value)
    return total


def _commit_files(root: Path, files: list[str], message: str) -> str | None:
    safe_dir = str(root).replace("\\", "/")
    git_base = ["git", "-c", f"safe.directory={safe_dir}"]
    add = subprocess.run(
        git_base + ["add", "--"] + files,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if add.returncode != 0:
        return None
    commit = subprocess.run(
        git_base + ["commit", "-m", message],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if commit.returncode != 0:
        return None
    head = subprocess.run(
        git_base + ["rev-parse", "--short", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return head.stdout.strip() if head.returncode == 0 else ""


def _changed_files_after_commit(root: Path, files: list[str]) -> list[str]:
    completed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "--"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    changed = [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()]
    expected = set(files)
    return [path for path in changed if path in expected]


def _write_raw_output(
    output_dir: Path,
    raw_text: str,
    attempt: str,
    target_file: str | None = None,
) -> str:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    slug = ""
    if target_file:
        slug = "_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", target_file).strip("_")
    path = output_dir / f"golem_known_files_patch_{timestamp}_{attempt}{slug}_raw.txt"
    path.write_text(raw_text, encoding="utf-8", newline="\n")
    return path.as_posix()


def _write_escalation(
    root: Path,
    output_dir: Path,
    delegation: KnownFilesPatchDelegation,
    *,
    stage: str,
    reason: str,
    model: str,
    base_url: str,
    raw_output_path: str | None = None,
    repair_raw_output_path: str | None = None,
    verification: VerificationResult | None = None,
) -> str:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"golem_known_files_patch_escalation_{timestamp}.json"
    report: dict[str, object] = {
        "type": "golem_known_files_patch_escalation",
        "stage": stage,
        "reason": reason,
        "next_action": "sage_review_or_manual_implementation",
        "mode": "known-files",
        "files": list(delegation.files),
        "verify": delegation.verify,
        "scope": delegation.scope,
        "max_diff_lines": delegation.max_diff_lines,
        "model": model,
        "base_url": base_url,
    }
    if raw_output_path:
        report["raw_output"] = _artifact_path(raw_output_path, root)
    if repair_raw_output_path:
        report["repair_raw_output"] = _artifact_path(repair_raw_output_path, root)
    if verification is not None:
        report["verification"] = {
            "command": verification.command,
            "returncode": verification.returncode,
            "stdout_tail": _tail_text(verification.stdout),
            "stderr_tail": _tail_text(verification.stderr),
        }
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path.as_posix()


def _artifact_path(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return path


def _tail_text(text: str, max_chars: int = 4000) -> str:
    return text if len(text) <= max_chars else text[-max_chars:]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
