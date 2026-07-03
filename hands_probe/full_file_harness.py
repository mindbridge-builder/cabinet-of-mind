"""Sandbox-only full-file replacement checks for Golem probe tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


class FullFileHarnessError(ValueError):
    """Raised when a generated full-file response violates the probe contract."""


@dataclass(frozen=True)
class FullFileBlock:
    path: str
    content: str


@dataclass(frozen=True)
class VerificationResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


FILE_START = "=== FILE: "
FILE_END = "=== END FILE ==="
PROBE_TEST_PREFIX = "tests/test_hands_probe"


def is_probe_sandbox_path(path: str) -> bool:
    normalized = _normalize_relative_path(path)
    return normalized.startswith("hands_probe/") or (
        normalized.startswith(PROBE_TEST_PREFIX) and normalized.endswith(".py")
    )


def parse_full_file_response(raw_text: str, expected_files: list[str]) -> dict[str, str]:
    """Parse strict full-file blocks and return content by expected path.

    Expected format:

        === FILE: relative/path.py ===
        full file content
        === END FILE ===

    The parser deliberately rejects Markdown fences and placeholder wrappers
    because the manual probe showed those were recurring contract violations.
    """

    expected = [_normalize_relative_path(path) for path in expected_files]
    _validate_expected_files(expected)
    _reject_known_wrappers(raw_text)

    blocks = _parse_blocks(raw_text)
    paths = [block.path for block in blocks]

    duplicate_paths = {path for path in paths if paths.count(path) > 1}
    if duplicate_paths:
        raise FullFileHarnessError(
            f"duplicate file block(s): {', '.join(sorted(duplicate_paths))}"
        )

    unexpected = sorted(set(paths) - set(expected))
    if unexpected:
        raise FullFileHarnessError(f"unexpected file block(s): {', '.join(unexpected)}")

    missing = sorted(set(expected) - set(paths))
    if missing:
        raise FullFileHarnessError(f"missing file block(s): {', '.join(missing)}")

    return {block.path: block.content for block in blocks}


def apply_full_file_response(
    workspace_root: Path, raw_text: str, expected_files: list[str]
) -> list[Path]:
    replacements = parse_full_file_response(raw_text, expected_files)
    root = workspace_root.resolve()
    written: list[Path] = []

    for relative_path, content in replacements.items():
        target = (root / relative_path).resolve()
        if not _is_relative_to(target, root):
            raise FullFileHarnessError(f"path escapes workspace: {relative_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(target)

    return written


def run_verification(workspace_root: Path, argv: list[str]) -> VerificationResult:
    if not argv:
        raise FullFileHarnessError("verification command is required")

    completed = subprocess.run(
        argv,
        cwd=workspace_root,
        check=False,
        text=True,
        capture_output=True,
    )
    return VerificationResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _parse_blocks(raw_text: str) -> list[FullFileBlock]:
    lines = raw_text.splitlines(keepends=True)
    blocks: list[FullFileBlock] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if not (stripped.startswith(FILE_START) and stripped.endswith(" ===")):
            raise FullFileHarnessError(f"expected file header, got: {stripped[:80]}")

        path = stripped[len(FILE_START) : -len(" ===")]
        normalized_path = _normalize_relative_path(path)
        if not is_probe_sandbox_path(normalized_path):
            raise FullFileHarnessError(f"file outside probe sandbox: {normalized_path}")

        index += 1
        content_lines: list[str] = []
        while index < len(lines) and lines[index].strip() != FILE_END:
            content_lines.append(lines[index])
            index += 1

        if index >= len(lines):
            raise FullFileHarnessError(f"missing end marker for {normalized_path}")
        if not content_lines:
            raise FullFileHarnessError(f"empty file block: {normalized_path}")

        blocks.append(FullFileBlock(normalized_path, "".join(content_lines)))
        index += 1

    if not blocks:
        raise FullFileHarnessError("no file blocks found")
    return blocks


def _validate_expected_files(paths: list[str]) -> None:
    if not paths:
        raise FullFileHarnessError("expected_files is required")
    for path in paths:
        if not is_probe_sandbox_path(path):
            raise FullFileHarnessError(f"expected file outside probe sandbox: {path}")


def _reject_known_wrappers(raw_text: str) -> None:
    forbidden = ("```", "<complete file content>", "</complete>")
    for marker in forbidden:
        if marker in raw_text:
            raise FullFileHarnessError(f"forbidden wrapper marker: {marker}")


def _normalize_relative_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        raise FullFileHarnessError("empty path")
    if normalized.startswith("/") or ":" in normalized:
        raise FullFileHarnessError(f"absolute path is not allowed: {path}")
    parts = Path(normalized).parts
    if ".." in parts:
        raise FullFileHarnessError(f"path traversal is not allowed: {path}")
    return "/".join(parts)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
