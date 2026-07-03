# Cabinet Project Map

The stable architecture map. The residents read this before making claims
about the system, and append a short durable note here after significant
changes (what changed, why, which files, how it was verified). Session
chatter never goes here — only architecture that will still be true next month.

## Roles

| Tag | Role | Runs on |
|-----|------|---------|
| `@hux` | sage — reasons, reviews, argues | Claude Code CLI (subscription) |
| `@dro` | sage — reasons, reviews, argues | Codex CLI (subscription) |
| `@gol` | hands — executes narrow, verifiable work | any local coder model via Ollama |
| `@boss` | the human — owns every decision | you |

## Key pieces

- `server.py` — the room: local HTTP + WebSocket server, chat state, pending
  approvals, health checks. Local-only (127.0.0.1), token-authenticated.
- `core/dispatcher.py` — routing between residents: @-tags, routing JSON,
  turn budget, forced synthesis at the discussion ceiling, phantom-claim gate.
- `core/routing.py` — the tag/JSON grammar; `@gol run <action_id>` machine path.
- `core/validators.py` — trust layer: phantom-claim detection, observed
  reports built from the tool journal, never from the model's prose.
- `core/work_runtime.py` + `core/work_store.py` — long jobs as tracked
  processes: status, logs, artifacts, cancel.
- `adapters/` — one adapter per resident (Claude Code CLI, Codex CLI, Ollama).
- `hands_probe/` — the patch harness: search/replace patches, pytest referee,
  bounded retries.
- `prompts/` — the cast: one Markdown personality per resident, plus the
  bootstrap index and the high-risk gate.
- `ui/index.html` — the whole chat UI, one file.
- `CABINET_REALITY.json` (+ gitignored `.local.json`) — wiring: models,
  allowed roots, ports.
- `ROUTING_CONTRACT.json` — who may call whom, as data.

## Contracts worth knowing before editing

- Nothing is true without a tool call: reports come from the tool journal.
- Hands never write their own configuration (prompts, model files, contracts).
- Repeatable work goes through WorkRuntime, not freestyle tool loops.
- Consensus must be earned: a sage may not agree before raising an objection.

## Change log

- (append durable notes here: what changed, why, which files, verification)
