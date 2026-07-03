"""Routing logic for Cabinet of Mind.

Participants: hux (Huxley, Claude Code CLI), dro (Diderot, Codex CLI), gol (Golem, Ollama).

Routing enum: hux | dro | gol | boss | "".

Routing JSON is an alternate spelling of an @tag, not a higher-priority mode.
The dispatcher can combine a JSON route with @-mentions in the response text.
route:"boss" → creates a pending Y/N. Everything else → direct launch.
"""
from __future__ import annotations

import json
import re


VALID_ROLES = ("hux", "dro", "gol", "boss")


def _parse_routing_tail(text: str) -> tuple[dict, int, int] | None:
    """Return routing JSON data plus the line range it occupies.

    Agents put raw JSON on the final non-empty line. Local models sometimes
    wrap it in a ```json fenced block — accepted as equivalent.
    """
    lines = (text or "").splitlines()
    nonempty = [idx for idx, line in enumerate(lines) if line.strip()]
    if not nonempty:
        return None

    def parse_candidate(candidate: str, start: int, end: int):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return None
        return (data, start, end) if isinstance(data, dict) else None

    last_idx = nonempty[-1]
    direct = parse_candidate(lines[last_idx].strip(), last_idx, last_idx + 1)
    if direct:
        return direct

    if not lines[last_idx].strip().startswith("```"):
        return None
    for opener_idx in reversed(nonempty[:-1]):
        if lines[opener_idx].strip().startswith("```"):
            candidate = "\n".join(lines[opener_idx + 1:last_idx]).strip()
            return parse_candidate(candidate, opener_idx, last_idx + 1)
    return None


def parse_mentions(text: str) -> list[str]:
    """Find @-mentions in user text. Order preserved, duplicates removed."""
    found = re.findall(r"@(?:hux|dro|gol|boss|cabinet)\b", text.lower())
    result = []
    for item in found:
        if item not in result:
            result.append(item)
    return result


def parse_route(text: str) -> str | None:
    """Determine the route from agent's final response.

    Routing JSON is parsed as an explicit tag. A legacy @tag on the last
    non-empty line is still accepted for older responses.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None

    parsed = _parse_routing_tail(text)
    if parsed and "route" in parsed[0]:
        route = (parsed[0]["route"] or "").lower().strip()
        return "@" + route if route in VALID_ROLES else None

    match = re.fullmatch(r"@(hux|dro|gol|boss)\b.*", lines[-1].lower())
    return "@" + match.group(1) if match else None


def parse_route_data(text: str) -> dict:
    """Return the routing JSON object from the response tail, if present."""
    parsed = _parse_routing_tail(text)
    if not parsed:
        return {}
    data = parsed[0]
    return data if isinstance(data, dict) else {}


def route_tail_visible_text(text: str) -> str:
    """Return response text with a trailing routing JSON block removed."""
    lines = (text or "").splitlines()
    parsed = _parse_routing_tail(text)
    if not parsed:
        return (text or "").strip()
    _data, start, end = parsed
    return "\n".join(lines[:start] + lines[end:]).strip()


_AGENT_TAG_RE = re.compile(r"@(hux|dro|gol|boss|cabinet)\b", re.IGNORECASE)
# Markdown decoration markers that can precede a leading tag on a line:
# "**@gol — ...**", "- @gol ...", "## @gol".
_LINE_DECOR_CHARS = "*-#•> \t"


def _line_leading_tag(line: str) -> str | None:
    """The participant tag a line starts with (after markdown markers)."""
    core = line.lstrip(_LINE_DECOR_CHARS)
    m = _AGENT_TAG_RE.match(core)
    return m.group(1).lower() if m else None


def extract_addressed_block(text: str, tag: str) -> str | None:
    """Cuts out the block of a reply addressed to @tag.

    The block: from the line with the tag (or from the tag's position, if it
    sits mid-line) to the line starting with another participant's tag, or to
    the end. Tags inside fenced code blocks and quotes (`> `) don't count as
    addressing.

    Used by fallback dispatch: the launched agent gets the block addressed to
    it, not someone else's whole reply — it sees the rest in history as
    context. None = no block found, the caller passes the full text.
    """
    tag = (tag or "").lstrip("@").lower()
    lines = (text or "").splitlines()

    in_fence = False
    start = None
    start_col = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith(">"):
            continue
        for m in _AGENT_TAG_RE.finditer(line):
            if m.group(1).lower() != tag:
                continue
            start = i
            # A tag at the start of a line (accounting for markdown markers) —
            # take the whole line; a tag mid-sentence — from the tag's position.
            start_col = 0 if _line_leading_tag(line) == tag else m.start()
            break
        if start is not None:
            break
    if start is None:
        return None

    block = [lines[start][start_col:]]
    in_fence = False
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            block.append(line)
            continue
        if not in_fence and not stripped.startswith(">"):
            leading = _line_leading_tag(line)
            if leading and leading != tag:
                break
        block.append(line)
    result = "\n".join(block).strip()
    return result or None


_ACK_ONLY_RE = re.compile(
    r"^\s*(?:[*\-#>\s]+)?@?(?:hux|dro)?\s*(?:[—:\-,]\s*)?"
    r"(?:heard\s+you|agree|agreed|acknowledged|confirmed|accepted|ok|ack|"
    r"no\s+disagreement|echo\s+closed)\b",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"\?|"
    r"\b(?:check|review|answer|summarize|clarify|"
    r"compare|give\s+an\s+opinion|look|break\s+down|take|do|write|"
    r"pass|return|need\s+a\s+look|"
    r"what\s+do\s+you\s+think)\b",
    re.IGNORECASE,
)


def should_dispatch_addressed_block(block: str | None, tag: str) -> bool:
    """Return True when an addressed @block asks the target to act or answer.

    Tags remain the routing primitive, but an ack-only/deescalation block such
    as "@hux - agreed, closing the echo loop" should not start another agent turn.
    """
    tag = (tag or "").lstrip("@").lower()
    if tag in ("boss", "cabinet"):
        return True
    if not block:
        return True

    text = route_tail_visible_text(block).strip()
    if not text:
        return False
    if _ACTION_RE.search(text):
        return True
    return True


def extract_message(text: str) -> str:
    """Extract the message field from routing JSON, or strip the trailing tag."""
    lines = text.splitlines()
    parsed = _parse_routing_tail(text)
    if parsed and "route" in parsed[0]:
        data, start, end = parsed
        msg = (data.get("message") or "").strip()
        if msg:
            return msg
        return "\n".join(lines[:start] + lines[end:]).strip()

    route = parse_route(text)
    if not route:
        return text.strip()
    if lines and route in lines[-1].lower():
        lines = lines[:-1]
    return "\n".join(lines).strip()

_GOL_RUN_MSG_RE = re.compile(
    r"^(?:@gol\b)?[\s\u2014:\-]*run\s+([A-Za-z0-9_.\-]+)[\s.]*$",
    re.IGNORECASE,
)


def parse_gol_run_message(text: str) -> str | None:
    """Deterministic delegation to Golem-the-machine-tool: '@gol run <action_id>'
    (or 'run <action_id>' from routing JSON's message field) executes as code
    through a project action; Golem's LLM is never invoked. Only an exact
    single-line match goes down the machine path; any other text goes to the
    LLM adapter."""
    m = _GOL_RUN_MSG_RE.match((text or "").strip())
    return m.group(1) if m else None
