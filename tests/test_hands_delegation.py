import pytest

from core.hands_delegation import (
    GolemDelegationError,
    parse_known_files_patch_delegation,
)


def test_parse_known_files_patch_delegation_accepts_required_fields():
    parsed = parse_known_files_patch_delegation(
        '@gol patch mode=known-files files=[core/a.py, tests/test_a.py] '
        'verify="python -m pytest tests/test_a.py" scope="add behavior" '
        "max_diff_lines=25"
    )

    assert parsed is not None
    assert parsed.files == ("core/a.py", "tests/test_a.py")
    assert parsed.verify == "python -m pytest tests/test_a.py"
    assert parsed.scope == "add behavior"
    assert parsed.max_diff_lines == 25


def test_parse_known_files_patch_delegation_ignores_non_patch_messages():
    assert parse_known_files_patch_delegation("@gol run daily_batch") is None


def test_parse_known_files_patch_delegation_rejects_other_modes():
    with pytest.raises(GolemDelegationError, match="only patch mode=known-files"):
        parse_known_files_patch_delegation(
            '@gol patch mode=symbol-local symbol=core.x verify="pytest" scope="x"'
        )


def test_parse_known_files_patch_delegation_requires_verify_and_scope():
    with pytest.raises(GolemDelegationError, match='verify="..." is required'):
        parse_known_files_patch_delegation(
            '@gol patch mode=known-files files=[a.py] scope="x"'
        )

    with pytest.raises(GolemDelegationError, match='scope="..." is required'):
        parse_known_files_patch_delegation(
            '@gol patch mode=known-files files=[a.py] verify="pytest"'
        )


def test_parse_known_files_patch_delegation_rejects_unknown_keys():
    with pytest.raises(GolemDelegationError, match="unknown key"):
        parse_known_files_patch_delegation(
            '@gol patch mode=known-files files=[a.py] verify="pytest" scope="x" broad=true'
        )


def test_parse_known_files_patch_delegation_ignores_key_like_text_inside_quotes():
    parsed = parse_known_files_patch_delegation(
        '@gol patch mode=known-files files=[tests/test_x.py] verify="pytest tests/test_x.py" '
        'scope="add a test containing @gol patch mode=known-files verify=\\"pytest\\""'
    )

    assert parsed is not None
    assert parsed.files == ("tests/test_x.py",)
    assert parsed.verify == "pytest tests/test_x.py"
    assert "mode=known-files" in parsed.scope


def test_parse_known_files_patch_delegation_rejects_duplicate_mode_key():
    with pytest.raises(GolemDelegationError, match="duplicate key: mode"):
        parse_known_files_patch_delegation(
            '@gol patch mode=known-files files=[a.py] verify="pytest" scope="x" mode=known-files'
        )


def test_parse_known_files_patch_delegation_rejects_non_positive_max_diff_lines():
    with pytest.raises(GolemDelegationError, match="positive"):
        parse_known_files_patch_delegation(
            '@gol patch mode=known-files files=[a.py] verify="pytest" scope="x" max_diff_lines=0'
        )
