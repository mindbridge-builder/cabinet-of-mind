from pathlib import Path
import sys

import pytest

from hands_probe.full_file_harness import (
    FullFileHarnessError,
    apply_full_file_response,
    is_probe_sandbox_path,
    parse_full_file_response,
    run_verification,
)


def _response(*blocks: tuple[str, str]) -> str:
    parts = []
    for path, content in blocks:
        if not content.endswith("\n"):
            content += "\n"
        parts.append(f"=== FILE: {path} ===\n{content}=== END FILE ===\n")
    return "\n".join(parts)


def test_parse_full_file_response_accepts_exact_expected_blocks():
    raw = _response(
        ("hands_probe/text_tools.py", "def f():\n    return 1\n"),
        ("tests/test_hands_probe.py", "def test_f():\n    assert True\n"),
    )

    result = parse_full_file_response(
        raw,
        ["hands_probe/text_tools.py", "tests/test_hands_probe.py"],
    )

    assert result["hands_probe/text_tools.py"].startswith("def f")
    assert result["tests/test_hands_probe.py"].startswith("def test_f")


def test_parse_full_file_response_rejects_markdown_fences():
    raw = _response(("hands_probe/text_tools.py", "```python\ndef f(): pass\n```"))

    with pytest.raises(FullFileHarnessError, match="forbidden wrapper marker"):
        parse_full_file_response(raw, ["hands_probe/text_tools.py"])


def test_parse_full_file_response_rejects_complete_wrappers():
    raw = _response(
        (
            "hands_probe/text_tools.py",
            "<complete file content>\ndef f(): pass\n</complete>",
        )
    )

    with pytest.raises(FullFileHarnessError, match="forbidden wrapper marker"):
        parse_full_file_response(raw, ["hands_probe/text_tools.py"])


def test_parse_full_file_response_rejects_missing_and_extra_files():
    raw = _response(("hands_probe/text_tools.py", "def f(): pass\n"))

    with pytest.raises(FullFileHarnessError, match="missing file block"):
        parse_full_file_response(
            raw,
            ["hands_probe/text_tools.py", "tests/test_hands_probe.py"],
        )

    with pytest.raises(FullFileHarnessError, match="unexpected file block"):
        parse_full_file_response(raw, ["tests/test_hands_probe.py"])


def test_parse_full_file_response_rejects_duplicate_files():
    raw = _response(
        ("hands_probe/text_tools.py", "def f(): pass\n"),
        ("hands_probe/text_tools.py", "def g(): pass\n"),
    )

    with pytest.raises(FullFileHarnessError, match="duplicate file block"):
        parse_full_file_response(raw, ["hands_probe/text_tools.py"])


def test_parse_full_file_response_rejects_non_sandbox_paths():
    with pytest.raises(FullFileHarnessError, match="outside probe sandbox"):
        parse_full_file_response(
            _response(("core/routing.py", "x = 1\n")),
            ["core/routing.py"],
        )

    with pytest.raises(FullFileHarnessError, match="path traversal"):
        parse_full_file_response(
            _response(("../hands_probe/text_tools.py", "x = 1\n")),
            ["../hands_probe/text_tools.py"],
        )


def test_is_probe_sandbox_path_allows_only_probe_targets():
    assert is_probe_sandbox_path("hands_probe/text_tools.py")
    assert is_probe_sandbox_path("tests/test_hands_probe.py")
    assert is_probe_sandbox_path("tests/test_hands_probe_extra.py")
    assert not is_probe_sandbox_path("tests/test_other.py")
    assert not is_probe_sandbox_path("core/dispatcher.py")


def test_apply_full_file_response_writes_only_expected_files(tmp_path: Path):
    raw = _response(("hands_probe/generated.py", "VALUE = 42\n"))

    written = apply_full_file_response(
        tmp_path,
        raw,
        ["hands_probe/generated.py"],
    )

    assert written == [tmp_path / "hands_probe" / "generated.py"]
    assert (tmp_path / "hands_probe" / "generated.py").read_text() == "VALUE = 42\n"


def test_run_verification_returns_process_result(tmp_path: Path):
    result = run_verification(
        tmp_path,
        [sys.executable, "-c", "print('VERIFY_OK')"],
    )

    assert result.passed
    assert result.stdout.strip() == "VERIFY_OK"


def test_run_verification_rejects_empty_command(tmp_path: Path):
    with pytest.raises(FullFileHarnessError, match="verification command is required"):
        run_verification(tmp_path, [])
