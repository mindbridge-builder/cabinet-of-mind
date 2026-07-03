"""Template registry for manifest-backed WorkRuntime actions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class WorkTemplateError(ValueError):
    pass


_TEMPLATE_IDS = {
    "noop",
    "pytest",
    "python_module",
    "python_script",
    "git_status",
    "git_diff",
}

# Allowlist of python modules the sages may launch as long-running work.
# Populate per active project; an empty set means "no module-style work yet".
# Example: "myproject.nightly_batch"
_ALLOWED_PYTHON_MODULES: set[str] = set()
_ALLOWED_PYTHON_SCRIPTS: set[str] = set()

_COMMON_PARAMS = {"title", "cwd", "env", "handoff"}
_PARAMS_BY_TEMPLATE = {
    "noop": set(),
    "pytest": {"args"},
    "python_module": {"module", "args"},
    "python_script": {"script", "args"},
    "git_status": set(),
    "git_diff": {"paths"},
}


def template_ids() -> list[str]:
    return sorted(_TEMPLATE_IDS)


def expand_template_value(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        expanded = value
        for key, replacement in variables.items():
            expanded = expanded.replace("${" + key + "}", replacement)
        return expanded
    if isinstance(value, list):
        return [expand_template_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: expand_template_value(item, variables) for key, item in value.items()}
    return value


def compile_work_template(template_id: str, params: dict[str, Any] | None, variables: dict[str, str]) -> dict[str, Any]:
    template_id = str(template_id or "").strip()
    if template_id not in _TEMPLATE_IDS:
        raise WorkTemplateError(f"unknown work template: {template_id}")
    params = expand_template_value(params or {}, variables)
    if not isinstance(params, dict):
        raise WorkTemplateError("template params must be an object")
    allowed = _COMMON_PARAMS | _PARAMS_BY_TEMPLATE[template_id]
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise WorkTemplateError(f"unsupported params for {template_id}: {', '.join(unknown)}")

    cwd = str(params.get("cwd") or "${project_root}")
    env = params.get("env") if isinstance(params.get("env"), dict) else None
    title = str(params.get("title") or _default_title(template_id, params))

    if template_id == "noop":
        command = [
            variables["python"],
            "-c",
            "import json; print(json.dumps({'type':'result','summary':'noop'}), flush=True)",
        ]
    elif template_id == "pytest":
        command = [variables["python"], "-m", "pytest", *_string_list(params.get("args"), "args")]
    elif template_id == "python_module":
        module = _module_name(params.get("module"))
        if module not in _ALLOWED_PYTHON_MODULES:
            raise WorkTemplateError(f"python_module entrypoint is not allowed: {module}")
        command = [variables["python"], "-m", module, *_string_list(params.get("args"), "args")]
    elif template_id == "python_script":
        script = _project_relative_path(params.get("script"), "script")
        if script not in _ALLOWED_PYTHON_SCRIPTS:
            raise WorkTemplateError(f"python_script entrypoint is not allowed: {script}")
        command = [variables["python"], script, *_string_list(params.get("args"), "args")]
    elif template_id == "git_status":
        command = ["git", "status", "--short"]
    elif template_id == "git_diff":
        command = ["git", "diff", "--", *_path_list(params.get("paths"), "paths")]
    else:  # pragma: no cover - guarded by template_id validation
        raise WorkTemplateError(f"unknown work template: {template_id}")

    result = {
        "template_id": template_id,
        "title": title,
        "command": command,
        "cwd": cwd,
    }
    if env:
        result["env"] = {str(key): str(value) for key, value in env.items()}
    if isinstance(params.get("handoff"), dict):
        result["handoff"] = dict(params["handoff"])
    return result


def _default_title(template_id: str, params: dict[str, Any]) -> str:
    if template_id == "python_module":
        return f"python -m {params.get('module')}"
    if template_id == "python_script":
        return f"python {params.get('script')}"
    return template_id


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkTemplateError(f"{field} must be an array of strings")
    return list(value)


def _path_list(value: Any, field: str) -> list[str]:
    items = _string_list(value, field)
    for item in items:
        _project_relative_path(item, field)
    return items


def _module_name(value: Any) -> str:
    module = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", module):
        raise WorkTemplateError("module must be a dotted Python module name")
    return module


def _project_relative_path(value: Any, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise WorkTemplateError(f"{field} is required")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise WorkTemplateError(f"{field} must be a project-relative path")
    return raw
