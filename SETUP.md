# Setup

This is the honest part.

Cabinet doesn't ship the intelligence — it ships the room. The models, the CLIs, the
subscriptions, the local hardware: you bring those. Without them, Cabinet boots into an
empty room with very polite lighting and nobody home.

So this guide is mostly "make sure your own tools are awake and logged in," and only a
little "run the thing."

---

## 1. What you bring

Cabinet is a coordinator, not a bundle. You need these already installed **and logged in**
on your own machine:

| Tool | Role in Cabinet | Must be… |
|------|-----------------|----------|
| **Claude Code CLI** | a sage | installed + logged into your Claude subscription |
| **Codex CLI** | a sage | installed + logged into your ChatGPT subscription |
| **Ollama** | the hands | installed + at least one coder model pulled |
| **Python 3.11+** | runs Cabinet | on your PATH |
| **git** | the hands commit with it | on your PATH |

The two sages on subscriptions are the must-have — two strong, *different* frontier minds
are the whole point (see [PRINCIPLE.md](PRINCIPLE.md)). The hands are the opposite: **any
local coder model** your GPU can hold. There is no blessed model.

Quick "am I alive?" checks:

```
claude --version
codex --version
ollama list
python --version
git --version
```

---

## 2. Get the code

```
git clone <repo-url>
cd cabinet-of-mind
pip install -r requirements.txt
```

The runtime dependency list is one line (`websockets`); everything else is the Python
standard library. `requirements-dev.txt` adds `pytest` if you want to run the test suite:

```
python -m pytest tests/ -q
```

All tests run offline — no model, no Ollama, no network needed. If they're green, the
room itself is sound before you invite anyone in.

---

## 3. Wire your models

All wiring lives in config, not code. Two layers:

- **`CABINET_REALITY.json`** — tracked, project-neutral defaults. Safe to share.
- **`CABINET_REALITY.local.json`** — gitignored override for anything specific to your
  machine: your model choices, your folders, your ports. Same shape, wins on conflict.

What you configure per role, under `agents`:

- `hux` (sage 1) — a Claude Code model (e.g. `claude-opus-4-7`);
- `dro` (sage 2) — a Codex model (e.g. `gpt-5.5`);
- `gol` (hands) — **any local coder model** (see section 5).

Plus `allowed_roots` — the folders the residents are permitted to read and write. Keep
this list short and deliberate; it is the single most important line in the file.

If the Claude Code CLI isn't on your PATH, point Cabinet at it with the
`CABINET_CLAUDE_EXE` environment variable.

---

## 4. Cast your council

Each resident's personality is a plain Markdown file in `prompts/`:

- `prompts/HUX_HEAD.md` — sage 1: character, voice, how it argues
- `prompts/DRO_HEAD.md` — sage 2: a different temperament (different temperament = better disagreement)
- `prompts/GOL_HANDS.md` — the hands: narrow, literal, honest-by-rule
- `prompts/CABINET_BOOTSTRAP.md` — the house rules every resident reads

The reference prompts are in English, but that's just this build's default —
your cabinet should speak *your* language. Rename Huxley, Diderot, and Golem to whoever your
cast is; the prompts are yours to rewrite entirely, in whatever language you think in.
The structure (two sages who must disagree before they agree, one executor with no
opinions) is the part worth keeping.

---

## 5. Set up the local hands

The hands run on a **local model on your own machine**, served by Ollama: free, private,
no metered calls, doing the repetitive typing while your subscription quota is spent on
the sages' judgment.

**Any coder model works.** Pick one that fits your VRAM:

```
ollama pull qwen3-coder:30b      # ~24 GB
ollama pull qwen2.5-coder:14b    # ~12 GB
ollama pull qwen2.5-coder:7b     # ~8 GB
# or deepseek-coder-v2, codestral, codellama — your card, your call
```

Then either of these:

**Option A — build the alias** (recommended: bakes in sane decoding options).
Edit the one `FROM` line in `models/golem.hands.Modelfile`, then:

```
ollama create golem:hands -f models/golem.hands.Modelfile
```

**Option B — point at the raw model** and skip the Modelfile:

```
set CABINET_HANDS_MODEL=qwen2.5-coder:14b        (Windows)
export CABINET_HANDS_MODEL=qwen2.5-coder:14b     (macOS / Linux)
```

or set `agents.gol.model` in your `CABINET_REALITY.local.json`.

Smaller hands mean shorter reliable patches — that's fine. The harness already refuses
jobs that are too big rather than mangling them, and the sages' judgment doesn't shrink
with the hands' VRAM.

---

## 6. Run it

```
runUI.bat          (Windows)
python server.py   (anywhere)
```

Cabinet starts a local server and opens the chat in your browser, already authenticated —
it generates a private token on first run (`ws_token.txt`, gitignored) and hands it to the
browser for you.

---

## 7. First-run check

The header shows a status dot per resident. Read it before you panic:

- **green** — that resident's CLI/model answered, you're good;
- **red** — usually one of: the CLI isn't logged in, Ollama isn't running, or the model
  name in config doesn't match `ollama list`.

---

## 8. Not on Windows?

The reference implementation is Windows-first and **not yet tested on macOS or Linux**.
The principle is portable; the platform-specific spots are few and known:

1. **the launcher** — `runUI.bat` is trivially replaced by `python server.py`;
2. **the hands' shell** — `bash_run` currently executes through the Windows shell;
   point it at `bash`/`zsh`;
3. **locating the Claude CLI** — already handled: PATH first, `CABINET_CLAUDE_EXE`
   env var as override; only the last-resort fallback path is Windows-shaped.

Pull requests that port these are the most useful thing you could contribute.

---

## 9. Safety, in one breath

Cabinet is **local-first and write-capable on purpose.** If you give a hand write
tools, it can write. If you give it shell access, it can run commands. That's the
feature, not a bug.

- Don't expose it to the internet.
- Keep `allowed_roots` pointed only at folders you're willing to let the residents touch.
- It is not a sandbox and doesn't pretend to be one. The safety story is *locality* —
  your machine, your folders, your call.

---

## Troubleshooting

- **a status dot is red** → check login / Ollama running / model name vs `ollama list`;
- **port already in use** → the launcher frees the old process; or find the PID on 8381
  and kill it;
- **"the report shows `none` but the hand said it did the work"** → working as intended:
  the report is built from what tools actually ran, not from what the model claims;
  see [PRINCIPLE.md](PRINCIPLE.md), Principle 5, and [CASEBOOK.md](CASEBOOK.md), Case 1.
