from pathlib import Path

import pytest

from hands_probe.full_file_harness import VerificationResult
from hands_probe.run_full_file_probe import (
    TASKS,
    build_sage_escalation_report,
    build_oracle_test_source,
    build_repair_prompt,
    count_test_functions,
    get_task,
    test_preservation_error as check_test_preservation_error,
    write_sage_escalation_report,
    write_oracle_test_file,
)


def test_build_oracle_test_source_contains_only_requested_task_imports():
    source = build_oracle_test_source([get_task("contact_quality")])

    assert "has_complete_contact" in source
    assert "extract_candidate_names" not in source
    assert "filter_candidates_by_company" not in source
    assert "dedupe_candidates_by_email" not in source
    assert "group_candidate_names_by_company" not in source
    assert "format_candidate_report" not in source
    assert "test_contact_quality_oracle" in source
    assert "The model does not write this file" in source


def test_build_oracle_test_source_accumulates_batch_task_imports():
    source = build_oracle_test_source(TASKS)

    assert "has_complete_contact" in source
    assert "extract_candidate_names" in source
    assert "filter_candidates_by_company" in source
    assert "dedupe_candidates_by_email" in source
    assert "group_candidate_names_by_company" in source
    assert "format_candidate_report" in source
    assert "test_contact_quality_oracle" in source
    assert "test_candidate_names_oracle" in source
    assert "test_company_filter_oracle" in source
    assert "test_email_dedupe_oracle" in source
    assert "test_company_name_groups_oracle" in source
    assert "test_candidate_report_oracle" in source


def test_build_oracle_test_source_rejects_empty_task_list():
    with pytest.raises(ValueError, match="at least one oracle task is required"):
        build_oracle_test_source([])


def test_write_oracle_test_file_writes_generated_file(tmp_path: Path):
    path = write_oracle_test_file(tmp_path, [get_task("candidate_names")])

    assert path == tmp_path / "tests" / "test_hands_probe_oracle.py"
    assert "extract_candidate_names" in path.read_text(encoding="utf-8")


def test_build_repair_prompt_includes_failure_context(tmp_path: Path):
    (tmp_path / "hands_probe").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "hands_probe" / "text_tools.py").write_text(
        "def has_complete_contact(info):\n    return False\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_hands_probe.py").write_text(
        "def test_generated():\n    assert False\n",
        encoding="utf-8",
    )
    verification = VerificationResult(
        argv=("python", "-m", "pytest"),
        returncode=1,
        stdout="FAILED tests/test_hands_probe_oracle.py::test_contract",
        stderr="AssertionError: contract mismatch",
    )

    prompt = build_repair_prompt(tmp_path, get_task("contact_quality"), verification)

    assert "You are repairing a Python sandbox edit." in prompt
    assert "Original task:" in prompt
    assert "exit code: 1" in prompt
    assert "FAILED tests/test_hands_probe_oracle.py::test_contract" in prompt
    assert "AssertionError: contract mismatch" in prompt
    assert "def has_complete_contact" in prompt


def test_build_sage_escalation_report_captures_terminal_failure(tmp_path: Path):
    verification = VerificationResult(
        argv=("python", "-m", "pytest", "tests/test_hands_probe.py"),
        returncode=1,
        stdout="line 1\n" + ("x" * 5000),
        stderr="AssertionError: contract mismatch",
    )

    report = build_sage_escalation_report(
        tmp_path,
        get_task("email_dedupe"),
        stage="repair_verify",
        reason="bounded_repair_failed_verification",
        raw_output_path=tmp_path / "notes" / "raw.txt",
        repair_output_path=tmp_path / "notes" / "repair.txt",
        verification=verification,
    )

    assert report["type"] == "sage_escalation"
    assert report["task_id"] == "email_dedupe"
    assert report["stage"] == "repair_verify"
    assert report["reason"] == "bounded_repair_failed_verification"
    assert report["next_action"] == "sage_review_or_manual_implementation"
    assert report["raw_output"] == "notes/raw.txt"
    assert report["repair_raw_output"] == "notes/repair.txt"
    assert report["expected_files"] == [
        "hands_probe/text_tools.py",
        "tests/test_hands_probe.py",
    ]
    verification_report = report["verification"]
    assert verification_report["argv"] == [
        "python",
        "-m",
        "pytest",
        "tests/test_hands_probe.py",
    ]
    assert verification_report["returncode"] == 1
    assert len(verification_report["stdout_tail"]) == 4000
    assert verification_report["stderr_tail"] == "AssertionError: contract mismatch"


def test_write_sage_escalation_report_uses_task_specific_json_path(tmp_path: Path):
    report = {
        "type": "sage_escalation",
        "task_id": "email_dedupe",
        "stage": "repair_verify",
    }

    path = write_sage_escalation_report(
        tmp_path,
        tmp_path / "notes",
        get_task("email_dedupe"),
        report,
    )

    assert path == tmp_path / "notes" / "golem_full_file_sage_escalation_email_dedupe.json"
    assert '"stage": "repair_verify"' in path.read_text(encoding="utf-8")


def test_count_test_functions_counts_top_level_tests_only():
    source = "\n".join(
        [
            "def test_one():",
            "    pass",
            "    def test_nested():",
            "        pass",
            "def helper():",
            "    pass",
            "def test_two():",
            "    pass",
        ]
    )

    assert count_test_functions(source) == 2


def test_test_preservation_error_rejects_generated_test_shrink(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hands_probe.py").write_text(
        "def test_one():\n    pass\n\ndef test_two():\n    pass\n",
        encoding="utf-8",
    )

    error = check_test_preservation_error(
        tmp_path,
        {"tests/test_hands_probe.py": "def test_one():\n    pass\n"},
    )

    assert error == "generated tests/test_hands_probe.py reduced test functions from 2 to 1"


def test_test_preservation_error_allows_equal_or_larger_test_set(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hands_probe.py").write_text(
        "def test_one():\n    pass\n",
        encoding="utf-8",
    )

    error = check_test_preservation_error(
        tmp_path,
        {
            "tests/test_hands_probe.py": (
                "def test_one():\n    pass\n\n"
                "def test_two():\n    pass\n"
            )
        },
    )

    assert error is None
