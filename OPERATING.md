# Cabinet Operating Notes

## Project Boundaries

Cabinet is the local runtime. Active projects own their code, data, manifests, plans, and external account requirements.

When working in another repository, update that repository's docs only when the architecture or operating contract changes. Do not keep a manual commit ledger in Cabinet.

## Commit Discipline

- Commit Cabinet changes in `C:\cabinet_of_mind`.
- Commit active-project changes in that project repository.
- Do not mix platform changes and project changes in one commit.
- Leave unrelated dirty files alone and report them.
- Do not claim a commit happened unless `git commit` succeeded.

## Runtime State

Local install state belongs in ignored files or environment variables:

- `CABINET_REALITY.local.json`
- `CABINET_ACTIVE_PROJECT`
- tokens, logs, work state, pending queue

Tracked defaults must stay project-neutral.

## Remote Model Runtimes

Remote model hosts are allowed for local agents when they improve reliability
or resource isolation. For Golem, the model may run on a second workstation
over an SSH tunnel, while all tools, filesystem writes, git operations,
WorkRuntime state, validation, and observed reports stay on the main Cabinet
machine. A remote model response is still not an execution fact.

Remote Golem must remain in execution quarantine until the following are true:

- Ollama step telemetry records `prompt_eval_count`, `eval_count`, `num_ctx`,
  and prompt context pressure.
- Tool results returned to the model are size-bounded and explicitly marked
  when truncated.
- Qwen/Ollama text tool calls are normalized into assistant `tool_calls`
  history before tool results are appended.
- A multi-step remote smoke test passes with observed facts, no duplicate tool
  loop, acceptable context pressure, and acceptable GPU/offload behavior.

Context pressure is measured per current request:

```text
prompt_context_ratio = prompt_eval_count / num_ctx
```

`eval_count` is logged separately and is not added to the context-ratio
threshold. The default warning threshold is `0.85`.
