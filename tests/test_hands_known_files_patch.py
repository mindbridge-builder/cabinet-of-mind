from pathlib import Path
import subprocess
import sys

import pytest

from core.hands_delegation import KnownFilesPatchDelegation
from core.hands_full_file_patch import (
    build_search_replace_prompt,
    run_search_replace_patch,
    summarize_verification_failure,
    VerificationResult,
    GolemPatchHarnessError,
    SearchReplaceBlock,
    apply_search_replace_blocks,
    parse_full_file_response,
    parse_search_replace_response,
    run_known_files_patch,
)


def _block(path: str, content: str) -> str:
    if not content.endswith("\n"):
        content += "\n"
    return f"=== FILE: {path} ===\n{content}=== END FILE ===\n"


def _patch_block(path: str, search: str, replace: str) -> str:
    return (
        f"=== PATCH: {path} ===\n"
        "<<<<<<< SEARCH\n"
        f"{search}"
        "=======\n"
        f"{replace}"
        ">>>>>>> REPLACE\n"
    )


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "golem@example.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Golem Test"], cwd=root, check=True)


def _commit_all(root: Path, message: str = "init") -> None:
    subprocess.run(["git", "add", "--", "."], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True, text=True)


def _seed_project(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "tests").mkdir()
    (root / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )


def _delegation(verify: str) -> KnownFilesPatchDelegation:
    return KnownFilesPatchDelegation(
        files=("pkg/calc.py", "tests/test_calc.py"),
        verify=verify,
        scope="add subtract helper",
        max_diff_lines=80,
    )


def _generated_calc_files() -> dict[str, str]:
    return {
        "pkg/calc.py": _block(
            "pkg/calc.py",
            "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n",
        ),
        "tests/test_calc.py": _block(
            "tests/test_calc.py",
            "from pkg.calc import add, subtract\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n\n"
            "def test_subtract():\n"
            "    assert subtract(5, 2) == 3\n",
        ),
    }


def _generate_by_target(files: dict[str, str], metrics: dict | None = None):
    def generate(prompt: str) -> dict:
        for path, content in files.items():
            if f"Current {path}:" in prompt:
                return {"content": content, **(metrics or {})}
        raise AssertionError(f"unknown prompt target:\n{prompt}")

    return generate


def test_parse_full_file_response_rejects_wrappers_and_unexpected_files():
    with pytest.raises(GolemPatchHarnessError, match="forbidden wrapper marker"):
        parse_full_file_response(_block("pkg/calc.py", "```python\nx = 1\n```"), ["pkg/calc.py"])

    with pytest.raises(GolemPatchHarnessError, match="unexpected file block"):
        parse_full_file_response(_block("pkg/other.py", "x = 1\n"), ["pkg/calc.py"])


def test_parse_search_replace_response_groups_blocks_by_expected_file():
    raw = (
        _patch_block(
            "pkg/calc.py",
            "def add(a, b):\n    return a + b\n",
            "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n",
        )
        + _patch_block(
            "tests/test_calc.py",
            "from pkg.calc import add\n",
            "from pkg.calc import add, subtract\n",
        )
    )

    parsed = parse_search_replace_response(raw, ["pkg/calc.py", "tests/test_calc.py"])

    assert [block.file_path for block in parsed["pkg/calc.py"]] == ["pkg/calc.py"]
    assert parsed["pkg/calc.py"][0].search.startswith("def add")
    assert parsed["tests/test_calc.py"][0].replace == "from pkg.calc import add, subtract\n"


def test_parse_search_replace_response_groups_multiple_blocks_for_same_file():
    raw = (
        _patch_block("pkg/calc.py", "VALUE = 1\n", "VALUE = 2\n")
        + _patch_block("pkg/calc.py", "NAME = 'old'\n", "NAME = 'new'\n")
    )

    parsed = parse_search_replace_response(raw, ["pkg/calc.py"])

    assert [block.search for block in parsed["pkg/calc.py"]] == [
        "VALUE = 1\n",
        "NAME = 'old'\n",
    ]
    assert [block.replace for block in parsed["pkg/calc.py"]] == [
        "VALUE = 2\n",
        "NAME = 'new'\n",
    ]


def test_parse_search_replace_response_rejects_unexpected_file():
    raw = _patch_block("pkg/other.py", "x = 1\n", "x = 2\n")

    with pytest.raises(GolemPatchHarnessError, match="unexpected patch block"):
        parse_search_replace_response(raw, ["pkg/calc.py"])


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "no patch blocks found"),
        ("=== PATCH: pkg/calc.py ===\nx = 1\n", "missing search marker"),
        (
            "=== PATCH: pkg/calc.py ===\n<<<<<<< SEARCH\nx = 1\n",
            "missing search/replace separator",
        ),
        (
            "=== PATCH: pkg/calc.py ===\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n",
            "missing replace marker",
        ),
    ],
)
def test_parse_search_replace_response_rejects_malformed_blocks(raw: str, message: str):
    with pytest.raises(GolemPatchHarnessError, match=message):
        parse_search_replace_response(raw, ["pkg/calc.py"])


def test_parse_search_replace_response_rejects_reserved_markers_in_payload():
    raw = _patch_block(
        "pkg/calc.py",
        "x = 1\n",
        "x = 2\n=======\nx = 3\n",
    )

    with pytest.raises(GolemPatchHarnessError, match="reserved patch marker"):
        parse_search_replace_response(raw, ["pkg/calc.py"])


def test_apply_search_replace_blocks_requires_exactly_one_match():
    originals = {"pkg/calc.py": "VALUE = 1\nVALUE = 1\n"}
    patches = {
        "pkg/calc.py": [
            SearchReplaceBlock(
                file_path="pkg/calc.py",
                search="VALUE = 1\n",
                replace="VALUE = 2\n",
            )
        ]
    }

    with pytest.raises(GolemPatchHarnessError, match="matched 2 time"):
        apply_search_replace_blocks(originals, patches)


def test_apply_search_replace_blocks_returns_replacements_without_mutating_originals():
    originals = {"pkg/calc.py": "def add(a, b):\n    return a + b\n"}
    patches = {
        "pkg/calc.py": [
            SearchReplaceBlock(
                file_path="pkg/calc.py",
                search="def add(a, b):\n    return a + b\n",
                replace=(
                    "def add(a, b):\n"
                    "    return a + b\n\n\n"
                    "def subtract(a, b):\n"
                    "    return a - b\n"
                ),
            )
        ]
    }

    replacements = apply_search_replace_blocks(originals, patches)

    assert originals["pkg/calc.py"] == "def add(a, b):\n    return a + b\n"
    assert "def subtract" in replacements["pkg/calc.py"]


def test_apply_search_replace_blocks_applies_multiple_blocks_to_same_file_in_order():
    originals = {"pkg/calc.py": "VALUE = 1\nNAME = 'old'\n"}
    patches = {
        "pkg/calc.py": [
            SearchReplaceBlock(
                file_path="pkg/calc.py",
                search="VALUE = 1\n",
                replace="VALUE = 2\n",
            ),
            SearchReplaceBlock(
                file_path="pkg/calc.py",
                search="NAME = 'old'\n",
                replace="NAME = 'new'\n",
            ),
        ]
    }

    replacements = apply_search_replace_blocks(originals, patches)

    assert replacements["pkg/calc.py"] == "VALUE = 2\nNAME = 'new'\n"


def test_known_files_patch_auto_commits_green_full_file_output(tmp_path: Path):
    _init_repo(tmp_path)
    _seed_project(tmp_path)
    _commit_all(tmp_path)
    verify = f"{sys.executable} -m pytest tests/test_calc.py"
    generated = _generated_calc_files()

    result = run_known_files_patch(
        workspace_root=tmp_path,
        delegation=_delegation(verify),
        generate=_generate_by_target(
            generated,
            {
                "prompt_eval_count": 10,
                "eval_count": 20,
                "num_ctx": 24,
                "done_reason": "stop",
            },
        ),
        model="fake",
        base_url="fake",
        commit_message="golem: add subtract helper",
    )

    assert result.status == "auto_committed"
    assert result.commit
    assert result.verification is not None
    assert result.verification.passed
    assert result.model_eval_tokens == 40
    assert result.model_eval_tokens_max == 20
    assert result.model_num_ctx == 24
    assert result.model_done_reason == "stop,stop"
    assert result.model_context_total_tokens == 30
    assert result.model_context_shift_suspected is True
    assert "def subtract" in (tmp_path / "pkg" / "calc.py").read_text(encoding="utf-8")
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "golem: add subtract helper" in log


def test_known_files_patch_uses_one_repair_then_commits(tmp_path: Path):
    _init_repo(tmp_path)
    _seed_project(tmp_path)
    _commit_all(tmp_path)
    verify = f"{sys.executable} -m pytest tests/test_calc.py"
    calls = iter(
        [
            {"content": _block("pkg/calc.py", "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a + b\n")},
            {
                "content": _block(
                    "tests/test_calc.py",
                    "from pkg.calc import add, subtract\n\n"
                    "def test_add():\n"
                    "    assert add(2, 3) == 5\n\n"
                    "def test_subtract():\n"
                    "    assert subtract(5, 2) == 3\n",
                )
            },
            {"content": _block("pkg/calc.py", "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n")},
            {
                "content": _block(
                    "tests/test_calc.py",
                    "from pkg.calc import add, subtract\n\n"
                    "def test_add():\n"
                    "    assert add(2, 3) == 5\n\n"
                    "def test_subtract():\n"
                    "    assert subtract(5, 2) == 3\n",
                )
            },
        ]
    )

    result = run_known_files_patch(
        workspace_root=tmp_path,
        delegation=_delegation(verify),
        generate=lambda _prompt: next(calls),
        model="fake",
        base_url="fake",
    )

    assert result.status == "auto_committed"
    assert result.attempt == "repair"
    assert result.commit


def test_known_files_patch_ignores_preexisting_untracked_files(tmp_path: Path):
    _init_repo(tmp_path)
    _seed_project(tmp_path)
    _commit_all(tmp_path)
    (tmp_path / "scratch.txt").write_text("already here\n", encoding="utf-8")
    verify = f"{sys.executable} -m pytest tests/test_calc.py"
    generated = _generated_calc_files()

    result = run_known_files_patch(
        workspace_root=tmp_path,
        delegation=_delegation(verify),
        generate=_generate_by_target(generated),
        model="fake",
        base_url="fake",
    )

    assert result.status == "auto_committed"
    assert result.commit
    assert (tmp_path / "scratch.txt").read_text(encoding="utf-8") == "already here\n"


def test_known_files_patch_flags_new_untracked_files(tmp_path: Path):
    _init_repo(tmp_path)
    _seed_project(tmp_path)
    _commit_all(tmp_path)
    verify = f"{sys.executable} -m pytest tests/test_calc.py"
    generated = {
        "pkg/calc.py": _block("pkg/calc.py", "def add(a, b):\n    return a + b\n"),
        "tests/test_calc.py": _block(
            "tests/test_calc.py",
            "from pkg.calc import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n",
        ),
    }

    def generate(_prompt: str) -> dict:
        (tmp_path / "new_scratch.txt").write_text("new\n", encoding="utf-8")
        return _generate_by_target(generated)(_prompt)

    result = run_known_files_patch(
        workspace_root=tmp_path,
        delegation=_delegation(verify),
        generate=generate,
        model="fake",
        base_url="fake",
    )

    assert result.status == "escalated"
    assert "touched files outside declaration: new_scratch.txt" in result.reason


def test_known_files_patch_escalates_and_restores_after_failed_repair(tmp_path: Path):
    _init_repo(tmp_path)
    _seed_project(tmp_path)
    _commit_all(tmp_path)
    verify = f"{sys.executable} -m pytest tests/test_calc.py"
    bad = {
        "pkg/calc.py": _block("pkg/calc.py", "def add(a, b):\n    return 0\n"),
        "tests/test_calc.py": _block(
            "tests/test_calc.py",
            "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        ),
    }

    result = run_known_files_patch(
        workspace_root=tmp_path,
        delegation=_delegation(verify),
        generate=_generate_by_target(bad),
        model="fake",
        base_url="fake",
    )

    assert result.status == "escalated"
    assert result.escalation_path
    assert (tmp_path / "pkg" / "calc.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n    return a + b\n"
    )


def test_known_files_patch_refuses_protected_paths(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "GOLEM.md").write_text("prompt\n", encoding="utf-8")
    _commit_all(tmp_path)

    result = run_known_files_patch(
        workspace_root=tmp_path,
        delegation=KnownFilesPatchDelegation(
            files=("prompts/GOLEM.md",),
            verify=f"{sys.executable} -c \"print('ok')\"",
            scope="edit prompt",
        ),
        generate=lambda _prompt: {"content": ""},
        model="fake",
        base_url="fake",
    )

    assert result.status == "escalated"
    assert "protected path" in result.reason


def test_known_files_patch_refuses_oversized_target_before_model_call(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    oversized = "x = 1\n" * 6000
    (tmp_path / "pkg" / "large.py").write_text(oversized, encoding="utf-8")
    _commit_all(tmp_path)

    def generate(_prompt: str) -> dict:
        raise AssertionError("model should not be called for oversized targets")

    result = run_known_files_patch(
        workspace_root=tmp_path,
        delegation=KnownFilesPatchDelegation(
            files=("pkg/large.py",),
            verify=f"{sys.executable} -c \"print('ok')\"",
            scope="edit large file",
        ),
        generate=generate,
        model="fake",
        base_url="fake",
    )

    assert result.status == "escalated"
    assert "file too large" in result.reason


def test_run_search_replace_patch_retries_until_green(tmp_path: Path):
    _init_repo(tmp_path)
    _seed_project(tmp_path)
    _commit_all(tmp_path)
    verify = f"{sys.executable} -m pytest tests/test_calc.py -q"

    good_calc = _patch_block(
        "pkg/calc.py",
        "def add(a, b):\n    return a + b\n",
        "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n",
    )
    bad_calc = _patch_block(
        "pkg/calc.py",
        "def add(a, b):\n    return a + b\n",
        "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a + b\n",
    )
    tests_patch = _patch_block(
        "tests/test_calc.py",
        "def test_add():\n    assert add(2, 3) == 5\n",
        "def test_add():\n    assert add(2, 3) == 5\n\n\ndef test_subtract():\n    assert subtract(5, 2) == 3\n",
    )
    tests_patch = tests_patch.replace(
        "=== PATCH: tests/test_calc.py ===\n<<<<<<< SEARCH\n",
        "=== PATCH: tests/test_calc.py ===\n<<<<<<< SEARCH\nfrom pkg.calc import add\n\n",
    ).replace(
        "=======\ndef test_add():",
        "=======\nfrom pkg.calc import add, subtract\n\ndef test_add():",
    )

    responses = iter([
        {"content": "this is not a patch at all"},
        {"content": bad_calc + tests_patch},
        {"content": good_calc + tests_patch},
    ])
    prompts: list[str] = []

    def generate(prompt: str) -> dict:
        prompts.append(prompt)
        return next(responses)

    result = run_search_replace_patch(
        workspace_root=tmp_path,
        delegation=_delegation(verify),
        generate=generate,
        model="fake",
        base_url="fake",
        commit_message="golem: retry engine test",
    )

    assert result.status == "auto_committed"
    assert result.commit
    assert result.attempts_used == 3
    assert [a["strategy"] for a in result.attempts] == ["initial", "fresh", "retry_feedback"]
    assert result.attempts[0]["result"].startswith("format")
    assert result.attempts[1]["result"] == "verify_fail"
    assert result.attempts[2]["result"] == "pass"
    # capped feedback reached the third prompt, with the failing test named
    assert "Previous attempt failed verification" in prompts[2]
    assert "test_subtract" in prompts[2]
    assert "Previous attempt failed" not in prompts[1]
    assert "def subtract" in (tmp_path / "pkg" / "calc.py").read_text(encoding="utf-8")


def test_run_search_replace_patch_escalates_after_budget(tmp_path: Path):
    _init_repo(tmp_path)
    _seed_project(tmp_path)
    _commit_all(tmp_path)
    verify = f"{sys.executable} -m pytest tests/test_calc.py -q"

    def generate(prompt: str) -> dict:
        return {"content": "garbage, never a patch"}

    result = run_search_replace_patch(
        workspace_root=tmp_path,
        delegation=_delegation(verify),
        generate=generate,
        model="fake",
        base_url="fake",
        max_attempts=3,
    )

    assert result.status == "escalated"
    assert result.reason == "no green within 3 attempts"
    assert result.attempts_used == 3
    assert len(result.attempts) == 3
    assert all(a["result"].startswith("format") for a in result.attempts)
    # workspace untouched
    assert "subtract" not in (tmp_path / "pkg" / "calc.py").read_text(encoding="utf-8")


def test_summarize_verification_failure_caps_and_names_tests():
    verification = VerificationResult(
        command="pytest",
        returncode=1,
        stdout="FAILED tests/test_calc.py::test_subtract - assert 7 == 3\n" + ("x" * 9000),
        stderr="",
    )
    summary = summarize_verification_failure(verification, max_chars=2000)
    assert "test_subtract" in summary
    assert len(summary) <= 2400


def test_build_search_replace_prompt_contract(tmp_path: Path):
    _seed_project(tmp_path)
    prompt = build_search_replace_prompt(tmp_path, _delegation("pytest"), feedback=None)
    assert "=== PATCH: " in prompt
    assert "<<<<<<< SEARCH" in prompt
    assert ">>>>>>> REPLACE" in prompt
    assert "must not overlap" in prompt
    assert "Current pkg/calc.py:" in prompt
    assert "Current tests/test_calc.py:" in prompt
    with_feedback = build_search_replace_prompt(
        tmp_path, _delegation("pytest"), feedback="exit code: 1"
    )
    assert "Previous attempt failed verification" in with_feedback
