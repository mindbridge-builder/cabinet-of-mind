"""Typed Golem delegation parser.

This module is intentionally narrow: production support currently covers only
the proven `patch mode=known-files` path.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


class GolemDelegationError(ValueError):
    """Raised when a Golem delegation tag is missing required typed fields."""


@dataclass(frozen=True)
class KnownFilesPatchDelegation:
    files: tuple[str, ...]
    verify: str
    scope: str
    max_diff_lines: int = 200


_KES_PATCH_RE = re.compile(r"@gol\s+(?:[-\u2014]\s*)?patch\b(?P<body>.*)", re.IGNORECASE | re.DOTALL)
_KEY_RE = re.compile(r"\b([a-zA-Z_][\w-]*)=")
_ALLOWED_KEYS = {"mode", "files", "verify", "scope", "max_diff_lines"}


def parse_known_files_patch_delegation(text: str) -> KnownFilesPatchDelegation | None:
    """Parse `@gol patch mode=known-files ...` from a message.

    Returns None when the message is not a patch delegation. Raises
    GolemDelegationError when it is a patch delegation but the typed contract is
    invalid.
    """

    match = _KES_PATCH_RE.search(text or "")
    if not match:
        return None

    values = _parse_key_values(match.group("body"))
    mode = values.get("mode", "").strip().lower()
    if mode != "known-files":
        raise GolemDelegationError("only patch mode=known-files is supported")

    unknown = sorted(set(values) - _ALLOWED_KEYS)
    if unknown:
        raise GolemDelegationError(f"unknown key(s): {', '.join(unknown)}")

    files = _parse_files(values.get("files", ""))
    verify = values.get("verify", "").strip()
    scope = values.get("scope", "").strip()
    if not files:
        raise GolemDelegationError("files=[...] is required")
    if not verify:
        raise GolemDelegationError('verify="..." is required')
    if not scope:
        raise GolemDelegationError('scope="..." is required')

    max_diff_lines = 200
    if "max_diff_lines" in values:
        try:
            max_diff_lines = int(values["max_diff_lines"].strip())
        except ValueError as exc:
            raise GolemDelegationError("max_diff_lines must be an integer") from exc
        if max_diff_lines <= 0:
            raise GolemDelegationError("max_diff_lines must be positive")

    return KnownFilesPatchDelegation(
        files=tuple(files),
        verify=verify,
        scope=scope,
        max_diff_lines=max_diff_lines,
    )


def _parse_key_values(body: str) -> dict[str, str]:
    matches = _find_key_matches(body or "")
    if not matches:
        raise GolemDelegationError("patch delegation requires key=value fields")

    values: dict[str, str] = {}
    for index, (key, key_start, value_start) in enumerate(matches):
        start = value_start
        end = matches[index + 1][1] if index + 1 < len(matches) else len(body)
        raw_value = body[start:end].strip()
        if key in values:
            raise GolemDelegationError(f"duplicate key: {key}")
        values[key] = _strip_quotes(raw_value)
    return values


def _find_key_matches(body: str) -> list[tuple[str, int, int]]:
    matches: list[tuple[str, int, int]] = []
    quote: str | None = None
    bracket_depth = 0
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "[":
            bracket_depth += 1
            index += 1
            continue
        if char == "]" and bracket_depth:
            bracket_depth -= 1
            index += 1
            continue
        if bracket_depth == 0:
            match = _KEY_RE.match(body, index)
            if match:
                matches.append((match.group(1), match.start(), match.end()))
                index = match.end()
                continue
        index += 1
    return matches


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_files(raw: str) -> list[str]:
    value = raw.strip()
    if not value.startswith("[") or not value.endswith("]"):
        raise GolemDelegationError("files must use bracket syntax: files=[a.py, b.py]")
    inner = value[1:-1].strip()
    if not inner:
        return []
    files: list[str] = []
    for item in inner.split(","):
        path = _strip_quotes(item.strip())
        if not path:
            raise GolemDelegationError("files contains an empty path")
        files.append(path)
    return files
