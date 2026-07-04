# Cabinet of Mind

## Why

Confession: I'm a vibe coder. I don't really read code, I read vibes. The code happens, I nod, it runs or it doesn't, and either way I tell the model "great, now fix it."

So the day something looked off, I did what we all do — I went for a second opinion. Asked Claude. Claude sounded confident. Asked GPT. GPT sounded *equally* confident, and disagreed. Cool. So now I'm a human USB cable, ferrying paragraphs between two tabs — "he said you said" — pasting and re-pasting until both of them are arguing with a version of each other from four messages ago. I wasn't moderating a debate. I was the debate's worst translator.

Fine, I thought, I'll be a Real Developer and use the API. That lasted a day. The meter starts the second you breathe. Every dumb question prints a receipt. I caught myself rationing — *"is this worth a token?"* — which is a deeply stupid way to be curious. I already pay for the subscriptions. Why am I paying twice for the privilege of feeling guilty?

So Cabinet just uses the subscriptions I already have. No meter. There's a little five-hour window that refills like a tide, and I watch that instead of a bill. Turns out I think better when nobody's charging me by the thought.

And the real fix for the second-opinion circus: put both of them in the same room. Let them read each other. Let them disagree to each other's faces instead of through me. Because — the part I didn't see coming — the spot where the two smart ones start arguing is usually the exact spot where I was about to do something dumb. Now I just sit in the middle, like the parent who has to pick a side. Owner of the decision, blissfully unqualified.

And the typing? Not them. That's the hands — a little local model on my own machine: free, tireless, and a bit of a liar. It'll cheerfully report "done, committed, tested!" having done none of it. So it gets the one rule I actually enforce: show me the tool you used, or it didn't happen.

One room. One memory. Two smart ones who finally argue with each other instead of through me, a local intern who does the typing (supervised), and me — still picking sides, still clueless, finally not the cable.

---

Cabinet of Mind is a local-first cabinet of minds for vibe coders.

It is not a cloud platform, not SaaS, not an enterprise "agentic platform", and not a network service meant to be exposed to the internet. It is a local cockpit that runs on your own PC, in your own folders, with your own tools, models, keys, terminals, and responsibility.

The idea is simple: put a small council of minds on your machine and let them think, route, execute, ask for approval, and show real work in progress.

**This repo ships a principle. The code is an appendix — the principle doesn't need it.** The eight ideas that make a cabinet work live in **[PRINCIPLE.md](PRINCIPLE.md)**: how stateless model CLIs share one memory, how `@`-tags route work, how sages and hands divide labor, how to catch a local model that lies about what it did. The receipts — five true stories of things breaking, each the reason a principle exists — are in **[CASEBOOK.md](CASEBOOK.md)**. Read this to understand *what*, PRINCIPLE for *why*, CASEBOOK for *how do I know it's true*.

> The reference build is included — the actual code this cabinet runs on, tests and all. Windows-first (the principle is portable; macOS and Linux need a few obvious swaps — see [SETUP.md](SETUP.md)). It is not a product, it is *evidence*: one working instance of the principle, offered so you can read it, run it, or rip it apart for your own.

## Core Idea

Cabinet is for people who already run things locally:

- Claude Code
- Codex
- Ollama
- git
- scripts
- terminals
- local project folders

Cabinet gives that workflow a shared room.

Sages think. Hands execute. The human approves. Long-running work is tracked with logs, status, stdout, stderr, and artifacts.

## Positioning

Cabinet is a local AI cabinet for vibe coders.

Install it on your own PC, wire it to your own tools, and use it as a personal operating room for code, research, scripts, and project work.

It is deliberately local. If you give it write tools, it can write. That is the point.

Do not expose it to the internet. Do not treat it as a hardened security product. Do not pretend it is a compliant enterprise agent platform.

## The Cast

Cabinet is role-based, not model-based.

The important unit is not "which LLM answered", but what role the resident plays:

- sages reason, review, plan, and route;
- hands execute concrete work in the filesystem and terminal;
- the human remains the owner of decisions and approvals;
- work runtime tracks long processes separately from the chatter.

This makes Cabinet different from the multi-agent frameworks. It is less about models talking forever and more about a local household that turns discussion into visible work.

## Why this README avoids the word "agent"

On purpose. I tried the agentic thing: gave a local model freedom and a shell. It lied about commits, claimed other people's work as its own, and one evening quietly edited its own config file. So Cabinet takes the agency *out*: minds argue, code orchestrates, execution is deterministic wherever the task is known, and nothing is true without a tool call. Turns out a cabinet works better than an agent.

## Make It Yours

Cabinet is meant to be adapted, not run untouched. Everything concrete is a swappable choice:

- **Names and characters.** The reference build has Huxley (sage), Diderot (sage), and Golem (hands). Yours could be Linda, Basik, and Rumpel, with their own personalities — characters are plain Markdown files, one per resident.
- **Models.** The two sages are the must-have: strong frontier minds on the subscriptions you already pay for (the reference wires Claude Code and Codex). The hands are the opposite — deliberately replaceable: **any local coder model** Ollama can serve, chosen to fit your GPU. Set it in `CABINET_REALITY.json`, or the `CABINET_HANDS_MODEL` environment variable, or the one `FROM` line in [models/golem.hands.Modelfile](models/golem.hands.Modelfile). A 7B coder on an 8 GB card is a legitimate cabinet.
- **The chat UI.** It is a single HTML file. Rewrite it however you like.
- **The CLI.** Any prompt-in / text-out tool can be a sage; implement one small adapter interface.
- **The rules.** Who can call whom, and when the human must approve, is a data file you own.

You bring the tools, the models, and the cast. Cabinet brings the organizing principle that lets them share one room. See **[PRINCIPLE.md](PRINCIPLE.md)** for the full breakdown and a map of where each piece lives.

## Work Runtime

Long work should not disappear into a chat bubble.

Cabinet tracks work as process-backed jobs:

- status
- progress
- stdout
- stderr
- logs
- artifacts
- cancel controls

This is the bridge between vibe coding and actual operations. The cabinet can discuss a task, hand it to an executor, launch a long-running process, and keep the human informed while it runs.

## Safety Framing

The safety answer is locality.

Cabinet is designed for a single person running it locally. The machine owner decides what tools are available and what folders are writable.

The correct default story is:

- local-only by design;
- no public network exposure;
- no cloud control plane;
- no multi-tenant assumptions;
- no hidden promise that write-enabled hands are harmless.

If a hand has write access, it can write. If it has shell access, it can run commands. Cabinet's job is to make that work visible, role-based, and approval-aware, not to turn local automation into a fake sandbox.

## Audience

Cabinet is for vibe coders and technical operators who want a local council of minds around their existing workflow.

It is for people who are comfortable with local tools, git, terminals, scripts, and model CLIs, and who want a warmer, more structured room to run them in.

## Honest Risks

Every repository says "trust me." This one tells you where it can break:

1. **The CLIs are moving targets.** The sages ride on Claude Code CLI and Codex CLI — official tools that Anthropic and OpenAI change without asking us. A breaking change in their flags or output format breaks the adapters until they're patched. This is the price of the no-meter principle: the subscription CLIs are the only legal door to flat-rate frontier models.

2. **The shell is stronger than the sandbox.** Write tools are checked against `allowed_roots`, but `bash_run` executes real commands — a Python path check cannot contain a shell. Cabinet's honesty layer will *report* what ran; it will not *prevent* a destructive command. The safety story is locality and your own restraint in what you grant. If you want hard isolation, run the whole cabinet in a container — that's on the roadmap as an option, not a default.

3. **Arguments are budgeted, not endless.** If you worry that mandatory disagreement (Principle 8) means infinite bickering: every sage turn carries a visible `CABINET_TURN_BUDGET` counter, most decisions close in 2–4 exchanges, and at the ceiling the system forces a final synthesis to the Boss instead of another round.

4. **Small models mean small patches — that's the model's ceiling, not the system's.** Cabinet's architecture places no limit on the hands' model size — any Ollama-served coder model works, from 7B to 70B+. What varies is reliability: a small model on a modest GPU will fail more often on complex patches than a large one; the harness compensates with short surgical diffs, a test referee, and bounded retries, and it refuses jobs that are too big rather than mangling them. Bigger card, bigger model, fewer retries needed — the ceiling is yours to raise.

5. **Threading and asyncio live under one roof.** The server mixes an async WebSocket loop with a threaded HTTP server and thread locks. For a single-user local tool this is pragmatic and tested; if the project grows, a pure-async rewrite is the known path.

## One-Line Pitch

A cozy local cabinet for vibe coders: sages argue, hands execute, **nothing is true without a tool call** — and you own every decision.

No cloud. No meter. No agents.
