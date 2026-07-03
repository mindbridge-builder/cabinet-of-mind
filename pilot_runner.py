# -*- coding: utf-8 -*-
"""Golem evidence pilot: medium-nudge coding tasks through the REAL @gol patch path.

Runs ONE task per invocation (python pilot_runner.py --task N), through
OllamaAdapter.call() — the same battle path a sage delegation takes — against
the configured local hands model. Appends a JSON evidence record to
notes/golem_pilot_evidence.jsonl and writes notes/pilot_task_<N>.done.

Gate under test (v4): >=80% eventual-green within <=5 attempts, wall <=10 min/task.
"""
import argparse
import json
import time
from pathlib import Path

from adapters.ollama import OllamaAdapter

ROOT = Path(__file__).resolve().parent
NOTES = ROOT / "notes"

FILES = "[hands_probe/text_tools.py, tests/test_hands_probe.py]"
VERIFY = "python -m pytest tests/test_hands_probe.py tests/test_hands_probe_oracle.py -q"
FILES_B = "[hands_probe/format_tools.py, tests/test_hands_format.py]"
VERIFY_B = "python -m pytest tests/test_hands_format.py -q"

TASKS = {
    1: ("normalizers",
        "Add three functions to hands_probe/text_tools.py: normalize_email(value) "
        "(strip, lowercase; None/blank/non-string -> None), normalize_phone(value) "
        "(keep only digits and a leading plus; None if fewer than 7 digits remain), "
        "normalize_name(value) (strip, collapse inner whitespace runs to one space; "
        "None/blank -> None). Preserve every existing function and test. Add focused "
        "tests for all three to tests/test_hands_probe.py."),
    2: ("csv_roundtrip",
        "Add candidate_to_csv_row(info) and candidate_from_csv_row(row) to "
        "hands_probe/text_tools.py. Fixed field order: name, company, email, phone. "
        "to_csv_row returns a list of 4 strings (missing/None -> empty string); "
        "from_csv_row accepts a list of 4 strings and returns a dict with those keys "
        "(empty string -> None). Round-trip must be lossless for present fields. "
        "Preserve all existing functions and tests. Add round-trip tests."),
    3: ("validators",
        "Add is_valid_email(value) and is_valid_phone(value) to "
        "hands_probe/text_tools.py. Email valid: string containing exactly one '@' "
        "with a non-empty local part and a domain containing at least one dot. Phone "
        "valid: after keeping digits only, at least 7 digits. None/blank -> False. "
        "Preserve all existing functions and tests. Add parametrized-style tests "
        "covering valid and invalid cases for both."),
    4: ("redact",
        "Add redact_contact(info) to hands_probe/text_tools.py: returns a COPY of "
        "the dict; if 'email' is a non-blank string, replace it with first character "
        "+ '***@' + domain part; if 'phone' is a non-blank string, replace it with "
        "'***' + last 4 digits (digits only considered). Other keys unchanged; input "
        "dict must not be mutated. Preserve all existing functions and tests. Add "
        "tests including the no-mutation check."),
    5: ("merge_candidates",
        "Add merge_candidates(primary, secondary) to hands_probe/text_tools.py: "
        "returns a new dict with the union of keys; for each key prefer the value "
        "from primary when it is a non-blank string, else take secondary's value. "
        "Neither input may be mutated. Preserve all existing functions and tests. "
        "Add tests covering prefer-primary, fallback-to-secondary and no-mutation."),
}

TASKS_B = {
    6: ("slugify",
        "Add slugify(value) to hands_probe/format_tools.py. It returns a URL "
        "slug: strip and lowercase string input, replace each run of non-alphanumeric "
        "characters with one hyphen, strip leading/trailing hyphens, and return an "
        "empty string for None, non-string, blank, or no alphanumeric content. "
        "Preserve every existing function and test. Add focused tests to "
        "tests/test_hands_format.py."),
    7: ("phone_display",
        "Add format_phone_us(value) to hands_probe/format_tools.py. Keep digits only. "
        "For 10 digits return '(123) 456-7890'. For 11 digits starting with 1 return "
        "'+1 (234) 567-8901'. Return None for None, non-string, blank, or any other "
        "digit count. Preserve every existing function and test. Add focused tests."),
    8: ("date_iso",
        "Add normalize_date_iso(value) to hands_probe/format_tools.py. Accept string "
        "dates in 'YYYY-MM-DD', 'DD.MM.YYYY', and 'MM/DD/YYYY' formats and return "
        "'YYYY-MM-DD'. Return None for None, non-string, blank, impossible dates, or "
        "unsupported formats. Preserve every existing function and test. Add focused "
        "tests including an invalid leap-day case."),
    9: ("dedupe_labels",
        "Add dedupe_labels(labels) to hands_probe/format_tools.py. Return a new list "
        "of stripped string labels, preserving first occurrence order and de-duping "
        "case-insensitively. Ignore None, non-strings, and blank strings. Preserve "
        "every existing function and test. Add focused tests covering order, casing, "
        "blank values, and no mutation of the input list."),
    10: ("markdown_table",
        "Add render_markdown_table(rows, columns) to hands_probe/format_tools.py. "
        "rows is a list of dicts and columns is a list of column names. Return a "
        "GitHub-style markdown table with header, separator, and one row per input "
        "row. Missing/None values render as empty strings. Cell values are stripped "
        "strings and pipe characters are escaped as '\\|'. Return an empty string "
        "when columns is empty. Preserve every existing function and test. Add "
        "focused tests."),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, required=True)
    args = ap.parse_args()
    if args.task in TASKS_B:
        task_id, scope = TASKS_B[args.task]
        files = FILES_B
        verify = VERIFY_B
        pilot = "golem_evidence_20260702_v4b"
    else:
        task_id, scope = TASKS[args.task]
        files = FILES
        verify = VERIFY
        pilot = "golem_evidence_20260702_v4"

    NOTES.mkdir(exist_ok=True)
    tag = (
        f"@gol patch mode=known-files files={files} "
        f'verify="{verify}" scope="{scope}"'
    )

    adapter = OllamaAdapter(workspace=ROOT, allowed_roots=[ROOT])
    t0 = time.time()
    result = adapter.call(
        system_prompt="",
        history=[],
        user_message=tag,
        timeout=900,
        allow_write_tools=True,
    )
    elapsed = time.time() - t0

    kp = (result.metrics or {}).get("known_files_patch") or {}
    record = {
        "pilot": pilot,
        "task": args.task,
        "task_id": task_id,
        "status": kp.get("status"),
        "reason": kp.get("reason"),
        "attempt": kp.get("attempt"),
        "attempts_used": kp.get("attempts_used"),
        "attempts": kp.get("attempts"),
        "commit": kp.get("commit"),
        "escalation_path": kp.get("escalation_path"),
        "model_prompt_tokens": kp.get("model_prompt_tokens"),
        "model_eval_tokens": kp.get("model_eval_tokens"),
        "model_eval_tokens_max": kp.get("model_eval_tokens_max"),
        "model_num_ctx": kp.get("model_num_ctx"),
        "model_done_reason": kp.get("model_done_reason"),
        "model_context_total_tokens": kp.get("model_context_total_tokens"),
        "model_context_shift_suspected": kp.get("model_context_shift_suspected"),
        "wall_seconds": round(elapsed, 1),
        "error": result.error,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out = NOTES / "golem_pilot_evidence.jsonl"
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    (NOTES / f"pilot_task_{args.task}.done").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(record, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
