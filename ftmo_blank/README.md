# ftmo_blank: a self-calling FTMO-challenge agent, paper only

The agent starts with no strategy. It has the field (researched facts about the challenge rules, candles,
costs, time, sizing, measurement), a paper broker, a goal ladder computed by code, and the program that
calls Claude for each event. It decides, journals, reviews, writes papers, and can edit its own routing.

Nothing here connects to a broker. The live MT5 stack in `~/deltaForceFTMO` is not touched.

## Layout

| path | who writes it | what it is |
|---|---|---|
| `manual/OPERATOR_MANUAL.md` | operator | the tools, fill model, rules, time, events, kinds, steering protocol |
| `field/` | the program (research kind) | researched facts with sources; `INDEX.md` first |
| `guard/rules.py` | operator, read-only for the agent | hard limits, goal verifiers, order checks |
| `guard/policy.py` | operator, read-only for the agent | kinds: model, effort, tools, permissions, budgets, spawn rules |
| `guard/MANIFEST.json` | `driver.py guard-write` | sha256 of guard files; a turn refuses to run if they changed |
| `tools/paper.py` | operator | the paper broker and the agent's Bash interface |
| `tools/codetool.py` | operator | backup, selftest, guard-check for the evolve and verify kinds |
| `agent_code/routing.py` | agent (evolve kind) | which event wakes which kind, and the prompt it gets |
| `agent_code/versions/` | codetool | backups taken before every edit |
| `papers/` | agent (write kind) | written insights, postmortems, final report |
| `feed/EURUSD_H1.csv` | operator | read-only copy of the H1 bars from `data/study_bars/EURUSD_H1.npy` |
| `driver.py` | operator | bootstrap, research, field, turn, run, status |
| `state/`, `runs/`, `workspace/` | runtime | account, ledger, outbox, engine database, run records |

## Commands

```
python3 driver.py guard-write          # once, at bootstrap
python3 driver.py research             # queue and run the field research (real Claude; costs money)
python3 driver.py field                # build field/*.md and INDEX.md from finished research
python3 driver.py turn --bars 24       # one turn: advance one day of candles, route events, reassess
python3 driver.py run --turns 10       # ten turns; stops at replay end or on a halt
python3 driver.py status               # account, goals, engine caps and cost
python3 tools/codetool.py selftest     # all unit tests, plus the guard check
```

## How one turn works

1. `verify_guard`: refuse to trade if any guard file changed since `guard-write`.
2. The replay advances `--bars` hourly candles. The broker processes fills, stops, targets, day closes, rule checks.
3. Each routed event in the outbox (`trade_closed`, `day_closed`, `risk_warning`, `rule_breach`, `goal_passed`, `replay_end`)
   goes to the router in `agent_code/routing.py`. It returns a kind and a prompt, or `None` for no model call.
4. A `turn_reassess` demand is queued first, so the decision runs before the reviews of what just happened.
5. The engine drains the queue. Every follow-up the answers spawn runs under the same policy and caps.
6. The turn is appended to `state/turns.jsonl`.

## Controls

- Stop everything: `python3 -c "import sys; sys.path.insert(0,'~/selfcall'); from selfcall.engine import Engine; from pathlib import Path; Engine(home='~/ftmo_blank').stop_file.write_text('stop\n')"`
  (or `touch ~/ftmo_blank/state/STOP`). Remove the file to resume.
- Caps: `guard/policy.py` `LIMITS` (runs, total cost, depth, children). The default total cost cap is 60 USD.
- Model and effort per kind: `guard/policy.py`. Opus at max effort is refused by the engine.

## Verified

- `tests/test_paper.py`: 16 tests. Fills, half spread, stop vs target in one bar, gap through stop, every refusal reason,
  worst-case day gate, daily and total breach, warning once, day record, journal goal, no widening, replay end, outbox.
- `selfcall/tests`: 21 tests for the engine, including the Opus-max refusal and per-kind model and effort in argv.
- Dry run of the full loop in a scratch copy with the fake `claude`: a placed order closed, its `trade_closed` routed to a review,
  the traded day's `day_closed` routed, untraded days routed to nothing, reassess ran every turn.

## Known limits (assumptions, all written in the code and the field notes)

- The feed is replay only. There is no live feed on purpose: the live MT5 bridge belongs to the other stack.
- Spread (1 pip) and commission (6 USD round turn per lot) are assumptions until `field/instruments_costs.md` is verified.
- Broker clock is taken as UTC+3 (evidence in `guard/rules.py`). The day boundary is Prague midnight, equity-based daily loss.
- Weekend and news rules of real FTMO accounts are not simulated.
