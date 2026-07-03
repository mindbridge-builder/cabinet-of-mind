# The Principle

Cabinet of Mind is not really a program. It is a **principle** with one reference
implementation attached.

The principle is: *give different LLMs one shared room, a shared memory, a way to
address each other, a clear split between who thinks and who executes, and one
iron rule — what a model **says** is testimony, what its tools **did** is fact —
then keep the human as the owner of decisions.*

Everything below is transferable. The names, the models, the operating system, the
chat UI — all of it is yours to change. What matters is the eight ideas. If you keep
the ideas and replace everything else, you still have a Cabinet.

This document explains each idea: the problem it solves, the mechanism, why it works,
and where it lives in the reference build so you can find and swap it.

**The eight, in one breath — quote them, that's the point:**

1. *The shared log is the mind.*
2. *A tag is a call, not a mention.*
3. *The routing contract is data, not code.*
4. *Sages think, hands execute.*
5. *Nothing is true without a tool call.*
6. *Subscriptions for thinking, free local hands for typing.*
7. *Long work is tracked, not lost in a chat bubble.*
8. *Consensus must be earned, not exchanged.*

Every one of these was paid for by something breaking. The bills are in **[CASEBOOK.md](CASEBOOK.md)**.

---

## Before the mechanics

The human story behind all of this — the copy-paste circus, the API meter, the two
models who finally argue in one room — is told in the [README](README.md). This document
is the *how*: the eight ideas that make it work, each one transferable to your own names,
models, and machine.

---

## Principle 1 — A shared log is the memory

**Problem.** Each CLI model is stateless. You invoke it, it answers, it forgets. Three
different CLIs cannot share a conversation on their own.

**Mechanism.** One append-only log (a JSONL file). Every message — from the human or
any resident — is one line. When you invoke a resident, you replay the relevant history
into its prompt. The log *is* the shared mind; the residents are stateless workers that
read from it and write back to it.

**The unread index.** A resident was not running while the others talked. So before each
turn, give it a compact block: *"here is what was written since your last turn."* Track,
per resident, the id of the last message it has seen. This is what makes a cold, stateless
model behave like a participant who was paying attention.

**Why it works.** The hard part of multi-model collaboration is not the models — it is
the shared state. An append-only log is the simplest possible shared state: durable,
inspectable, model-agnostic, trivially replayable.

**Make it yours.** Any format works (JSONL is just convenient). The two things you must
keep: history is replayed into each call, and each resident gets "what's new since I
last ran." Persist the per-resident "last seen" marker so a restart doesn't replay the
whole history as if it were new.

---

## Principle 2 — `@`-tag is a call, not a mention

**Problem.** In a shared room you need a dead-simple way to say *"this is for you,
act now"* — one that any model writes naturally, with no special syntax to learn.

**Mechanism.** `@name` in a message **invokes** that resident. Writing the bare name
(`Diderot`, `Golem`) without `@` is just talk *about* them — no invocation. Tags inside code
blocks and quotes are ignored, so pasting a log or a snippet doesn't accidentally launch
anyone.

**Why it works.** It mirrors how humans already address each other in a chat. There is
nothing to memorize. A model "wanting to hand off to Diderot" just writes `@dro …` — which is
what it would do instinctively anyway.

**Make it yours.** Keep the one rule sharp: tag = invocation, bare name = mention. That
single distinction prevents both missed handoffs and runaway loops.

---

## Principle 3 — A routing contract, kept as data

**Problem.** Who is allowed to call whom? When does control return to the human? Bury
this in code and it becomes invisible and unchangeable.

**Mechanism.** A small contract (a JSON file) states the rules: which roles exist, who
can route to whom, and — most importantly — *when the human must be asked*. The default
is: residents act freely within the current task; the human is pulled in (a Y/N gate) only
for **critical** actions — destructive, irreversible, secret-touching, publishing
outward, or stepping outside the task.

**Why it works.** Making the contract data, not code, means you can read it, audit it,
and rewrite your own rules without touching the engine. It also forces you to decide,
explicitly, the one thing that actually matters for safety: what is critical enough to
stop and ask.

**Make it yours.** Define your own critical-action list and your own routing graph. A
solo tinkerer might allow everything; a cautious setup might gate every write. The engine
shouldn't care — the contract should.

---

## Principle 4 — Sages think, Hands execute

**Problem.** "Agents" is too vague. The useful question is not *which model answered* but
*what role it plays.*

**Mechanism.** Two kinds of resident.

- **Sages** — reason, review, plan, disagree, decide direction. This is where you spend
  your best (and most expensive) models. They have full power, including the ability to
  read files and run checks mid-thought — because an opinion is worth more when the one
  giving it can verify its own hypothesis.
- **Hands** — execute concrete, one-step work in the filesystem and terminal. Narrow
  scope, no strategy, no scope-creep.

And the best hand is often **no model at all**: when the work is a known, repeatable
action, run it as plain code — deterministic, free, incapable of lying. The model earns
a place in the hands only where generation is genuinely needed.

The human stays the owner of decisions and approvals. Long processes are tracked
separately (see Principle 7).

**Why it works.** It puts disagreement at the center (two sages) while protecting your
scarce resource. On subscriptions, the scarce resource is **quota and attention**, not
dollars — so you let cheap/local hands do the typing and save the sages' budget for
judgment.

**Make it yours.** You decide the cast. One sage or three. Hands can be a local model, or
a cheaper tier of the same CLI. The split is the principle; the line you draw between
"thinking" and "doing" is yours.

---

## Principle 5 — Trust the tool journal, not the model's prose

**Problem.** Weaker models — especially small local ones doing the "hands" job — will
cheerfully report *"done, committed, files written"* when they did nothing. For a user
who can't read the code, an unverifiable lie is the most dangerous failure mode there is.
And you can't out-prompt a model into honesty: scolding it in the system prompt just makes
the lies more polite.

**Mechanism.** Stop asking the model what it did. The model's final text is **not** a
source of truth about execution. Instead, the system watches what tools actually ran and
builds the report itself from that journal — files actually written, the real commit hash,
the real exit code, the real job id. Whatever the model *says* it did is stripped before
the report is shown; only observed facts survive.

A second net sits under that: if the text still claims a mutation that no executed tool
backs, flag it. But the primary move is the inversion — reality is the journal, not the
paragraph.

So the report the human sees is assembled from evidence:

```
files_changed: <from actual write/delete tool results>
commit:        <from the real git result, or none>
verification:  <from what actually ran>
```

…never from the model's own narration of itself.

**Why it works.** You stop arguing with the model about reality. A weak, free, local hand
can be as confidently wrong as it likes — it no longer gets to *author* the record of what
happened. This is the tax you pay for free local execution, and it's the single most
transferable safety idea in the whole project: anyone who wires up local hands hits the
same lying problem, and this is the answer.

One more lock, learned the hard way: **hands never write their own configuration.**
Model profiles, character files, routing contracts are read-only to the executor. An
executor that can edit its own brain will, one quiet evening, do exactly that.

**Make it yours.** The tool names are implementation detail. The principle: **execution
facts come from the tool layer, not the model's words.** The model proposes; the journal
disposes.

---

## Principle 6 — Subscriptions via CLI, free hands locally

**Problem.** You want two strong models in one room without a metered API bill.

**Mechanism.** Reach the strong models through their **CLI tools** (Claude Code, Codex,
etc.), which authenticate against the subscription you already pay for — not a
per-token API key. Do the heavy, repetitive execution with a **local** model (via
Ollama) that costs nothing per call.

**Why it works.** This is the whole economic engine, and it's honest about its trade-off:

- The win: two premium models + unlimited local hands, with no marginal cost.
- The cost: CLIs can't be bundled or shipped; the local model is weaker and needs the
  trust net (Principle 5). The scarce resource becomes your subscription *quota* — which
  is exactly why you offload typing to the free hands and spend quota on opinion.

**Make it yours.** Any CLI that takes a prompt and returns text can be a sage. Any local
runner can be hands. The reference build wires Claude Code + Codex + Ollama because those
are what the author pays for; you wire what *you* pay for.

---

## Principle 7 — Long work is tracked, not lost in a chat bubble

**Problem.** A 40-minute classifier run started inside a chat turn is invisible. The
human stares at a silent screen and can't tell if it's working or hung.

**Mechanism.** Long processes run as tracked **jobs**, not chat messages. Each job exposes
status, progress, stdout, stderr, logs, artifacts, and a cancel control. The chat can
*discuss* a task, hand it to an executor, launch it as a background job, and keep the
human informed while it runs.

**Why it works.** It's the bridge between "vibe coding" and real operations. Discussion
turns into visible, cancellable, inspectable work instead of a frozen bubble.

**Make it yours.** The contract is simple: a long job writes progress/result lines to
stdout, and something surfaces them. Keep that and the rest is your UI.

---

## Principle 8 — Consensus must be earned, not exchanged

**Problem.** Two sages left alone will politely agree. Fast, warm, useless. The echo
chamber doesn't look like failure — it looks like harmony. And the spot where they
*would* have disagreed is usually the exact spot where you were about to do something
dumb.

**Mechanism.** Make agreement expensive. Before a sage may agree, it must raise at
least one concrete objection — a failure mode, a counterexample, a place where the
other's position breaks. The debate ends not when they agree, but when no **new**
objection survives. The final artifact is a **dissent report**: what survived the
attack, what objections remain open, and what actually changed because of the dispute.
Give the exchange a visible turn budget so arguing has a cost — and honest leftover
disagreement is a valid outcome to hand to the human, not a failure.

**Why it works.** You bought two opinions to get disagreement, not chorus. Models are
trained to be agreeable; left alone they converge on the first plausible answer and
call it consensus. Mandatory objection converts politeness into scrutiny. And when both
sages *do* share a blind spot, the dissent report at least shows the human where nobody
looked.

**Make it yours.** The numbers are yours: how many objections, how long the budget.
The invariant to keep: agreement without a survived attack is not consensus, and
disagreement delivered honestly is a *good* outcome.

---

## These are principles, not prescriptions

Everything concrete in the reference build is one instantiation. Swap freely:

| You can change | …without losing the principle |
|---|---|
| **Names & characters** — Huxley, Diderot, Golem → Linda, Basik, Rumpel | Roles (sage/hands) stay |
| **Models** — Claude/GPT/Qwen → whatever you pay for or run | "two opinions + hands" stays |
| **The chat UI** — rewrite `index.html` however you like | The shared log underneath stays |
| **The CLI** — any prompt-in/text-out tool | Implement one adapter interface |
| **The OS** — reference is Windows | Replace the ~3 platform-specific spots |
| **The rules** — your own critical-action list | A routing contract, kept as data, stays |

You are not meant to run this untouched. You are meant to take the eight ideas and build
*your* cabinet around *your* tools. That is the point.

---

## Where each principle lives (reference build)

A map, so you can find and replace each piece:

- **Shared log + unread index** → the dispatcher and storage layer (append-only JSONL,
  per-resident "last seen" marker).
- **`@`-tag protocol** → the routing parser (mention detection, code/quote stripping).
- **Routing contract** → `ROUTING_CONTRACT.json` (data, not code).
- **Sages vs Hands** → the adapter layer; sage adapters vs the hands adapter.
- **Trust the journal, not the prose** → the report is assembled from observed tool
  results; the validators module is the secondary net.
- **Subscriptions + local hands** → the three adapters (CLI-based sages, local hands).
- **Work runtime** → the work-runtime / work-store modules and the job panel in the UI.
- **Consensus must be earned** → the sage character files (the dispute protocol) and
  the dispatcher's turn budget.
- **Characters** → `prompts/*.md` — one file per resident. This is where Huxley becomes
  Linda.

To stand up your own cabinet: pick your cast, write their character files, point each
role at a CLI or local model, set your routing rules, and (if you're not on Windows)
port the platform-specific spots. The engine doesn't change — your wiring does.
