# selfcall: a program that calls Claude on demand, and lets each answer ask for more

Stdlib Python, no framework. Claude is invoked as `claude -p` (print mode) once per demand.

## What it does

1. A **demand** is a request for one Claude invocation: `kind` + `prompt` + optional `context`.
2. The **engine** runs the demand with that kind's model, effort, tools and budget, and records the answer.
3. The answer is schema-checked (`status`, `summary`, `findings`, `sources`, `next_demands`).
4. Each `next_demands` item is a **follow-up** the engine accepts or rejects by policy, then runs. So one
   answer can cause more calls, which can cause more, within hard limits.
5. An **event** (a trade closed, a day ended, a goal passed) is routed to a demand by a **router** function.

The model proposes; the engine decides. The model never chooses tools, budgets, or models for a follow-up.

## Hard rules (enforced in code, `selfcall/engine.py`)

- A kind may spawn only the kinds in its `may_spawn`.
- Depth, runs, total cost, children per answer and prompt size are capped (`LIMITS`, overridable per home).
- Duplicate follow-ups are refused, so a loop cannot repeat the same question.
- `STOP` file halts before the next call. `resume` clears it.
- Opus at max effort is refused (`policy_check`), at start and again at every call. Other models at max are allowed.
- Permission denials are recorded per run, so a blocked action shows up in the record.
- A single worker holds a lock; a crash mid-call is recovered as `failed` on the next start.

## Kinds: model and effort per kind

`KINDS` maps a kind to `model`, `effort`, `tools`, `allow` (permission patterns), `budget_usd`, `may_spawn`.
Models and effort can differ per kind, so different models do different work and hand off to each other.

| kind | default model | effort | tools |
|---|---|---|---|
| research | sonnet | high | WebSearch, WebFetch |
| work | sonnet | high | Read, Glob, Grep |
| reflect | haiku | low | none |

Opus is allowed at any effort below max. Max is allowed for sonnet and haiku.

## Use

```
python3 -m selfcall enqueue research "question"     queue a root demand
python3 -m selfcall emit trade_closed '{"id": 7}'    route an event
python3 -m selfcall drain                            run until idle or halted
python3 -m selfcall serve --poll 5                   long-running worker
python3 -m selfcall status | show <id>               ledger and run records
python3 -m selfcall stop | resume                    halt / release
```

Run records land in `runs/<id>.json` and `runs/<id>.md`, with the answer, spawned and rejected follow-ups,
and permission denials.

## Routing and homes

`Engine(home, kinds=..., router=..., limits=...)` lets one program host different agents. `home` holds its own
state, runs and workspace. `router(event, payload) -> (kind, prompt) | None` decides what an event wakes.
The FTMO field agent in `~/ftmo_blank` is one such home.

## Tests

```
python3 -m unittest tests.test_engine        # 21 tests; uses tests/fake_claude.py, no tokens
```

`tests/fake_claude.py` answers in the same envelope shape as the real CLI, so the plumbing (argv policy,
envelope parse, spawn validation, limits, failures) is tested without spending money.

## Verified live

- A research demand ran the real `claude -p` with the schema contract and recorded cost and sources.
- A research parent spawned a reflect child that ran with the parent's context.
- A reflect parent that tried to spawn a reflect was rejected by policy (`reflect may not spawn reflect`).
- `--effort max` was accepted for haiku.
