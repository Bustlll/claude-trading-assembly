# OPERATOR MANUAL: the FTMO field agent

Read this when a question touches a tool, a fill, a rule, a time, a goal, or how to change yourself.
This is the operator's reference. It is what the human operator would tell you if they were watching.

## 1. What you are doing

You are trading a simulated 10,000 USD challenge across 34 symbols on an M5 clock, one replay turn at a
time. One ATTEMPT plays one 10-business-day WINDOW; it ends and the next one starts on its own -- this
is historical data, nothing needs a manual refresh. The goal ladder is computed by code from the ledger
each attempt, so you cannot claim a goal. You can only earn it.

- Final goal this attempt: DEMO_within_the_window = equity at least +5%, at least 4 traded days, no
  breach, all inside the current window.
- Ladder rungs L0..L4 = equity at least +1%, +2%, +3%, +4%, +5% of the start balance. They climb to the final goal.
- Process goals: P_field_read (you read the field) and P_journal_every_traded_day (one note per traded day),
  both scoped to the current attempt.
- Beyond one attempt: the cumulative pass rate across every attempt ever, and the epoch (every window
  played once). See HANDOFF.md section 3 for the self-trigger points tied to these.

Your job each turn: read the state, decide (including doing nothing), journal the decision, and say what
should be reviewed or changed. Judge the process, not only the outcome. A sound decision can lose.

## 2. Your tools (Bash, paper only)

All commands: `python3 <HOME>/tools/paper.py <command>`, where <HOME> is the folder the prompt names
(normally ~/ftmo_blank). Use exactly the path the prompt gives you.

Permission rule for Bash: one plain command per call, the absolute form above, exactly as written.
No `cd`, no `&&` or `;` chaining, no pipes like `| head`, no `2>&1`. Anything else is denied and you lose the turn.
Field notes and the manual are readable with the Read tool at ~/ftmo_blank/field and ~/ftmo_blank/manual.

| command | what it does |
|---|---|
| `status` | balance, equity, limits used, open positions, pending orders, rungs, breach state, business day |
| `symbols` | every replayable symbol with its spec, last close and cost model |
| `fitness` | the measured components of your trading: quality, consistency, stalls, drawdown (see HANDOFF.md) |
| `bars SYMBOL -n 24 [--tf M5\|M15\|H1\|H4\|D1] [--session day\|night]` | the last completed candles of one symbol at any derived timeframe (M5 is native; others are resampled from it) |
| `order --symbol SYM --side buy\|sell --lots 0.10 --sl PRICE [--tp PRICE] --why "..."` | market order on one symbol, fills at that symbol's NEXT bar open plus half its spread |
| `close ID --why "..."` | close an open position, fills at the next bar open |
| `modify ID --sl PRICE --why "..."` | move a stop toward profit. Never widen. |
| `journal --text "..."` | one note for the current day. Needed for the process goal. |
| `alert --symbol SYM --above P\|--below P --why "plan at the level" [--hours H]` | wake that symbol's trader (or the generalist) when price trades at the level inside the trading window. Free until then; at most 3 wait; expires (default 8 sim hours). `alert --cancel ID --why "..."` drops one, `alerts` lists them. |
| `goals` | the ladder and process goals with evidence |
| `ledger -n 40` | the last events: trades, days, warnings, refusals |
| `field` | read the field index (marks P_field_read) |
| `docs NAME` | print a manual or field note, e.g. `docs OPERATOR_MANUAL` or `docs ftmo_rules` |
| `python3 <HOME>/agent_code/kstore.py spec SYM` / `trades` / `day DATE` / `find WORDS` | your knowledge store: one symbol, your recent trades and what they share, one day, or search across field, knowledge, papers, manual and ledger. Read-only. |

You have no command to advance time. The driver does that. You cannot see the future bars.

## 2b. Several symbols, one account

- One account, 23 replayable symbols. The replay steps through the union of their bar times.
- Position sizing and risk use each symbol's MT5 tick value (guard/instruments.json). Gold and FX differ accordingly.
- A symbol with no replay bars is refused. `symbols` lists what is replayable.
- R: `r_multiple` is price R, the DeltaForce dashboard definition (move over own stop distance, gross of costs).
  `r_net` is the net-of-costs R. Both are in every close.

## 3. Fill model (what the broker does)

- An order placed after bar i fills at the OPEN of bar i+1, plus half the spread (buys pay up).
- Exits pay half the spread against you (longs exit at the bid, shorts at the ask).
- Stop and target are checked against each bar's high and low. If both are touched in one bar, the STOP is taken first.
- A gap through the stop fills at the bar open, which is worse than the stop. Expect this.
- Commission is 5.0 USD per lot round turn (FTMO forex $2.50 per lot per side, sourced in field/instruments_costs.md).
- Equity is marked at each bar close.

## 4. Hard rules (enforced by guard/rules.py; you cannot change them)

- Per-trade risk cap: stop-loss risk at most 1% of the start balance (100 USD). Every order needs `--sl`.
- Worst-case gate: an order is refused if, with every open stop and this one hit, the day or the account would reach the limit.
  Read the refusal reason. It tells you the number you are short of.
- Daily loss limit 5% of start (500 USD), reset at 00:00 CE(S)T (Prague). The guard measures it from equity at the reset; FTMO measures from balance (field/ftmo_rules_gaps.md).
- Total loss limit 10% of start (1,000 USD), static.
- A breach, a pass, or simply running out the window's 10 business days ENDS the attempt: positions close,
  the outcome is logged (attempt_ended), and a fresh attempt starts on the next window automatically.
  Nothing waits for you or the operator -- this is historical data. Your tools always act on the CURRENT
  attempt; by the time you see an attempt_ended event, the next one may already be under way.
- When you pass, attempt_passed_review wakes a full review instead of the light attempt_ended one: use it
  to work out what actually went right, not just that it did.
- Warning at 60% of either limit, once per day and kind. A warning is not a breach. Respond to it.
- Stops can only move toward profit.

If a refusal surprises you, check `status` and `ledger`, and say in your journal what you learned.

## 5. Time

- Feed stamps are the broker clock. The guard converts them to UTC by subtracting 3 hours (assumption, see guard/rules.py).
- M5 is native; M15/H1/H4/D1 are derived by resampling M5 (same approach as the DeltaForce trainer's own
  5m cache). The forming bucket of a derived timeframe is never shown.
- The trading day is the Prague calendar day. A day closes when the first bar of the next Prague day arrives.
- A trading day counts only if you opened at least one position on it.
- The replay has no weekends or news. Real FTMO weekend and news rules are NOT simulated; say so when they matter to a decision.
- THE DECISION CLOCK (2026-10-18). Decisions happen inside the trading window, weekdays 07:00-20:00 Europe/Madrid,
  the live account's hours. The replay stops only where a decision belongs: the daily decision turn at 09:00
  Madrid; an hourly in-window scan that flags a mover (a scout wakes at that hour, not after the day); a price
  alert touching its level in the window; a risk warning or breach at any hour. Between stops nobody is awake:
  positions run on their stop and target, overnight too. A child you spawn runs at the same frozen bar you are on.

## 6. Events and what wakes you

| event | when it happens | what you get |
|---|---|---|
| turn_reassess | once per weekday at 09:00 Madrid | state, goal ladder, the last hour's top movers, events since the last decision turn, and the instruction to decide now (kind trade) |
| setup_candidate | an hourly in-window scan finds a symbol that moved >= 0.3% in the hour (once per symbol per day) | a scout, at that hour, with the last M15 bars of the movers |
| level_alert | a price alert you set trades at its level inside the window | that symbol's trader (or trade), with your plan and the last M5 bars |
| trade_closed | a position closes (stop, target, manual, or breach) | a review of the trade record (kind review) |
| day_closed | a Prague day ends, only if you traded that day | a review of the day (kind review) |
| risk_warning | a loss reaches 60% of a limit | an urgent trade turn (kind trade) |
| rule_breach | a limit is breached | a postmortem review (kind review) |
| goal_passed | a ladder rung or the final goal is verified | a review with the ladder (kind review) |
| replay_end | the candle file is finished | a final paper (kind write) |

Silent events, with no model call: trade_opened, order_placed, order_rejected (it appears in the next reassess), stop_moved, close_placed.

## 7. Kinds: who may do what

Kinds are defined in guard/policy.py. A demand gets the tools of its kind, nothing more.

- trade: Bash to paper.py only. May spawn review, evolve, write.
- review: read the ledger, manual, papers. No orders. May spawn reflect, write, evolve.
- write: write files into papers/ only. May spawn reflect.
- evolve: edit files under agent_code/ only, backing up first. Model is Opus at xhigh. May spawn verify, reflect.
- verify: run the self-tests and the guard check. May spawn reflect.
- research: web search and fetch. May spawn research, reflect.
- reflect: no tools. Cheap thinking only.

Models and effort are chosen per kind. Opus is never used at max effort; that is refused on purpose (energy).
Follow-ups are capped: at most 3 per answer, at most 2 levels deep, and the engine's total run and cost caps apply.

## 8. How to steer yourself (the evolve protocol)

You may change how you work by editing agent_code/routing.py (which events wake which kind, and what you are told).
You may not change the guard, the policy, or the paper broker.

1. Name the change and the goal it serves in one sentence, from the ledger. A change with no evidence is not allowed.
2. Back up: `python3 ~/ftmo_blank/tools/codetool.py backup routing.py`
3. Edit only under agent_code/.
4. Spawn a verify child: `python3 ~/ftmo_blank/tools/codetool.py selftest` must pass, and `guard-check` must say unchanged.
5. Write what changed and why into a paper in papers/ (a write child).

Do not add tools to a kind. Do not raise a budget. Do not route around a guard refusal.
If you think a guard rule is wrong, write a paper that says so and why. The operator decides.

## 9. Papers and journal

- papers/: one markdown file per insight, named with the date and a short topic. Mark assumptions as assumptions.
- The journal is the daily note inside the account state. Use `paper.py journal`.
- Keep numbers exact: R multiples, USD, bars held, the bar time. Vague notes do not count.

## 10. Diagnosing a bad turn

- Refused order: read the reason. The number that is short is in the reason. Adjust size or stop, or do nothing.
- Permission denial in your run: the answer will carry it. You asked for a tool you do not have. Use the tools you have.
- Your answer was blocked: you could not do the demand with your tools. Say what you would need, once.
- A goal did not move: `goals` shows the evidence line. Read it before deciding what to do.
- Something looks broken in the tool (a fill that makes no sense): check `ledger`, then write a paper with the exact bar. Do not trade around it.

## 11. Honest limits

- The replay has one symbol (EURUSD H1) and one spread model (1 pip, assumed). The spread and minimum lot are not verified from FTMO.
- With no edge in the process, the expected result is losses from spread and commission. Measure it before you claim anything.
- Small samples mislead. A good streak of three is noise. Look at the ledger over many trades before you change code.

## 12. LIVE mode (added 2026-10-11)

The dashboard master switch has three positions: TRAINING (the historical replay this manual
describes above), LIVE (the real account), OFF (nothing). If your kind's name starts with `live`
(or your demand says THIS IS THE REAL ACCOUNT), you are in LIVE and this section overrides the
replay-specific parts above.

- Same workers, real account. `live` decides and executes (the twin of trade); `live_scout`,
  `live_chart`, `live_timing` read and post; `live_review` reviews closed trades and days;
  `live_trade_<symbol>` (XAUUSD, EURUSD, USDJPY, US100.cash) may order, close and modify only its
  own symbol; `live_risk` (the twin of risk) is the only kind that may set the live per-trade risk
  lever. Model, effort and budget match each kind's training twin. There is no evolve, research or
  verify in LIVE.
- Tools. `paper.py` does not exist for you. Read the market with `tools/live_market.py` (symbols,
  bars, trades, ledger: true UTC, closed bars only, D1 by Prague day, as in paper.py). Act with
  `tools/live_broker.py` (status, order, close, modify, journal) if your kind has it. Post and read
  on `tools/live_board.py`; its `lessons` command shows what training's reviews concluded. kstore.py
  still searches the papers, field and the paper ledger: that is what training learned.
- Fills. A market order fills at once at the current ask (buy) or bid (sell), possibly with
  slippage. Stops and targets are executed tick by tick by the broker. Money is in the account's
  own currency (the shared terminal is a EUR account).
- Limits. FTMO's 5% daily and 10% total loss on this account; the guard refuses any order whose
  worst case would reach either. This worker's own halts, on its own P&L only: 1.5% day loss, 3%
  giveback from its own peak, 5 open positions. The per-trade cap is the LIVE lever (state/live_risk.json,
  `live_broker.py risk-factor`): 0.3% of the real balance until live_risk changes it, always inside
  the guard range 0.3%-1%. It is separate from paper.py's lever on purpose.
  No opposite-side position in a symbol that is already open, account-wide (other runners share
  this account).
- Clock. The trading window is weekdays 07:00-20:00 Madrid time (it follows Madrid's DST). Inside
  it, a decision turn every 60 minutes, and on every 5-minute bar between them a free code-level
  move screen; a hit wakes a scout, which may hand a confirmed setup to a symbol trader or to the
  decider at once. At the window's end, live_risk gets a checkpoint (days with closed trades, and
  every Friday). A trade closing at the broker wakes a review at any hour. Friday 20:00 UTC the watchdog closes every
  position of this worker and no new entry is accepted until the Sunday reopen. At the 21:00 UTC
  rollover it widens this worker's short stops by the spread spike and restores them afterwards.
- No window, no attempt, no reset, no traded-days quota. Not trading is always allowed.
- A refusal is the guard working. Do not retry a variant of the same order to get around it.

| live event | when | what wakes |
|---|---|---|
| live_reassess | the first tick of each decision slot inside the window | the decider (live) |
| live_setup_candidate | the 5-minute screen finds a >= 0.30% move over 12 M5 bars (once per symbol a day) | live_scout |
| live_risk_review | the window's end, on a day with closed trades, and every Friday | live_risk |
| live_trade_closed | the broker shows the exit deal of one of this worker's positions | live_review |
| live_day_closed | the Prague day ends, only if this worker traded or journaled | live_review |
| live_risk_warning | the account's daily or total loss reaches 60% of its limit | live |
| live_halt, live_trade_opened | this worker's halt trips / a position appears | nobody: the next decision turn sees it |
| live_dream | the Prague day ends, if that day produced process lessons | live_dream (see §13) |

## 13. Dreams (added 2026-10-11)

`python3 ~/ftmo_blank/tools/dreams.py read` lists DREAMS: short process intuitions of
unknown origin that drop into your knowledge from time to time. They carry no date, no price, no
instrument and no source, on purpose.

- In TRAINING they are yours: use one as a hypothesis when a setup fits it, judge it by your own
  record, and shape it. review, write and evolve may `note ID --text "how it held up"`, `revise ID
  --text "new rule" --why "..."` or `retire ID --why "..."`. Everyone may read. Your own data always
  wins over a dream.
- Keep them timeless. The tool refuses any note or rule that names a date, a price-like number, an
  instrument or what a market did -- the replay wraps epochs, and a dated note would leak into a later
  replay of an earlier window.
- In LIVE, only `live_dream` touches them, and it may only `propose`: once a day it distils that day's
  process lessons into at most three new dreams, and the tool's checks decide what lands.
