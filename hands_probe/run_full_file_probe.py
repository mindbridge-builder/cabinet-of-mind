"""Run a sandbox full-file generation probe through the strict gate.

This is a probe utility, not a general Golem harness. It asks the local
configured local hands model to modify only known sandbox files, parses the response
with `full_file_harness`, and applies it only after the strict format gate
passes.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import urllib.request

from hands_probe.full_file_harness import (
    FullFileHarnessError,
    VerificationResult,
    apply_full_file_response,
    parse_full_file_response,
    run_verification,
)


MODEL = os.environ.get("CABINET_HANDS_MODEL", "qwen3-coder:30b")
OLLAMA_URL = "http://127.0.0.1:11434"
EXPECTED_FILES = ["hands_probe/text_tools.py", "tests/test_hands_probe.py"]
ORACLE_TEST_FILE = "tests/test_hands_probe_oracle.py"
ESCALATION_PREFIX = "golem_full_file_sage_escalation"
VERIFY_COMMAND = [
    sys.executable,
    "-m",
    "pytest",
    "tests/test_hands_probe.py",
    ORACLE_TEST_FILE,
]


@dataclass(frozen=True)
class ProbeTask:
    task_id: str
    description: str
    oracle_tests: str


TASKS = [
    ProbeTask(
        task_id="contact_quality",
        description="""Add a function has_complete_contact(info: dict[str, str | None]) -> bool.

Behavior:
- Return True only when both info.get("email") and info.get("phone") are non-empty, non-whitespace strings.
- Return False for missing keys, None values, empty strings, and whitespace-only strings.
- Preserve all existing behavior and tests.
- Add focused tests for complete contact, email-only, phone-only, missing keys, None values, empty strings, and whitespace-only values.
""",
        oracle_tests="""
def test_contact_quality_oracle_requires_both_non_blank_fields():
    assert has_complete_contact({"email": "a@example.com", "phone": "+1"}) is True
    assert has_complete_contact({"email": "a@example.com", "phone": "   "}) is False
    assert has_complete_contact({"email": "   ", "phone": "+1"}) is False
    assert has_complete_contact({"email": None, "phone": "+1"}) is False
    assert has_complete_contact({"phone": "+1"}) is False
""",
    ),
    ProbeTask(
        task_id="candidate_names",
        description="""Add a function extract_candidate_names(candidates: list[dict[str, str | None]]) -> list[str].

Behavior:
- Return candidate names in input order.
- Skip missing, None, empty, and whitespace-only names.
- Strip surrounding whitespace from returned names.
- Preserve duplicate non-empty names; do not deduplicate.
- Preserve all existing behavior and tests.
- Add focused tests for normal names, missing/None names, whitespace trimming, empty names, and duplicate names.
""",
        oracle_tests="""
def test_candidate_names_oracle_strips_skips_and_preserves_order():
    candidates = [
        {"name": "  Ivan Petrov  "},
        {"name": None},
        {"email": "missing-name@example.com"},
        {"name": ""},
        {"name": "Anna"},
        {"name": "Ivan Petrov"},
        {"name": "   "},
    ]

    assert extract_candidate_names(candidates) == [
        "Ivan Petrov",
        "Anna",
        "Ivan Petrov",
    ]
""",
    ),
    ProbeTask(
        task_id="company_filter",
        description="""Add a function filter_candidates_by_company(candidates: list[dict[str, str | None]], company: str) -> list[dict[str, str | None]].

Behavior:
- Return candidates whose "company" value matches the requested company after stripping surrounding whitespace.
- Matching is case-insensitive.
- Return [] when company is empty or whitespace-only.
- Ignore candidates with missing, None, empty, or whitespace-only company values.
- Preserve original candidate dictionaries in the returned list; do not copy or mutate them.
- Preserve all existing behavior and tests.
- Add focused tests for case-insensitive match, whitespace trimming, empty search, missing company, None company, and preserving original dictionary identity.
""",
        oracle_tests="""
def test_company_filter_oracle_matches_without_copying_or_mutating():
    first = {"name": "Ivan", "company": " Megalift "}
    second = {"name": "Anna", "company": "MEGALIFT"}
    ignored = {"name": "No Company", "company": None}
    candidates = [first, ignored, {"name": "Other", "company": "Acme"}, second]

    result = filter_candidates_by_company(candidates, "megalift")

    assert result == [first, second]
    assert result[0] is first
    assert result[1] is second
    assert filter_candidates_by_company(candidates, "   ") == []
""",
    ),
    ProbeTask(
        task_id="email_dedupe",
        description="""Add a function dedupe_candidates_by_email(candidates: list[dict[str, str | None]]) -> list[dict[str, str | None]].

Behavior:
- Return the first candidate for each non-empty email address.
- Treat email matching as case-insensitive after stripping surrounding whitespace.
- Ignore candidates with missing, None, empty, or whitespace-only email values.
- Preserve original candidate dictionaries in the returned list; do not copy or mutate them.
- Preserve input order for first occurrences.
- Preserve all existing behavior and tests.
- Add focused tests for duplicate emails, case-insensitive matching, whitespace trimming, missing/None/blank emails, and preserving dictionary identity.
""",
        oracle_tests="""
def test_email_dedupe_oracle_keeps_first_normalized_email_identity():
    first = {"name": "Ivan", "email": " Ivan@Example.com "}
    duplicate = {"name": "Ivan Duplicate", "email": "ivan@example.com"}
    second = {"name": "Anna", "email": "anna@example.com"}
    candidates = [
        {"name": "No Email"},
        first,
        {"name": "Blank", "email": "   "},
        duplicate,
        second,
        {"name": "None", "email": None},
        {"name": "Anna Duplicate", "email": " ANNA@EXAMPLE.COM "},
    ]

    result = dedupe_candidates_by_email(candidates)

    assert result == [first, second]
    assert result[0] is first
    assert result[1] is second
""",
    ),
    ProbeTask(
        task_id="company_name_groups",
        description="""Add a function group_candidate_names_by_company(candidates: list[dict[str, str | None]]) -> dict[str, list[str]].

Behavior:
- Group candidate names by company.
- Use lowercase stripped company names as dictionary keys.
- Strip surrounding whitespace from names before adding them.
- Skip candidates with missing, None, empty, or whitespace-only company values.
- Skip candidates with missing, None, empty, or whitespace-only name values.
- Preserve candidate order within each company's list.
- Preserve duplicate names; do not deduplicate.
- Preserve all existing behavior and tests.
- Add focused tests for grouping, case-insensitive company normalization, whitespace trimming, blank/missing values, and duplicate names.
""",
        oracle_tests="""
def test_company_name_groups_oracle_normalizes_and_skips_blank_values():
    candidates = [
        {"name": " Ivan ", "company": " MEGALIFT "},
        {"name": "Anna", "company": "megalift"},
        {"name": "No Company", "company": "   "},
        {"name": "", "company": "Acme"},
        {"company": "Acme"},
        {"name": "Bob", "company": "ACME"},
        {"name": "Ivan", "company": "Megalift"},
    ]

    assert group_candidate_names_by_company(candidates) == {
        "megalift": ["Ivan", "Anna", "Ivan"],
        "acme": ["Bob"],
    }
""",
    ),
    ProbeTask(
        task_id="candidate_report",
        description="""Add a function format_candidate_report(candidates: list[dict[str, str | None]]) -> str.

Behavior:
- Return one line per candidate that has at least a non-blank name or company.
- Each line format is "<name> - <company> - <contacts>".
- Use "Unknown candidate" for missing, None, empty, or whitespace-only names.
- Use "Unknown company" for missing, None, empty, or whitespace-only companies.
- Contacts are email and phone, in that order, joined by ", ". Ignore blank contacts.
- Use "no contact" when both email and phone are missing or blank.
- Strip surrounding whitespace from all emitted fields.
- Preserve input order.
- Return an empty string when no candidate has a non-blank name or company.
- Preserve all existing behavior and tests.
- Add focused tests for mixed complete/incomplete records, blank contacts, whitespace trimming, skipped fully blank records, and empty input.
""",
        oracle_tests="""
def test_candidate_report_oracle_formats_lines_and_skips_fully_blank_records():
    candidates = [
        {
            "name": " Ivan ",
            "company": " Megalift ",
            "email": "ivan@example.com",
            "phone": " +1 ",
        },
        {"name": None, "company": "Acme", "email": "   ", "phone": None},
        {"name": "   ", "company": "   ", "email": "x@example.com", "phone": "+2"},
        {"name": "Anna", "company": None, "email": None, "phone": "   "},
    ]

    assert format_candidate_report(candidates) == "\\n".join(
        [
            "Ivan - Megalift - ivan@example.com, +1",
            "Unknown candidate - Acme - no contact",
            "Anna - Unknown company - no contact",
        ]
    )
    assert format_candidate_report([]) == ""
""",
    ),
]

PROMPT_TEMPLATE = """You are editing a Python sandbox.

Return exactly two complete file blocks and nothing else.

Required output format:
=== FILE: hands_probe/text_tools.py ===
<complete content of hands_probe/text_tools.py>
=== END FILE ===
=== FILE: tests/test_hands_probe.py ===
<complete content of tests/test_hands_probe.py>
=== END FILE ===

Hard rules:
- Do not use Markdown fences.
- Do not write placeholder wrapper tags.
- Do not omit existing functions or existing tests.
- Do not add extra files.
- Do not explain the change.

Task:
{task}

Current hands_probe/text_tools.py:
{text_tools}

Current tests/test_hands_probe.py:
{tests}
"""

REPAIR_PROMPT_TEMPLATE = """You are repairing a Python sandbox edit.

Return exactly two complete file blocks and nothing else.

Required output format:
=== FILE: hands_probe/text_tools.py ===
<complete content of hands_probe/text_tools.py>
=== END FILE ===
=== FILE: tests/test_hands_probe.py ===
<complete content of tests/test_hands_probe.py>
=== END FILE ===

Hard rules:
- Do not use Markdown fences.
- Do not write placeholder wrapper tags.
- Do not omit existing functions or existing tests.
- Do not add extra files.
- Do not explain the change.
- Fix the implementation and generated tests to match the task contract and the independent oracle.

Original task:
{task}

Verification failure:
{failure}

Current hands_probe/text_tools.py:
{text_tools}

Current tests/test_hands_probe.py:
{tests}
"""


def get_task(task_id: str) -> ProbeTask:
    for task in TASKS:
        if task.task_id == task_id:
            return task
    known = ", ".join(task.task_id for task in TASKS)
    raise ValueError(f"unknown task {task_id!r}; known tasks: {known}")


def build_prompt(workspace_root: Path, task: ProbeTask) -> str:
    return PROMPT_TEMPLATE.format(
        task=task.description.strip(),
        text_tools=(workspace_root / "hands_probe" / "text_tools.py").read_text(
            encoding="utf-8"
        ),
        tests=(workspace_root / "tests" / "test_hands_probe.py").read_text(
            encoding="utf-8"
        ),
    )


def build_repair_prompt(
    workspace_root: Path, task: ProbeTask, verification: VerificationResult
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
    return REPAIR_PROMPT_TEMPLATE.format(
        task=task.description.strip(),
        failure=failure,
        text_tools=(workspace_root / "hands_probe" / "text_tools.py").read_text(
            encoding="utf-8"
        ),
        tests=(workspace_root / "tests" / "test_hands_probe.py").read_text(
            encoding="utf-8"
        ),
    )


def build_oracle_test_source(tasks: list[ProbeTask]) -> str:
    if not tasks:
        raise ValueError("at least one oracle task is required")

    imports = sorted(_oracle_import_names(tasks))
    source = [
        '"""Spec-derived tests generated by run_full_file_probe.py.',
        "",
        "The model does not write this file. It is the independent oracle for",
        "the current strict full-file probe run.",
        '"""',
        "",
        f"from hands_probe.text_tools import {', '.join(imports)}",
        "",
    ]
    for task in tasks:
        source.append(task.oracle_tests.strip())
        source.append("")
    return "\n".join(source).rstrip() + "\n"


def write_oracle_test_file(workspace_root: Path, tasks: list[ProbeTask]) -> Path:
    path = workspace_root / ORACLE_TEST_FILE
    write_artifact(path, build_oracle_test_source(tasks))
    return path


def _oracle_import_names(tasks: list[ProbeTask]) -> set[str]:
    names_by_task = {
        "contact_quality": {"has_complete_contact"},
        "candidate_names": {"extract_candidate_names"},
        "company_filter": {"filter_candidates_by_company"},
        "email_dedupe": {"dedupe_candidates_by_email"},
        "company_name_groups": {"group_candidate_names_by_company"},
        "candidate_report": {"format_candidate_report"},
    }
    names: set[str] = set()
    for task in tasks:
        names.update(names_by_task[task.task_id])
    return names


def call_ollama(prompt: str, model: str, base_url: str) -> str:
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {
            "temperature": 0.2,
            "top_p": 0.8,
            "top_k": 20,
        },
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str((data.get("message") or {}).get("content") or "")


def write_artifact(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def count_test_functions(source: str) -> int:
    return sum(1 for line in source.splitlines() if line.startswith("def test_"))


def test_preservation_error(
    workspace_root: Path, replacements: dict[str, str]
) -> str | None:
    test_path = workspace_root / "tests" / "test_hands_probe.py"
    current_count = count_test_functions(test_path.read_text(encoding="utf-8"))
    generated_count = count_test_functions(replacements["tests/test_hands_probe.py"])
    if generated_count < current_count:
        return (
            "generated tests/test_hands_probe.py reduced test functions "
            f"from {current_count} to {generated_count}"
        )
    return None


def _tail_text(text: str, *, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _artifact_path_for_report(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def build_sage_escalation_report(
    workspace_root: Path,
    task: ProbeTask,
    *,
    stage: str,
    reason: str,
    raw_output_path: Path,
    verification: VerificationResult | None = None,
    repair_output_path: Path | None = None,
    format_error: str | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "type": "sage_escalation",
        "task_id": task.task_id,
        "stage": stage,
        "reason": reason,
        "next_action": "sage_review_or_manual_implementation",
        "task_description": task.description.strip(),
        "expected_files": EXPECTED_FILES,
        "raw_output": _artifact_path_for_report(raw_output_path, workspace_root),
    }
    if repair_output_path is not None:
        report["repair_raw_output"] = _artifact_path_for_report(
            repair_output_path,
            workspace_root,
        )
    if format_error is not None:
        report["format_error"] = format_error
    if verification is not None:
        report["verification"] = {
            "argv": list(verification.argv),
            "returncode": verification.returncode,
            "stdout_tail": _tail_text(verification.stdout),
            "stderr_tail": _tail_text(verification.stderr),
        }
    return report


def write_sage_escalation_report(
    workspace_root: Path,
    output_dir: Path,
    task: ProbeTask,
    report: dict[str, object],
) -> Path:
    path = output_dir / f"{ESCALATION_PREFIX}_{task.task_id}.json"
    write_artifact(path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return path


def print_sage_escalation_report(
    workspace_root: Path,
    output_dir: Path,
    task: ProbeTask,
    report: dict[str, object],
) -> None:
    path = write_sage_escalation_report(workspace_root, output_dir, task, report)
    print(f"{task.task_id}: SAGE_ESCALATION: {path.relative_to(workspace_root)}")


def run_task(
    workspace_root: Path,
    task: ProbeTask,
    *,
    model: str,
    base_url: str,
    apply_changes: bool,
    raw_output_dir: Path,
    oracle_tasks: list[ProbeTask] | None = None,
    repair_on_fail: bool = False,
) -> int:
    oracle_path = write_oracle_test_file(workspace_root, oracle_tasks or [task])
    print(f"{task.task_id}: oracle: {oracle_path.relative_to(workspace_root)}")

    prompt = build_prompt(workspace_root, task)
    raw_response = call_ollama(prompt, model, base_url)
    raw_output_path = raw_output_dir / f"golem_full_file_{task.task_id}_raw.txt"
    write_artifact(raw_output_path, raw_response)

    try:
        replacements = parse_full_file_response(raw_response, EXPECTED_FILES)
    except FullFileHarnessError as exc:
        print(f"{task.task_id}: FORMAT_GATE_FAIL: {exc}")
        print(f"{task.task_id}: raw_output: {raw_output_path}")
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="format_gate",
            reason="model_output_failed_strict_full_file_format",
            raw_output_path=raw_output_path,
            format_error=str(exc),
        )
        print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
        return 2

    preservation_error = test_preservation_error(workspace_root, replacements)
    if preservation_error is not None:
        print(f"{task.task_id}: TEST_PRESERVATION_FAIL: {preservation_error}")
        print(f"{task.task_id}: raw_output: {raw_output_path}")
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="test_preservation",
            reason="model_output_removed_existing_tests",
            raw_output_path=raw_output_path,
            format_error=preservation_error,
        )
        print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
        return 2

    print(f"{task.task_id}: FORMAT_GATE_PASS")
    print(f"{task.task_id}: raw_output: {raw_output_path}")

    if not apply_changes:
        print(f"{task.task_id}: APPLY_SKIPPED: pass --apply to write generated files")
        return 0

    written = apply_full_file_response(workspace_root, raw_response, EXPECTED_FILES)
    for path in written:
        print(f"{task.task_id}: wrote: {path.relative_to(workspace_root)}")

    verification = run_verification(workspace_root, VERIFY_COMMAND)
    print(f"{task.task_id}: VERIFY_EXIT: {verification.returncode}")
    if verification.stdout:
        print(verification.stdout)
    if verification.stderr:
        print(verification.stderr, file=sys.stderr)
    if verification.passed or not repair_on_fail:
        if not verification.passed:
            report = build_sage_escalation_report(
                workspace_root,
                task,
                stage="verify",
                reason="verification_failed_without_repair",
                raw_output_path=raw_output_path,
                verification=verification,
            )
            print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
        return verification.returncode

    print(f"{task.task_id}: REPAIR_ATTEMPT: 1")
    repair_prompt = build_repair_prompt(workspace_root, task, verification)
    repair_response = call_ollama(repair_prompt, model, base_url)
    repair_output_path = raw_output_dir / f"golem_full_file_{task.task_id}_repair_raw.txt"
    write_artifact(repair_output_path, repair_response)

    try:
        repair_replacements = parse_full_file_response(repair_response, EXPECTED_FILES)
    except FullFileHarnessError as exc:
        print(f"{task.task_id}: REPAIR_FORMAT_GATE_FAIL: {exc}")
        print(f"{task.task_id}: repair_raw_output: {repair_output_path}")
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="repair_format_gate",
            reason="bounded_repair_failed_strict_full_file_format",
            raw_output_path=raw_output_path,
            repair_output_path=repair_output_path,
            format_error=str(exc),
        )
        print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
        return 3

    preservation_error = test_preservation_error(workspace_root, repair_replacements)
    if preservation_error is not None:
        print(f"{task.task_id}: REPAIR_TEST_PRESERVATION_FAIL: {preservation_error}")
        print(f"{task.task_id}: repair_raw_output: {repair_output_path}")
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="repair_test_preservation",
            reason="bounded_repair_removed_existing_tests",
            raw_output_path=raw_output_path,
            repair_output_path=repair_output_path,
            format_error=preservation_error,
        )
        print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
        return 3

    print(f"{task.task_id}: REPAIR_FORMAT_GATE_PASS")
    print(f"{task.task_id}: repair_raw_output: {repair_output_path}")
    written = apply_full_file_response(workspace_root, repair_response, EXPECTED_FILES)
    for path in written:
        print(f"{task.task_id}: repair_wrote: {path.relative_to(workspace_root)}")

    repaired_verification = run_verification(workspace_root, VERIFY_COMMAND)
    print(f"{task.task_id}: REPAIR_VERIFY_EXIT: {repaired_verification.returncode}")
    if repaired_verification.stdout:
        print(repaired_verification.stdout)
    if repaired_verification.stderr:
        print(repaired_verification.stderr, file=sys.stderr)
    if not repaired_verification.passed:
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="repair_verify",
            reason="bounded_repair_failed_verification",
            raw_output_path=raw_output_path,
            repair_output_path=repair_output_path,
            verification=repaired_verification,
        )
        print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
    return repaired_verification.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--base-url", default=OLLAMA_URL)
    parser.add_argument(
        "--raw-output",
        default="notes/golem_full_file_strict_probe_raw.txt",
        help="Raw output file for a single task. Ignored by --batch.",
    )
    parser.add_argument(
        "--raw-output-dir",
        default="notes",
        help="Directory for raw output files in --batch mode.",
    )
    parser.add_argument(
        "--task",
        default="contact_quality",
        choices=[task.task_id for task in TASKS],
        help="Probe task to run when --batch is not set.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Run all configured probe tasks sequentially and stop on first failure.",
    )
    parser.add_argument(
        "--start-at",
        choices=[task.task_id for task in TASKS],
        help="Start --batch at this task while keeping oracle tests for prior tasks.",
    )
    parser.add_argument(
        "--repair-on-fail",
        action="store_true",
        help="Run one bounded strict full-file repair attempt after verification failure.",
    )
    args = parser.parse_args()

    workspace_root = Path.cwd()
    if args.batch:
        raw_output_dir = workspace_root / args.raw_output_dir
        oracle_tasks: list[ProbeTask] = []
        running = args.start_at is None
        for task in TASKS:
            oracle_tasks.append(task)
            if not running and task.task_id == args.start_at:
                running = True
            if not running:
                print(f"{task.task_id}: BATCH_SKIPPED_BEFORE_START_AT")
                continue
            result = run_task(
                workspace_root,
                task,
                model=args.model,
                base_url=args.base_url,
                apply_changes=args.apply,
                raw_output_dir=raw_output_dir,
                oracle_tasks=oracle_tasks,
                repair_on_fail=args.repair_on_fail,
            )
            if result != 0:
                print(f"BATCH_STOPPED_AT: {task.task_id}")
                return result
        print(f"BATCH_PASS: {len(TASKS)} task(s)")
        return 0

    task = get_task(args.task)
    oracle_path = write_oracle_test_file(workspace_root, [task])
    print(f"oracle: {oracle_path.relative_to(workspace_root)}")
    prompt = build_prompt(workspace_root, task)
    raw_response = call_ollama(prompt, args.model, args.base_url)
    write_artifact(workspace_root / args.raw_output, raw_response)

    try:
        replacements = parse_full_file_response(raw_response, EXPECTED_FILES)
    except FullFileHarnessError as exc:
        print(f"FORMAT_GATE_FAIL: {exc}")
        print(f"raw_output: {args.raw_output}")
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="format_gate",
            reason="model_output_failed_strict_full_file_format",
            raw_output_path=workspace_root / args.raw_output,
            format_error=str(exc),
        )
        print_sage_escalation_report(
            workspace_root,
            (workspace_root / args.raw_output).parent,
            task,
            report,
        )
        return 2

    preservation_error = test_preservation_error(workspace_root, replacements)
    if preservation_error is not None:
        print(f"TEST_PRESERVATION_FAIL: {preservation_error}")
        print(f"raw_output: {args.raw_output}")
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="test_preservation",
            reason="model_output_removed_existing_tests",
            raw_output_path=workspace_root / args.raw_output,
            format_error=preservation_error,
        )
        print_sage_escalation_report(
            workspace_root,
            (workspace_root / args.raw_output).parent,
            task,
            report,
        )
        return 2

    print("FORMAT_GATE_PASS")
    print(f"raw_output: {args.raw_output}")

    if not args.apply:
        print("APPLY_SKIPPED: pass --apply to write generated files")
        return 0

    written = apply_full_file_response(workspace_root, raw_response, EXPECTED_FILES)
    for path in written:
        print(f"wrote: {path.relative_to(workspace_root)}")

    verification = run_verification(workspace_root, VERIFY_COMMAND)
    print(f"VERIFY_EXIT: {verification.returncode}")
    if verification.stdout:
        print(verification.stdout)
    if verification.stderr:
        print(verification.stderr, file=sys.stderr)
    if verification.passed or not args.repair_on_fail:
        if not verification.passed:
            report = build_sage_escalation_report(
                workspace_root,
                task,
                stage="verify",
                reason="verification_failed_without_repair",
                raw_output_path=workspace_root / args.raw_output,
                verification=verification,
            )
            print_sage_escalation_report(
                workspace_root,
                (workspace_root / args.raw_output).parent,
                task,
                report,
            )
        return verification.returncode

    raw_output_dir = (workspace_root / args.raw_output).parent
    print("REPAIR_ATTEMPT: 1")
    repair_prompt = build_repair_prompt(workspace_root, task, verification)
    repair_response = call_ollama(repair_prompt, args.model, args.base_url)
    repair_output_path = raw_output_dir / f"golem_full_file_{task.task_id}_repair_raw.txt"
    write_artifact(repair_output_path, repair_response)

    try:
        repair_replacements = parse_full_file_response(repair_response, EXPECTED_FILES)
    except FullFileHarnessError as exc:
        print(f"REPAIR_FORMAT_GATE_FAIL: {exc}")
        print(f"repair_raw_output: {repair_output_path}")
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="repair_format_gate",
            reason="bounded_repair_failed_strict_full_file_format",
            raw_output_path=workspace_root / args.raw_output,
            repair_output_path=repair_output_path,
            format_error=str(exc),
        )
        print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
        return 3

    preservation_error = test_preservation_error(workspace_root, repair_replacements)
    if preservation_error is not None:
        print(f"REPAIR_TEST_PRESERVATION_FAIL: {preservation_error}")
        print(f"repair_raw_output: {repair_output_path}")
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="repair_test_preservation",
            reason="bounded_repair_removed_existing_tests",
            raw_output_path=workspace_root / args.raw_output,
            repair_output_path=repair_output_path,
            format_error=preservation_error,
        )
        print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
        return 3

    print("REPAIR_FORMAT_GATE_PASS")
    print(f"repair_raw_output: {repair_output_path}")
    written = apply_full_file_response(workspace_root, repair_response, EXPECTED_FILES)
    for path in written:
        print(f"repair_wrote: {path.relative_to(workspace_root)}")

    repaired_verification = run_verification(workspace_root, VERIFY_COMMAND)
    print(f"REPAIR_VERIFY_EXIT: {repaired_verification.returncode}")
    if repaired_verification.stdout:
        print(repaired_verification.stdout)
    if repaired_verification.stderr:
        print(repaired_verification.stderr, file=sys.stderr)
    if not repaired_verification.passed:
        report = build_sage_escalation_report(
            workspace_root,
            task,
            stage="repair_verify",
            reason="bounded_repair_failed_verification",
            raw_output_path=workspace_root / args.raw_output,
            repair_output_path=repair_output_path,
            verification=repaired_verification,
        )
        print_sage_escalation_report(workspace_root, raw_output_dir, task, report)
    return repaired_verification.returncode


if __name__ == "__main__":
    raise SystemExit(main())
