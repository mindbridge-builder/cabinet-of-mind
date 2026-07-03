import sys
import unittest
from unittest import mock

import core.work_templates as work_templates
from core.work_templates import WorkTemplateError, compile_work_template, template_ids


class WorkTemplateTests(unittest.TestCase):
    def setUp(self):
        self.variables = {
            "cabinet_root": "C:/cabinet_of_mind",
            "project_root": "C:/cabinet_of_mind",
            "python": sys.executable,
        }

    def test_pytest_template_compiles_to_argv(self):
        item = compile_work_template("pytest", {"args": ["tests/test_work_templates.py"]}, self.variables)

        self.assertEqual(item["template_id"], "pytest")
        self.assertEqual(item["command"], [sys.executable, "-m", "pytest", "tests/test_work_templates.py"])
        self.assertEqual(item["cwd"], "${project_root}")

    def test_python_module_rejects_non_module_name(self):
        with self.assertRaises(WorkTemplateError):
            compile_work_template("python_module", {"module": "bad;name"}, self.variables)

    def test_python_module_rejects_unlisted_entrypoint(self):
        with self.assertRaises(WorkTemplateError):
            compile_work_template("python_module", {"module": "os"}, self.variables)

    def test_python_module_allows_explicit_entrypoint(self):
        # The shipped allowlist is empty; installations add their own modules.
        with mock.patch.object(
            work_templates, "_ALLOWED_PYTHON_MODULES", {"myproject.nightly_batch"}
        ):
            item = compile_work_template(
                "python_module",
                {"module": "myproject.nightly_batch", "args": ["--help"]},
                self.variables,
            )

        self.assertEqual(
            item["command"],
            [sys.executable, "-m", "myproject.nightly_batch", "--help"],
        )

    def test_unknown_params_are_rejected(self):
        with self.assertRaises(WorkTemplateError):
            compile_work_template("pytest", {"command": ["git", "commit"]}, self.variables)

    def test_script_path_must_be_project_relative(self):
        with self.assertRaises(WorkTemplateError):
            compile_work_template("python_script", {"script": "../worker.py"}, self.variables)

    def test_python_script_rejects_unlisted_entrypoint(self):
        with self.assertRaises(WorkTemplateError):
            compile_work_template("python_script", {"script": "scripts/worker.py"}, self.variables)

    def test_registry_exposes_only_known_ids(self):
        self.assertIn("pytest", template_ids())
        self.assertNotIn("run_command", template_ids())


if __name__ == "__main__":
    unittest.main()
