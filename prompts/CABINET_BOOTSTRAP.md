# Cabinet Bootstrap Index

This is a compact runtime index for Golem. It is intentionally short; do not
copy full project documents into every prompt.

## Always Know
- Cabinet's root repository is the current workspace.
- Your full role rules are in `prompts/GOL_HANDS.md`.
- Local machine/runtime overrides are in `CABINET_REALITY.local.json`.
- Product defaults are in `CABINET_REALITY.json`.
- The stable architecture map is `cabinet_project_map.md`.
- The current work plan is `PLAN.md`.
- Operator notes are in `OPERATING.md`.

## Read Before Acting
Use `read_file` before answering or changing files when the task depends on:
- the current plan, next steps, or "continue";
- Golem runtime, Ollama endpoint, a remote model host, tunnel, model, or GPU state;
- Cabinet architecture, trust guarantees, observed reports, validators, or work runtime;
- repository structure or historical design decisions.

For those tasks, read the smallest relevant set first:
- `PLAN.md` for current intent and unfinished work;
- `cabinet_project_map.md` for stable architecture;
- `OPERATING.md` for operating rules;
- `CABINET_REALITY.local.json` for local endpoint and active project.

Do not treat this index as proof of current facts. Current facts must come from
runtime, tools, git, config files, or work artifacts.
