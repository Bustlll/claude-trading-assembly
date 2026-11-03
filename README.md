# claude-trading-assembly

A trading desk made of Claude Code calls. Scouts watch the market, a trader decides, a position manager babysits open
trades, a risk worker guards the account, and reviewers and "dreams" turn closed days into lessons. Each worker is one
`claude -p` call with its own model, effort, tools and budget. A small engine (`selfcall/`) queues the calls, enforces
the limits, and waits out usage limits. It was built to attempt a prop-firm evaluation (FTMO-style rules: daily loss,
max loss, profit target), first on replayed candles ("training"), then on a real MT5 account ("live").

Released free under the MIT license. It is an engineering example, **not a strategy**: read "Results" before
running it with money.

## Results, honestly

- **Live, first week:** 6 trades, +2.64R, almost all from two trades. The weeks after were flat. With 9 live trades in
  total, that is inside luck.
- **Replay training never showed it carried over to live.** Worse, the layers added to make training "learn"
  (lesson cards, calibration, a rejection tracker) slowly taught every worker to skip. Live went from 25 of 249 scout
  hand-offs to 0 of 354 before anyone counted. An ablation on the same 21 market moments, one prompt layer at a time,
  found every layer cut entries. The lesson library cut them most.
- **The model remembers the replay.** With no tools, the worker model recalled 2025 gold month-end prices. Results on
  dated history the model has seen can't be trusted.
- **Blind test (`tools/blindcheat2.py`):** random moments from dates the model can't recall, each decided twice (with a
  learned playbook and without), graded by code with the live manager, spread, commission and swap. Over 71 trades
  that came to -0.11R a trade. The side it chose did no better than the opposite trade at the same moment (16 vs 18
  target-first). The playbook made no difference on the held-out exam.

So: no edge was found. What may be useful is the plumbing and the lessons below.

## What's here

| path | what |
|---|---|
| `ftmo_blank/driver.py` | bootstrap, training replay (`run`), live loop (`live-run`), status |
| `ftmo_blank/guard/` | hard limits, goal verifiers, order checks, worker kinds (model, effort, tools, budgets). Hash-checked: a turn refuses to run if they changed |
| `ftmo_blank/agent_code/` | routing (which event wakes which worker, with what prompt) and the knowledge store |
| `ftmo_blank/tools/` | paper broker, live broker (MT5 through a bridge), alerts, position manager, board, vault, dreams, the dashboard (`monitor_ui.py`, :8766), evaluators (`scout_eval.py`, `blindcheat2.py`) |
| `ftmo_blank/manual/OPERATOR_MANUAL.md` | what every worker is told about tools, fills, rules, time and kinds |
| `ftmo_blank/knowledge/` | instrument specs, sessions, measured spreads, and the shared desk vocabulary |
| `ftmo_blank/tests/`, `agent_code/tests/` | 471 tests, no model calls |
| `selfcall/` | the engine: a demand queue over `claude -p` with depth, children, budget, timeout and usage-limit handling |

## Running it

Requirements: Python 3.10+, `numpy`, and the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) logged
in to a subscription. The engine refuses to run with `ANTHROPIC_API_KEY` set, so it never bills the API by surprise.
Set `SELFCALL_ALLOW_API_BILLING=1` if you mean to.

```
cd ftmo_blank
python3 -m pytest -q tests agent_code/tests     # no model calls
python3 driver.py guard-write                   # once: hash the guard files
python3 driver.py status
python3 driver.py run --turns 1                 # one replay turn: real Claude calls, uses your subscription
```

- **Market data** is not included. The replay reads `ftmo_blank/feed/<SYMBOL>_M5.csv` with the header
  `t_broker,o,h,l,c,session`: unix seconds on the broker clock, then OHLC and a session tag.
- **Live** needs an MT5 terminal reachable through `pymt5linux` (the bridge is not part of this repo). Real orders are
  sent only if `ftmo_blank/state/LIVE_ARMED` exists. The dashboard's Pause and Kill, or `touch ftmo_blank/state/STOP`,
  stop both loops.
- **Two Claude accounts (optional):** the failover reads a small account registry (`~/.castl/accounts.json`, written
  by the author's account switcher, which is not included). Without it, everything runs on the logged-in account. With two accounts registered, a usage limit or a dead account moves the call to the other one. A dead
  account is billing lapsed, subscription off, or a revoked login. It is re-tried every 6 hours.

Some tools read files from the author's machine (an older MT5 stack, research data) and simply find nothing elsewhere.
Paths are under `~`.

## Lessons worth more than the code

1. **Count the orders before and after any change to what a worker reads.** Test on the same market moments. A prompt
   layer that only "adds context" can silently stop a desk from trading.
2. **Never grade a skip with an invented bracket.** A blind stop/target says "skipping was right" about almost any
   entry. If you want a ruler for skips, use a paired arm: the same moment decided by a different worker, with its real
   levels.
3. **"My stop was hit, then price reached my target" means little on its own.** Compare it with the opposite trade at
   the same moment: here it happened just as often (20 of 35 vs 20 of 33). It was the 5-day window, not the stop.
4. **Don't train on dates the model may remember.** Mask names and dates, or use data after its knowledge cutoff.
5. **Recompute every number a model quotes from its own lessons.** Several "edges" in the lesson library didn't survive
   being recomputed from the rows.
6. **Long interactive sessions are the most expensive thing in the setup.** A 600k-token context costs a full reload
   every time it wakes after a pause.

## Disclaimer

Not financial advice. Trading on leverage can lose more than you put in. Not affiliated with, endorsed by, or connected
to FTMO or Anthropic. The rules modeled here are one reading of a prop firm's public terms at one point in time; check
the real terms yourself.
