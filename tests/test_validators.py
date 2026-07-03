import tempfile
import unittest
from pathlib import Path

from core.validators import (
    build_observed_report,
    check_phantom_claims,
    format_observed_report,
    strip_self_report_facts,
)


class TestWriteClaimRegex(unittest.TestCase):
    def test_original_words_detected(self):
        text = "I changed the file ui/index.html and saved it"
        warnings = check_phantom_claims(text, ["read_file"])
        self.assertTrue(any("phantom_write" in w for w in warnings))

    def test_implemented_detected(self):
        text = "I successfully implemented the vertical resize change in style"
        warnings = check_phantom_claims(text, ["read_file"])
        self.assertTrue(any("phantom_write" in w for w in warnings))

    def test_enabled_detected(self):
        text = "enabled the vertical resize change in the html file"
        warnings = check_phantom_claims(text, ["read_file"])
        self.assertTrue(any("phantom_write" in w for w in warnings))

    def test_applied_detected(self):
        text = "applied a new style to index.html"
        warnings = check_phantom_claims(text, ["read_file"])
        self.assertTrue(any("phantom_write" in w for w in warnings))

    def test_no_false_positive_with_write_tool(self):
        text = "implemented the resize change in the style of the html file"
        warnings = check_phantom_claims(text, ["write_file"])
        self.assertFalse(any("phantom_write" in w for w in warnings))

    def test_empty_tools_skips_check(self):
        text = "implemented and changed the html file"
        warnings = check_phantom_claims(text, [])
        self.assertEqual(warnings, [])

    def test_empty_tools_strict_flags_completed_work_report(self):
        text = "fixed it\nfiles_changed: a.py\ncommit: 7b18a2c\nverification: tests passed"
        warnings = check_phantom_claims(text, [], strict_no_tools=True)
        warning_types = {w.split(":")[0] for w in warnings}
        self.assertIn("phantom_no_tools", warning_types)
        self.assertIn("phantom_commit_hash", warning_types)

    def test_empty_tools_default_stays_permissive_for_cli_adapters(self):
        text = "fixed it\nfiles_changed: a.py\ncommit: 7b18a2c\nverification: tests passed"
        warnings = check_phantom_claims(text, [], strict_no_tools=False)
        self.assertEqual(warnings, [])

    def test_write_file_unchanged_still_triggers_phantom(self):
        """write_file ran but content was identical — claim is still phantom."""
        text = "I changed the style in ui/index.html"
        warnings = check_phantom_claims(text, ["write_file:unchanged"])
        self.assertTrue(any("phantom_write" in w for w in warnings))

    def test_write_file_changed_true_no_phantom(self):
        """write_file ran with actual mutation — no phantom."""
        text = "I changed the style in ui/index.html"
        warnings = check_phantom_claims(text, ["write_file"])
        self.assertFalse(any("phantom_write" in w for w in warnings))

    def test_deterministic_e2e_phantom_commit_and_write(self):
        """Golem reports 'changed the file and committed' but tools show unchanged write, no bash."""
        text = "I changed the file index.html and committed the changes"
        warnings = check_phantom_claims(text, ["read_file", "write_file:unchanged"])
        warning_types = {w.split(":")[0] for w in warnings}
        self.assertIn("phantom_write", warning_types)
        self.assertIn("phantom_commit", warning_types)


    def test_failed_git_commit_with_reported_hash_is_phantom(self):
        text = "@boss - commit done\n\ncommit: d885f5c\nverification: git add failed"
        warnings = check_phantom_claims(text, ["git_commit:failed"])
        warning_types = {w.split(":")[0] for w in warnings}
        self.assertIn("phantom_commit_hash", warning_types)

    def test_reported_hash_must_match_git_commit_result(self):
        text = "@boss - committed\n\ncommit: d885f5c"
        warnings = check_phantom_claims(text, ["git_commit:success:a964532"])
        self.assertTrue(any("phantom_commit_hash" in w for w in warnings))

    def test_matching_git_commit_hash_is_not_phantom(self):
        text = "@boss - committed\n\ncommit: a964532"
        warnings = check_phantom_claims(text, ["git_commit:success:a964532"])
        self.assertFalse(any("phantom_commit" in w for w in warnings))

    def test_strip_self_report_facts_removes_agent_fact_lines(self):
        text = (
            "@hux - I tried the task\n"
            "files_changed: fake.csv\n"
            "commit: deadbee\n"
            "verification: trust me\n"
            "work_id: fake-work\n"
            "exit_code: 0\n"
        )

        stripped = strip_self_report_facts(text)

        self.assertIn("I tried the task", stripped)
        self.assertNotIn("fake.csv", stripped)
        self.assertNotIn("deadbee", stripped)
        self.assertNotIn("fake-work", stripped)

    def test_observed_report_uses_tool_results_not_text_claims(self):
        journal = [
            {
                "tool": "write_file",
                "args": {"path": "a.txt"},
                "result": {"type": "write_file", "path": "a.txt", "changed": True},
            },
            {
                "tool": "replace_text",
                "args": {"path": "b.txt"},
                "result": {"type": "replace_text", "path": "b.txt", "replaced": 0},
            },
            {
                "tool": "bash_run",
                "args": {"command": "python -m unittest tests.test_validators"},
                "result": {"type": "bash_run", "returncode": 0},
            },
            {
                "tool": "git_commit",
                "args": {"files": ["a.txt"]},
                "result": {"type": "git_commit", "success": True, "commit": "abc1234"},
            },
        ]

        report = build_observed_report(journal)
        rendered = format_observed_report(report)

        self.assertEqual(report["files_changed"], ["a.txt"])
        self.assertEqual(report["commits"], ["abc1234"])
        self.assertEqual(report["file_noops"], ["b.txt no replacements"])
        self.assertIn("python -m unittest", rendered)
        self.assertIn("CABINET OBSERVED REPORT", rendered)

    def test_observed_report_surfaces_write_ahead_crash_attempt(self):
        """Write-ahead journal: tool started but impl raised. The crashed entry
        has no `result` and a top-level `status=error`. The observed report
        must NOT lie that the file was 'unchanged' — file may be partially
        written. Surface the attempt as an error and as an attempted mutation."""
        journal = [
            {
                "tool": "write_file",
                "args": {"path": "victim.txt", "content": "new"},
                "status": "error",
                "error": "OSError(28, 'No space left on device')",
            },
        ]

        report = build_observed_report(journal)
        rendered = format_observed_report(report)

        self.assertNotIn("victim.txt unchanged", report.get("file_noops") or [])
        self.assertTrue(report.get("errors"), "errored tool must surface in errors")
        self.assertTrue(
            any("victim.txt" in e or "write_file" in e for e in report["errors"]),
            f"errors do not reference the crashed write: {report['errors']}",
        )
        self.assertTrue(
            any("victim.txt" in p for p in report.get("files_changed", [])),
            f"crashed write_file path must be surfaced as an attempted mutation, "
            f"not silently dropped; files_changed={report.get('files_changed')}",
        )
        self.assertIn("errors:", rendered)

    def test_observed_report_repeated_tool_keeps_prior_mutations(self):
        """When the run aborts on a repeated-tool loop, prior successful
        write_files must remain in files_changed — the main concern Diderot raised
        ('report says none, but files were changed')."""
        journal = [
            {"tool": "write_file", "args": {"path": "a.txt"}, "status": "done",
             "result": {"type": "write_file", "path": "a.txt", "changed": True}},
            {"tool": "write_file", "args": {"path": "b.txt"}, "status": "done",
             "result": {"type": "write_file", "path": "b.txt", "changed": True}},
            {"tool": "read_file", "args": {"path": "x.txt"}, "status": "done",
             "result": {"path": "x.txt", "content": ""}},
            {"tool": "read_file", "args": {"path": "x.txt"}, "status": "done",
             "result": {"path": "x.txt", "content": ""}},
        ]

        report = build_observed_report(journal)
        rendered = format_observed_report(report)

        self.assertEqual(report["files_changed"], ["a.txt", "b.txt"])
        self.assertIn("a.txt", rendered)
        self.assertIn("b.txt", rendered)
        self.assertNotIn("files_changed: none observed", rendered)

    def test_observed_report_failed_git_commit_is_not_silent(self):
        """A git_commit that returned success=False must be visible in the
        rendered report — at minimum via the errors line. Output must not be
        indistinguishable from 'no commit was attempted'."""
        journal = [
            {"tool": "write_file", "args": {"path": "a.txt"}, "status": "done",
             "result": {"type": "write_file", "path": "a.txt", "changed": True}},
            {"tool": "git_commit", "args": {"files": ["a.txt"], "message": "test"},
             "status": "done",
             "result": {"type": "git_commit", "success": False,
                        "error": "pre-commit hook failed"}},
        ]

        report = build_observed_report(journal)
        rendered = format_observed_report(report)

        self.assertTrue(
            any("git_commit" in e and "pre-commit" in e for e in report.get("errors", [])),
            f"failed git_commit not in errors: {report.get('errors')}",
        )
        self.assertIn("pre-commit hook failed", rendered)


    def test_observed_report_started_write_is_attempted_not_silent(self):
        """write_file with status='started' (process killed / segfault / ollama
        loop exited before recording 'done') must NOT be treated as a clean
        success or silent no-op. File may be partially written."""
        journal = [
            {
                "tool": "write_file",
                "args": {"path": "victim.txt", "content": "new"},
                "status": "started",
            },
        ]

        report = build_observed_report(journal)
        rendered = format_observed_report(report)

        self.assertTrue(
            any("victim.txt" in p and "attempted" in p and "started" in p
                for p in report.get("files_changed", [])),
            f"started write_file path must surface as attempted mutation; "
            f"files_changed={report.get('files_changed')}",
        )
        self.assertTrue(
            any("write_file" in e and "incomplete" in e for e in report.get("errors", [])),
            f"hanging started write_file must surface as incomplete error; "
            f"errors={report.get('errors')}",
        )
        self.assertNotIn("files_changed: none observed", rendered)

    def test_observed_report_other_mutating_tools_started_and_error(self):
        """append_file / replace_text / delete_path must follow the same
        write-ahead contract as write_file — incomplete or errored attempts
        cannot be silently dropped."""
        journal = [
            {"tool": "append_file", "args": {"path": "a.txt"}, "status": "started"},
            {"tool": "replace_text", "args": {"path": "b.txt"}, "status": "error",
             "error": "RegexError"},
            {"tool": "delete_path", "args": {"path": "c.txt"}, "status": "started"},
        ]

        report = build_observed_report(journal)

        files_changed = report.get("files_changed", [])
        self.assertTrue(any("a.txt" in p and "started" in p for p in files_changed))
        self.assertTrue(any("b.txt" in p and "error" in p for p in files_changed))
        self.assertTrue(any("c.txt" in p and "started" in p for p in files_changed))

    def test_observed_report_git_commit_started_is_not_silent(self):
        """git_commit started but never reached terminal status: must be
        visible as an attempted commit with unknown outcome, never indistinguishable
        from 'no commit attempted'."""
        journal = [
            {"tool": "write_file", "args": {"path": "a.txt"}, "status": "done",
             "result": {"type": "write_file", "path": "a.txt", "changed": True}},
            {"tool": "git_commit", "args": {"files": ["a.txt"], "message": "m"},
             "status": "started"},
        ]

        report = build_observed_report(journal)
        rendered = format_observed_report(report)

        self.assertTrue(
            any("attempted" in c and "started" in c for c in report.get("commits", [])),
            f"started git_commit must surface as attempted; "
            f"commits={report.get('commits')}",
        )
        self.assertNotIn("commit: none observed", rendered)
        self.assertTrue(
            any("git_commit" in e and "incomplete" in e for e in report.get("errors", [])),
        )

    def test_observed_report_git_commit_error_surfaces_in_commits_line(self):
        """git_commit that raised mid-flight (status=error, no result) must
        also appear in commits, not only in errors."""
        journal = [
            {"tool": "git_commit", "args": {"message": "m"}, "status": "error",
             "error": "GitError: index locked"},
        ]

        report = build_observed_report(journal)
        rendered = format_observed_report(report)

        self.assertTrue(
            any("attempted" in c and "error" in c for c in report.get("commits", [])),
            f"errored git_commit must surface as attempted; "
            f"commits={report.get('commits')}",
        )
        self.assertNotIn("commit: none observed", rendered)


class TestWriteFileChanged(unittest.TestCase):
    def _make_adapter(self, workspace):
        from adapters.ollama import OllamaAdapter
        return OllamaAdapter(workspace=workspace)

    def test_changed_true_on_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            adapter = self._make_adapter(Path(d))
            result = adapter._tool_write_file({"path": str(Path(d) / "new.txt"), "content": "hello"})
            self.assertTrue(result["changed"])
            self.assertNotIn("warning", result)

    def test_changed_false_on_same_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "same.txt"
            p.write_text("hello", encoding="utf-8")
            adapter = self._make_adapter(Path(d))
            result = adapter._tool_write_file({"path": str(p), "content": "hello"})
            self.assertFalse(result["changed"])
            self.assertIn("warning", result)
            self.assertIn("identical", result["warning"])

    def test_changed_true_on_different_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "diff.txt"
            p.write_text("old", encoding="utf-8")
            adapter = self._make_adapter(Path(d))
            result = adapter._tool_write_file({"path": str(p), "content": "new"})
            self.assertTrue(result["changed"])


if __name__ == "__main__":
    unittest.main()
