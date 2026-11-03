"""GUARD: what each kind of demand may do. Read-only for the agent.

A kind fixes the model, the effort, the tools, the permission rules, the budget, and
which kinds it may spawn. Routing (agent_code/routing.py) can choose a kind for an
event, but it cannot add tools or raise a budget. Opus at max effort is refused by the
engine (energy cost), so no entry here can use it.
"""

import sys as _sys
from pathlib import Path

HOME = str(Path(__file__).resolve().parents[1])   # the folder this guard belongs to (isolation: copies stay separate)
_sys.path.insert(0, f"{HOME}/tools")
import desk_shared as _DS  # noqa: E402  (operator-owned: the checklist and trader guide both desks' traders carry)
PAPER = f"{HOME}/tools/paper.py"
CODETOOL = f"{HOME}/tools/codetool.py"
KSTORE = f"{HOME}/agent_code/kstore.py"   # the agent's own read-only knowledge store (read-only commands)
BOARD = f"{HOME}/tools/board.py"         # the shared, append-only log the specialist kinds post findings to
LIVE_BROKER = f"{HOME}/tools/live_broker.py"  # the real order path; see manual/LIVE_TRADING_IDEA.md
LIVE_MARKET = f"{HOME}/tools/live_market.py"  # real market + live record READS; has no order verb
LIVE_BOARD = f"{HOME}/tools/live_board.py"    # the live assembly's own board (real clock), + training lessons
DREAMS = f"{HOME}/tools/dreams.py"            # live -> training: timeless process intuitions of unknown origin
VOCAB = f"{HOME}/tools/vocab.py"              # the desk vocabulary (knowledge/vocab.json): read-only, every kind
CONSULT = f"{HOME}/tools/consult.py"          # ask the chart/timing specialist NOW, answer inside the turn (2026-10-19)
VAULT = f"{HOME}/tools/vault.py"              # every closed trade on every account, read at entry and after (2026-10-20)
# Claude Code reads a single leading slash in a permission rule as relative to the settings root.
# Absolute paths need the double slash: Read(//home/...). Verified by probe.
ABS = "/" + HOME

# Appended to the system prompt of EVERY call of a kind that carries it (contract() in
# selfcall/engine.py does this via pol["notes"], for root calls AND for children a parent
# spawns). routing.py's CONTEXT carries the same reminder, but only reaches root demands --
# a child a review/write/evolve run spawns itself (via next_demands) never sees routing.py's
# text, only whatever prompt the parent model wrote. Without this, a child routinely tries a
# chained Bash command ("cmd1 && cmd2", "cmd1; cmd2", a pipe, or "2>&1") and is denied outright,
# because a permission rule like Bash(python3 X *) matches exactly one plain command, never a
# chain -- even when every piece of the chain alone would have been allowed. Confirmed live
# 2026-10-07/08: a child review demand asking for `kstore.py trades | tail; kstore.py day | tail`
# was denied as a whole and reported "Bash was denied" with no data read at all.
# The exact CLI, spelled out, so no kind ever needs --help, grep or a Read of the script to learn
# the syntax -- confirmed live 2026-10-08: scout repeatedly tried `paper.py --help`/`board.py --help`/
# `grep add_argument board.py`/a Read of board.py, and every one was denied (none of those verbs are
# in any kind's allow list), burning a whole call for nothing it could not have just been told here.
PAPER_SYNTAX = ("paper.py's subcommands, exactly (only the ones your own allow list grants will actually "
                "run): status | field | symbols | bars SYMBOL [-n N] [--tf M5|M15|H1|H4|D1] [--session day|night] "
                "| order --symbol S --side buy|sell --lots L --sl P [--tp P] --why \"...\" | "
                "close --symbol S ID [--volume V] --why \"...\" | modify --symbol S ID [--sl P] [--tp P] --why \"...\" | "
                "widen --symbol S ID --sl P --why \"...\" (the risk kind's alone) | journal --text \"...\" | risk-factor [--set PCT --why \"...\"] | goals | fitness | "
                "ledger [-n N] | attempts [-n N] | real-eval | alert --symbol S --above P|--below P --sl P --tp P --why \"plan\" "
                "[--when touch|close|hold|reject] [--tf M5|M15|H1] [--bars N] [--hours H] [--from WORKER] "
                "| alert --cancel ID --why \"...\" | alerts. An alert wakes the trader ONCE, when its condition is met "
                "on a closed bar (close = closes beyond the level, hold = N closes beyond, reject = trades through and "
                "closes back); never set one next to price to be woken again soon -- that is refused. --sl/--tp are the plan's MOCK stop and "
                "target (required; opposite sides of the level): they set the plan's side and grade it if it is passed "
                "-- the trader that wakes sets its own at entry. close and modify both require --symbol, matched "
                "against that position's own symbol. risk-factor with no --set just reads the current value. "
                "real-eval shows chances earned at the real two-step evaluation this demo is practice for "
                "(field/real_progression.md) -- read-only, recomputed fresh every time, nothing to set.")
BOARD_NOTE_SYNTAX = ("board.py's subcommands, exactly: post --from WORKER --topic T --text \"...\" [--symbol S] "
                      "| read [--worker W] [--topic T] [--symbol S] [-n N] | lessons [--symbol S] [--codes S1,A1] "
                      "[--phase entry|stop|target|manage|size|exit] [--session asia|london|us_open|us|rollover|friday|event] "
                      "[-n N] | lessons --show ID | lessons --map. `lessons` walks what the reviews and papers concluded "
                      "by situation and prints the ones on that path in full (the same verb as live_board.py lessons).")
LIVE_BROKER_NOTE = ("live_broker.py's subcommands, exactly (only the ones your own allow list grants will "
                    "actually run): status | order --symbol S --side buy|sell (--lots L | --risk-pct P) --sl P [--tp P] "
                    "--why \"...\" | order --symbol S --side buy|sell --sl P [--lots L | --risk-pct P] --dry-run "
                    "| close --symbol S ID [--volume V] --why \"...\" (--volume: a partial close) | modify --symbol S ID "
                    "[--sl P] [--tp P] --why \"...\" (stop toward profit only; target either way) | widen --symbol S ID --sl P "
                    "--why \"...\" (a wider stop: the live_risk kind's alone, once per trade) | journal --text \"...\" | risk-factor [--set PCT] [--day-halt F] [--giveback F] [--max-positions N] --why \"...\" (reading it is in "
                    "status; setting any of them -- per-trade risk and the assembly's own halts and position cap, bounded "
                    "only by FTMO's 5% daily / 10% total -- "
                    "is the live_risk kind's alone). ID is the position ticket status shows. status reports mode, armed, "
                    "connected, the real account, your own open positions, and exactly which gate would refuse "
                    "right now -- a refusal is the guard working, never a bug to work around. "
                    "SIZE FROM THE BROKER, never by mental math (one live trade was sized 10x too small that way): "
                    "order ... --dry-run places nothing and shows the broker's stop risk per lot, the contract size, "
                    "your per-trade cap, the most lots that fit it and the runners' valuation of the same symbol; "
                    "--risk-pct P (PERCENT of balance: 0.3 means 0.3%) sizes the order from that instead of --lots. "
                    "Every order reply states its real risk and how much of the cap it uses.")
LIVE_MARKET_NOTE = ("live_market.py's subcommands, exactly: symbols [--find TEXT] [--enable SYMBOL] | bars SYMBOL "
                    "[-n N] [--tf M5|M15|H1|H4|D1] [--session day|night] | trades [-n N] [--symbol S] | ledger [-n N]. "
                    "Real broker data, true UTC, closed bars only (the forming bar is never shown), D1 by Prague day "
                    "-- the same conventions as paper.py bars. `symbols` lists what you can TRADE, with its risk table (contract "
                    "size; what a 1.0 price move costs per lot, from the broker and from the other runners' own table); "
                    "`bars` also reads "
                    "any other symbol the broker offers (stocks, commodities, indices) for context. A symbol that is "
                    "not active in the terminal is activated for you; `symbols --find TEXT` finds the broker's exact "
                    "name (e.g. --find NVDA) and `symbols --enable SYMBOL` activates one and shows its quote. Orders "
                    "stay limited to the symbols `symbols` lists. PRICE ALERTS (places nothing at the broker; the "
                    "kinds that may set one): alert --symbol S --above P|--below P --sl P --tp P --why \"plan\" [--when "
                    "touch|close|hold|reject] [--tf M5|M15|H1] [--bars N] [--hours H] [--from WORKER] | alert --cancel "
                    "ID --why \"...\" | alerts. An alert wakes the trader ONCE, when its condition is met on a closed "
                    "bar (close = closes beyond the level, hold = N closes beyond, reject = trades through and closes "
                    "back); never set one next to price to be woken again soon -- that is refused. --sl/--tp are the plan's MOCK "
                    "stop and target (required; opposite sides of the level): they set the plan's side and grade it if "
                    "it is passed -- the trader that wakes sets its own at entry (the same rules as training's paper.py "
                    "alert, tools/alerts_core.py).")
LIVE_BOARD_NOTE = ("Live board (append-only, real clock, separate from the training board): "
                   f"{LIVE_BOARD} post --from WORKER --topic T --text \"...\" [--symbol S] | read [--worker W] "
                   "[--topic T] [--symbol S] [-n N] | lessons [--symbol S] [--codes S1,A1] [--phase entry|stop|target|"
                   "manage|size|exit] [--session asia|london|us_open|us|rollover|friday|event] [-n N] | lessons --show ID "
                   "| lessons --map. `lessons` walks what training's reviews, the papers and the live reviews concluded, "
                   "by situation, and prints the ones on that path in full -- process lessons, never today's prices "
                   "(the same verb as board.py lessons on the training desk). WORKER is one of "
                   "scout, chart, timing, review, trade. Post what you found so the next call does not redo it.")
LIVE_BASH_NOTE = ("Every Bash call is ONE plain command, the exact absolute path, no cd, no && or ;, no pipes, "
                  "no 2>&1 -- a chained command is denied as a whole. state/ is not yours to Read: go through "
                  "the tools. Never call --help, grep a tool's source, or Read it to learn its syntax -- the "
                  "syntax is given here. Always literally \"python3 /home/.../tool.py ...\". This is the LIVE "
                  "account: paper.py does not exist for you, and every order moves real money. "
                  + LIVE_MARKET_NOTE + " " + LIVE_BROKER_NOTE)

BASH_NOTE = ("Every Bash call is ONE plain command, the exact absolute path, no cd, no && or ;, "
             "no pipes, no 2>&1 -- a chained command is denied as a whole, even if each piece alone "
             "would be allowed. Run paper.py/kstore.py/codetool.py calls one at a time. state/ (the "
             "raw ledger, account.json) is not yours to Read directly: always go through paper.py or "
             "kstore.py, which filter out anything dated after the current simulated time. Never call "
             "--help, grep a tool's own source, or Read it to learn its syntax -- none of that is in "
             "any allow list and it is always denied; the syntax is given to you instead: " + PAPER_SYNTAX + " "
             "Always literally \"python3 /home/.../tool.py ...\" -- not /usr/bin/python3, not the script run "
             "directly without python3 in front: only that exact literal prefix matches your permission rule.")

# For write/evolve, which also carry Write/Edit/Glob directly: the most common denial by far
# (confirmed live, 2026-10-08) is reaching for Bash to do what one of those tools already does --
# `cat > file <<EOF`, `python3 -c "open(...).write(...)"` to edit a file, or `ls`/`grep` to look
# around -- instead of just calling Write, Edit or Glob. Bash here is for kstore.py/codetool.py only.
FILE_TOOL_NOTE = ("To create or edit a file, use the Write or Edit tool directly -- never Bash "
                   "(no cat heredocs, no `python3 -c \"...write(...)\"`, no cp/mv/sed on papers or "
                   "code). To look around a directory, use Glob or Grep, not Bash ls/grep/find. "
                   "Bash is for kstore.py (write) or codetool.py (evolve) only.")

# board.py has no edit or delete verb at all -- that absence, plus no kind below getting raw
# Write/Edit on state/board.jsonl, is the entire append-only guarantee. Nothing can erase a post,
# not even the worker that wrote it.
BOARD_NOTE = (f"Shared board (append-only; {BOARD} has no verb to edit or delete a post, so nothing "
              "you or anyone else posts can ever be taken back): post what you found so the next "
              "call does not redo your work, and read it before you start so you do not redo theirs. "
              + BOARD_NOTE_SYNTAX)

KINDS = {
    # knowledge gathering: the only kind with web access
    "research": {"tools": ["WebSearch", "WebFetch"], "model": "sonnet", "effort": "high",
                 "budget_usd": 0.80, "may_spawn": ["research"]},

    # pure thinking, no tools: cheapest reflection
    "reflect": {"tools": [], "model": "haiku", "effort": "medium",
                "budget_usd": 0.20, "may_spawn": []},

    # read the ledger, the manual and the papers; no writes, no orders
    "review": {"tools": ["Read", "Glob", "Grep", "Bash"], "model": "sonnet", "effort": "high",
               "allow": [f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)", f"Read({ABS}/manual/**)", f"Read({ABS}/agent_code/**)", f"Read({ABS}/papers/**)", "Glob", "Grep",
                         # read-only paper commands, so a review can measure its own results (no orders, no journal)
                         f"Bash(python3 {PAPER} status*)", f"Bash(python3 {PAPER} goals*)",
                         f"Bash(python3 {PAPER} fitness*)", f"Bash(python3 {PAPER} ledger*)",
                         f"Bash(python3 {PAPER} bars*)", f"Bash(python3 {PAPER} symbols*)",
                         f"Bash(python3 {PAPER} attempts*)", f"Bash(python3 {PAPER} real-eval*)",
                         f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)", f"Bash(python3 {BOARD} post*)", f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)"],
               "budget_usd": 0.50, "may_spawn": ["write", "evolve", "scout", "chart", "timing", "risk"],
               "notes": BASH_NOTE + " " + BOARD_NOTE + " You cannot write journal notes (that is the trader's own "
                        "record): if one is missing, say so in findings."},

    # the only kind that can place or close paper orders. Explicitly enumerated, not a Bash(...*)
    # wildcard on paper.py -- that used to be a blanket "any paper.py subcommand" rule, which meant
    # any NEW subcommand (like risk-factor, below) would have silently inherited into trade's access
    # the moment it was added to paper.py, defeating the point of giving "risk" kind sole ownership
    # of that lever. Every subcommand trade actually needs is named; risk-factor is not among them.
    "trade": {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "high",
              "allow": [f"Bash(python3 {PAPER} status*)", f"Bash(python3 {PAPER} field*)",
                        f"Bash(python3 {PAPER} symbols*)", f"Bash(python3 {PAPER} bars*)",
                        f"Bash(python3 {PAPER} order*)", f"Bash(python3 {PAPER} close*)",
                        f"Bash(python3 {PAPER} modify*)", f"Bash(python3 {PAPER} journal*)",
                        f"Bash(python3 {PAPER} alert*)",
                        f"Bash(python3 {PAPER} goals*)", f"Bash(python3 {PAPER} fitness*)",
                        f"Bash(python3 {PAPER} ledger*)", f"Bash(python3 {PAPER} attempts*)", f"Bash(python3 {PAPER} real-eval*)",
                        f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)",
                        f"Bash(python3 {BOARD} post*)", f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)",
                        f"Read({ABS}/field/**)", f"Read({ABS}/manual/**)", f"Read({ABS}/knowledge/**)"],
              "budget_usd": 0.60,
              "may_spawn": ["review", "evolve", "write", "scout", "chart", "timing"],
              "notes": BASH_NOTE + " " + BOARD_NOTE},

    # manages the account's risk APPETITE, not any one trade: reads performance/consistency data
    # across the whole history (kstore, paper.py fitness/attempts/ledger) and the guard's own rules,
    # then tunes the shared per-trade risk lever within the guard's fixed range. Cannot order, close,
    # modify or journal -- it recommends and sets a number, nothing else. See guard/rules.py
    # effective_risk_pct/RISK_FACTOR_MIN/MAX_RISK_PER_TRADE for the hard bounds it cannot escape.
    "risk": {"tools": ["Bash", "Read", "Glob", "Grep"], "model": "sonnet", "effort": "high",
             "allow": [f"Bash(python3 {PAPER} status*)", f"Bash(python3 {PAPER} fitness*)",
                       f"Bash(python3 {PAPER} attempts*)", f"Bash(python3 {PAPER} real-eval*)", f"Bash(python3 {PAPER} ledger*)",
                       f"Bash(python3 {PAPER} goals*)", f"Bash(python3 {PAPER} symbols*)",
                       f"Bash(python3 {PAPER} risk-factor*)", f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)",
                       f"Bash(python3 {BOARD} post*)", f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)",
                       f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)", f"Read({ABS}/manual/**)",
                       f"Read({ABS}/papers/**)", "Glob", "Grep"],
             "budget_usd": 0.70, "may_spawn": ["review"],
             "notes": BASH_NOTE + " " + BOARD_NOTE},

    # breadth: screens symbols for a candidate worth a closer look. Narrower and cheaper than trade/review
    # on purpose -- it is meant to be called often (free up the decider's own turn), not to decide anything.
    "scout": {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "medium",
              "allow": [f"Bash(python3 {PAPER} symbols*)", f"Bash(python3 {PAPER} bars*)",
                        f"Bash(python3 {PAPER} status*)", f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)",
                        f"Bash(python3 {PAPER} alert*)",
                        f"Bash(python3 {BOARD} post*)", f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)",
                        f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)"],
              "budget_usd": 0.30, "may_spawn": [], "notes": BASH_NOTE + " " + BOARD_NOTE},

    # depth: a multi-timeframe technical read of the one to three symbols scout or trade flagged.
    "chart": {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "medium",
              "allow": [f"Bash(python3 {PAPER} bars*)", f"Bash(python3 {PAPER} status*)",
                        f"Bash(python3 {PAPER} symbols*)", f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)",
                        f"Bash(python3 {BOARD} post*)", f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)",
                        f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)"],
              "budget_usd": 0.30, "may_spawn": [], "notes": BASH_NOTE + " " + BOARD_NOTE},

    # session, day-of-week, pacing within the window, and the FTMO clock -- not price itself.
    "timing": {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "medium",
               "allow": [f"Bash(python3 {PAPER} status*)", f"Bash(python3 {PAPER} goals*)",
                         f"Bash(python3 {PAPER} symbols*)", f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)",
                         f"Bash(python3 {PAPER} bars*)",
                         f"Bash(python3 {BOARD} post*)", f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)",
                         f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)"],
               "budget_usd": 0.20, "may_spawn": [], "notes": BASH_NOTE + " " + BOARD_NOTE},

    # write papers into papers/ only
    "write": {"tools": ["Read", "Glob", "Grep", "Write", "Bash"], "model": "sonnet", "effort": "medium",
              "allow": [f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)", f"Read({ABS}/manual/**)", f"Read({ABS}/agent_code/**)", f"Read({ABS}/papers/**)", "Glob", "Grep", f"Edit({ABS}/papers/**)", f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)"],  # Edit rules govern Write too (probed)
              "budget_usd": 0.50, "may_spawn": [], "notes": BASH_NOTE + " " + FILE_TOOL_NOTE},

    # change its own code under agent_code/ (Opus allowed, below max effort)
    "evolve": {"tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"], "model": "opus",
               "effort": "xhigh",
               "allow": [f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)", f"Read({ABS}/manual/**)", f"Read({ABS}/agent_code/**)", f"Read({ABS}/papers/**)", "Glob", "Grep",
                         f"Edit({ABS}/agent_code/**)",
                         f"Bash(python3 {CODETOOL} *)", f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)"],
               "budget_usd": 1.00, "may_spawn": ["verify"], "notes": BASH_NOTE + " " + FILE_TOOL_NOTE},

    # CODE GATE repair (2026-10-18, operator idea): driver.py code_gate() runs the self-test in code after every
    # evolve; if it broke something, ONE repair runs with the failures and the diff, to fix the code or the
    # outdated test WITHOUT losing the upgrade. If the self-test still fails after it, the program restores the
    # pre-evolve backups itself -- so broken code can never pile up behind training. Spawned by the program only.
    "repair": {"tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"], "model": "sonnet", "effort": "high",
               "allow": [f"Read({ABS}/agent_code/**)", f"Read({ABS}/tests/**)", f"Read({ABS}/tools/**)",
                         f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)", f"Read({ABS}/manual/**)", "Glob", "Grep",
                         f"Edit({ABS}/agent_code/**)", f"Bash(python3 {CODETOOL} *)", f"Bash(python3 {KSTORE} *)"],
               "budget_usd": 0.60, "may_spawn": [], "notes": BASH_NOTE + " " + FILE_TOOL_NOTE.replace(
                   "Bash is for kstore.py (write) or codetool.py (evolve) only.",
                   "Bash is for codetool.py (backup, selftest) and kstore.py only.")},

    # run the self-tests and the guard check, report only
    "verify": {"tools": ["Bash"], "model": "haiku", "effort": "high",
               "allow": [f"Bash(python3 {CODETOOL} selftest*)", f"Bash(python3 {CODETOOL} guard-check*)"],
               "budget_usd": 0.20, "may_spawn": [], "notes": BASH_NOTE},
}

# PER-SYMBOL TRADERS: a guard-enforced kind per symbol, each able to open/close/modify ONLY its own
# symbol -- not a convention in a prompt, but a Bash allow-rule that names the exact symbol, backed by
# paper.py's own close/modify now REQUIRING --symbol and refusing a mismatch (see tools/paper.py).
# Reading (status/bars/symbols/goals/fitness) is shared and unscoped: looking at another symbol's
# price is not a safety question, only moving money in it is.
# Curated, not all 34: one entry per symbol actually traded so far. Add a symbol here when it starts
# seeing real activity; there is no cost in leaving the rest to the generalist "trade" kind.
SYMBOL_TRADERS = ("XAUUSD", "EURUSD", "USDJPY", "US100.cash")


def _symbol_trader(sym: str) -> dict:
    return {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "high",
            "allow": [f"Bash(python3 {PAPER} order --symbol {sym} *)",
                      f"Bash(python3 {PAPER} close --symbol {sym} *)",
                      f"Bash(python3 {PAPER} modify --symbol {sym} *)",
                      f"Bash(python3 {PAPER} status*)", f"Bash(python3 {PAPER} bars*)",
                      f"Bash(python3 {PAPER} symbols*)", f"Bash(python3 {PAPER} goals*)",
                      f"Bash(python3 {PAPER} fitness*)", f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)",
                      f"Bash(python3 {PAPER} journal*)", f"Bash(python3 {PAPER} alert*)",
                      f"Bash(python3 {BOARD} post*)", f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)",
                      f"Read({ABS}/field/**)", f"Read({ABS}/manual/**)", f"Read({ABS}/knowledge/**)"],
            # no "review": every one a trader spawned (2026-10-17: 5 of 5) found its order not filled yet --
            # the clock is frozen during the call. trade_closed routes the real review once it closes.
            "budget_usd": 0.50, "may_spawn": ["scout", "chart", "timing"],
            "notes": (BASH_NOTE + " " + BOARD_NOTE + f" You may only place, close or modify {sym} -- "
                      "paper.py itself refuses a --symbol that does not match, so this is enforced, not "
                      "a reminder. Your permission rule also only matches --symbol placed right after the "
                      f"subcommand: write 'order --symbol {sym} --side ... --lots ... --sl ... --why \"...\"', "
                      f"'close --symbol {sym} ID --why \"...\"', 'modify --symbol {sym} ID --sl ... --why \"...\"' "
                      "in exactly that order or the call is denied. Before ordering you may post a board "
                      f"finding with --topic pre_trade --symbol {sym} (then paper.py tags the trade "
                      "consulted_analysis=true automatically by checking the board -- you do not report this "
                      "yourself), or place directly without one -- both are allowed. Either way, review sees "
                      "afterwards whether this trade had prior analysis behind it, so be honest about your "
                      "own reasoning either way. " + _DS.trader_guide(sym, f"python3 {BOARD}", f"python3 {PAPER} journal")
                      + "If the entry is a retest or a break still to come, set a price "
                      "alert at it (paper.py alert ...) instead of chasing now or asking anyone to check later.\n"
                      + _DS.entry_rules("training") + "\n")}


for sym in SYMBOL_TRADERS:
    KINDS[f"trade_{sym.lower().replace('.', '_')}"] = _symbol_trader(sym)

# ---- 2026-10-19 (operator, todos/006 sections 8, 12): THE POSITION MANAGER, THE RISK WORKER'S WIDEN, CONSULT ----
# manage: woken by code (tools/manage_core.py) on one open position -- R milestones, the plan's invalidation closing,
# structure breaking against it, a stall, the rollover, the Friday flatten, and outside the window ONLY a volatility
# burst. It may close (all or part) and modify (stop toward profit, target either way); it cannot open anything and
# cannot widen -- it asks the risk kind, which alone may widen, once per trade (paper.py / live_broker.py widen).
# consult: the deciders (trade, the symbol traders, manage, risk) may ask chart/timing NOW and get the answer inside
# their own turn (tools/consult.py); the desk word in the command is pinned here, so no kind can reach the other desk.
_TRAIN_CONSULT = f"Bash(python3 {CONSULT} training *)"
TRAIN_CONSULT_NOTE = _DS.consult_note(f"python3 {CONSULT} training")
KINDS["manage"] = {
    "tools": ["Bash", "Read"], "model": "sonnet", "effort": "high",
    "allow": [f"Bash(python3 {PAPER} status*)", f"Bash(python3 {PAPER} bars*)", f"Bash(python3 {PAPER} symbols*)",
              f"Bash(python3 {PAPER} close*)", f"Bash(python3 {PAPER} modify*)", f"Bash(python3 {PAPER} journal*)",
              f"Bash(python3 {PAPER} watch*)",   # 2026-10-20: its standing watch (re-wake at a level/time, or quiet)
              f"Bash(python3 {PAPER} ledger*)", f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)",
              f"Bash(python3 {BOARD} post*)", f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)",
              _TRAIN_CONSULT, f"Read({ABS}/field/**)", f"Read({ABS}/manual/**)", f"Read({ABS}/knowledge/**)"],
    "budget_usd": 0.40, "may_spawn": ["risk"],
    "notes": (BASH_NOTE + " " + BOARD_NOTE + " You manage OPEN positions only: there is no order verb for you, and a "
              "wider stop is the risk kind's decision (next_demands risk). " + TRAIN_CONSULT_NOTE)}
KINDS["risk"]["allow"] = KINDS["risk"]["allow"] + [f"Bash(python3 {PAPER} widen*)", f"Bash(python3 {PAPER} bars*)",
                                                     _TRAIN_CONSULT]
KINDS["risk"]["notes"] = KINDS["risk"]["notes"] + " " + _DS.WIDEN_RULES + " " + TRAIN_CONSULT_NOTE
for _k in ("trade", *[f"trade_{s.lower().replace('.', '_')}" for s in SYMBOL_TRADERS]):
    KINDS[_k]["allow"] = KINDS[_k]["allow"] + [_TRAIN_CONSULT]
    KINDS[_k]["notes"] = KINDS[_k]["notes"] + " " + TRAIN_CONSULT_NOTE
# the VAULT (2026-10-20, operator: "a worker in charge of value extracting ... all the closed trades like paperwork"):
# reads tools/vault.py (read-only: report, similar) and posts what it extracts; it trades nothing and spawns nothing.
# Asked NOW by a decider (consult.py ... vault) and once a day after the day closes (vault_review).
KINDS["vault"] = {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "medium",
                  "allow": [f"Bash(python3 {VAULT} report --desk training*)", f"Bash(python3 {VAULT} similar --desk training *)",
                            f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)", f"Bash(python3 {BOARD} post*)",
                            f"Bash(python3 {BOARD} read*)", f"Bash(python3 {BOARD} lessons*)"],
                  "budget_usd": 0.40, "may_spawn": [], "notes": BASH_NOTE + " " + BOARD_NOTE}

SYMBOL_TRADER_KINDS = [f"trade_{s.lower().replace('.', '_')}" for s in SYMBOL_TRADERS]
for k in ("trade", "review", "scout"):
    KINDS[k]["may_spawn"] = KINDS[k]["may_spawn"] + SYMBOL_TRADER_KINDS
# 2026-10-18 (operator: training must work like live): a scout may hand a confirmed setup in ANY symbol to the
# decider at once, as live_scout may hand it to "live" -- not only through an alert or the next decision turn
KINDS["scout"]["may_spawn"] = KINDS["scout"]["may_spawn"] + ["trade"]

# the engine caps for this home: one agent, one replay, bounded spend
# max_depth 2->3 and max_children 3->4: the symbol-trader roster adds one more legitimate spawn
# target (trade or scout handing a symbol to its own trader), one level deeper than scout/chart/
# timing already were. Still a deliberate, bounded cap, not unlimited -- raised exactly enough for
# trade -> scout -> trade_<symbol> -> review to fit, no further.
# max_runs / max_total_cost_usd: NO CAP since 2026-10-11 (operator): every call runs on the Claude
# subscription's usage, not API billing -- the reported $ is an API-price equivalent, the real cost is the
# flat subscription. The operator stops and resumes training by hand (dashboard Pause/Resume, state/STOP);
# the engine only ever pauses on its own for a usage limit. Before: 800 runs / $60 cumulative, which halted
# training for good at 637 runs / $49.70. Per-call budget_usd per kind still bounds one runaway call.
LIMITS = {"max_depth": 3, "max_children": 4, "max_runs": 10**9, "max_total_cost_usd": 10.0**9,
          "timeout_s": 900, "max_prompt_chars": 40_000}
# 40k (was 20k), 2026-10-18, both desks alike: the shared view (tools/desk_shared.py) puts full candles, the
# symbol's board thread and the lessons on its path IN the prompt instead of 2-6 tool round trips. Measured
# that day: training scout 21.5k, live scout 20.7k (3 movers), training decider 25.2k, live decider 16.0k, before
# events; every part has its own budget (desk_shared THREAD_CHARS, pathways BUDGET, REASSESS_EVENTS_MAX_CHARS),
# and a decision turn past desk_shared.PROMPT_FIT (36k) swaps its lessons for a pointer. A prompt over the cap is
# refused (the event is lost), so the cap sits above all of it.


# ---- LIVE: the same worker set, pointed at the real account (2026-10-11) ---------------------
# A SEPARATE kind table, not more entries in KINDS: driver.py builds the TRAINING engine from KINDS
# and the LIVE engine from LIVE_KINDS, so a training demand can never even name a live kind and a
# live demand can never name a paper one -- the separation is structural, not a convention.
#
# Each live kind mirrors its training twin (same model, effort, budget, job) with paper.py swapped
# for the live tools: live_market.py (reads, no order verb) for everyone, live_broker.py's
# order/close/modify/journal only for the decider ("live") and the per-symbol live traders.
# What training learned reaches live through kstore (papers, field, the paper ledger) and
# live_board.py lessons (training reviews' conclusions) -- read-only, never the training board's
# raw pre_trade posts, whose prices are from the replay.
#
# Deliberately NOT in the live set: evolve (no code changes while real money runs -- routing for
# live is operator-owned tools/live_routing.py anyway), verify, research. The training "risk" kind
# is replaced by "live_risk", which owns the LIVE lever (state/live_risk.json, live_broker.py
# risk-factor --set) -- a separate lever from paper's, so replay evidence never moves real-money
# sizing by itself (operator, 2026-10-11: 0.3% to start, "let the risk analysis worker decide").
# reflect and write carry over unchanged: neither can touch a market (no tools / papers only).
#
# Two independent switches still gate every real order inside live_broker.py itself: state/MODE
# must be LIVE and state/LIVE_ARMED must exist (operator-only; nothing below can create it).
_LIVE_READ = [f"Bash(python3 {LIVE_BROKER} status*)",
              f"Bash(python3 {LIVE_MARKET} symbols*)", f"Bash(python3 {LIVE_MARKET} bars*)",
              f"Bash(python3 {LIVE_MARKET} trades*)", f"Bash(python3 {LIVE_MARKET} ledger*)",
              f"Bash(python3 {KSTORE} *)", f"Bash(python3 {VOCAB} *)",
              f"Bash(python3 {LIVE_BOARD} post*)", f"Bash(python3 {LIVE_BOARD} read*)",
              f"Bash(python3 {LIVE_BOARD} lessons*)",
              f"Read({ABS}/field/**)", f"Read({ABS}/knowledge/**)"]
LIVE_NOTES = LIVE_BASH_NOTE + " " + LIVE_BOARD_NOTE
# price alerts on the live desk (2026-10-18): the twins of training's alert kinds (trade, scout, the symbol
# traders) -- the same set tools/parity_check.py kinds.*.alert compares
_LIVE_ALERT = f"Bash(python3 {LIVE_MARKET} alert*)"

LIVE_SYMBOL_TRADER_KINDS = [f"live_trade_{s.lower().replace('.', '_')}" for s in SYMBOL_TRADERS]
_LIVE_SPECIALISTS = ["live_scout", "live_chart", "live_timing"]

LIVE_KINDS = {
    # the decider AND executor: the live twin of "trade" (turn_reassess -> trade in training)
    "live": {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "high",
             "allow": _LIVE_READ + [_LIVE_ALERT, f"Bash(python3 {LIVE_BROKER} order*)", f"Bash(python3 {LIVE_BROKER} close*)",
                                    f"Bash(python3 {LIVE_BROKER} modify*)", f"Bash(python3 {LIVE_BROKER} journal*)",
                                    f"Read({ABS}/manual/**)", f"Read({ABS}/papers/**)"],
             "budget_usd": 0.60,
             "may_spawn": ["live_review", *_LIVE_SPECIALISTS, "write", *LIVE_SYMBOL_TRADER_KINDS],
             "notes": LIVE_NOTES},
    # a confirmed move in a symbol with no dedicated live trader goes to the decider itself, so the
    # 5-minute screen can lead to a trade in ANY symbol between decision turns, not only the four
    "live_scout": {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "medium", "allow": _LIVE_READ + [_LIVE_ALERT],
                   "budget_usd": 0.30, "may_spawn": [*LIVE_SYMBOL_TRADER_KINDS, "live"], "notes": LIVE_NOTES},
    "live_chart": {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "medium", "allow": list(_LIVE_READ),
                   "budget_usd": 0.30, "may_spawn": [], "notes": LIVE_NOTES},
    "live_timing": {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "medium", "allow": list(_LIVE_READ),
                    "budget_usd": 0.20, "may_spawn": [], "notes": LIVE_NOTES},
    # post-hoc only: may hand analysis, a paper or a risk question onward, never an entry
    "live_review": {"tools": ["Read", "Glob", "Grep", "Bash"], "model": "sonnet", "effort": "high",
                    "allow": _LIVE_READ + [f"Read({ABS}/manual/**)", f"Read({ABS}/agent_code/**)",
                                           f"Read({ABS}/papers/**)", "Glob", "Grep"],
                    "budget_usd": 0.50, "may_spawn": ["reflect", "write", "live_risk", *_LIVE_SPECIALISTS],
                    "notes": LIVE_NOTES},
    # the live twin of "risk": the ONLY kind that may set the live per-trade lever. Reads the live
    # record and the paper history; cannot order, close, modify or journal.
    "live_risk": {"tools": ["Bash", "Read", "Glob", "Grep"], "model": "sonnet", "effort": "high",
                  "allow": _LIVE_READ + [f"Bash(python3 {LIVE_BROKER} risk-factor*)", f"Read({ABS}/manual/**)",
                                         f"Read({ABS}/papers/**)", "Glob", "Grep"],
                  "budget_usd": 0.70, "may_spawn": ["live_review", "reflect"], "notes": LIVE_NOTES},
    "reflect": KINDS["reflect"],
    # its OWN copy: training's write kind gains dream-editing verbs below, and live must only ever
    # PROPOSE dreams (live_dream), never revise what training has made of them
    "write": __import__("copy").deepcopy(KINDS["write"]),
    # 2026-10-11 (operator): once a Prague day, distils the day's live process lessons into DREAMS for
    # the training replay -- through tools/dreams.py, whose checks refuse any date, price, instrument,
    # market claim or provenance. No market tool, no board, no spawn: it cannot trade, cannot read the
    # account, cannot change code. Its only output is what passes the dream checks.
    "live_dream": {"tools": ["Bash"], "model": "sonnet", "effort": "medium",
                   "allow": [f"Bash(python3 {DREAMS} propose*)", f"Bash(python3 {DREAMS} read*)"],
                   "budget_usd": 0.30, "may_spawn": [],
                   "notes": ("Every Bash call is ONE plain command, the exact absolute path, no cd, no && or ;, no pipes. "
                             f"dreams.py's subcommands for you, exactly: python3 {DREAMS} read [-n N] | python3 {DREAMS} "
                             "propose --text \"...\". A proposal is refused -- with its reasons -- if it names a date, "
                             "year, month, clock time, price-like number, instrument, ticker or market, claims what a "
                             "market did, says where it came from, or near-duplicates an existing dream; at most 3 "
                             "are accepted per day. A refusal is the check working: rephrase as a timeless process "
                             "rule, or drop it.")},
}


def _live_symbol_trader(sym: str) -> dict:
    return {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "high",
            "allow": [f"Bash(python3 {LIVE_BROKER} order --symbol {sym} *)",
                      f"Bash(python3 {LIVE_BROKER} close --symbol {sym} *)",
                      f"Bash(python3 {LIVE_BROKER} modify --symbol {sym} *)",
                      f"Bash(python3 {LIVE_BROKER} journal*)", _LIVE_ALERT,
                      *_LIVE_READ, f"Read({ABS}/manual/**)"],
            "budget_usd": 0.50, "may_spawn": ["live_review", *_LIVE_SPECIALISTS],
            "notes": (LIVE_NOTES + f" You may only place, close or modify {sym} -- live_broker.py itself refuses "
                      "a close/modify whose ticket is another symbol, and your permission rule only matches "
                      f"--symbol placed right after the subcommand: 'order --symbol {sym} --side ... --lots ... "
                      f"--sl ... --why \"...\"', 'close --symbol {sym} ID --why \"...\"', 'modify --symbol {sym} "
                      "ID --sl ... --why \"...\"' in exactly that order or the call is denied. A pre_trade post "
                      f"on the live board (--topic pre_trade --symbol {sym}) before ordering tags the order "
                      "consulted_analysis=true automatically; ordering without one is allowed too. "
                      + _DS.trader_guide(sym, f"python3 {LIVE_BOARD}", f"python3 {LIVE_BROKER} journal") + "\n"
                      + _DS.entry_rules("live") + "\n")}


for _sym in SYMBOL_TRADERS:
    LIVE_KINDS[f"live_trade_{_sym.lower().replace('.', '_')}"] = _live_symbol_trader(_sym)

# 2026-10-19: the live twins of manage / risk's widen / consult (see the training block above). Everything a manager
# or the risk worker does goes through tools/live_broker.py, which refuses any position that is not this assembly's
# own (MAGIC): the other runners' positions on the shared account are never touched.
_LIVE_CONSULT = f"Bash(python3 {CONSULT} live *)"
LIVE_CONSULT_NOTE = _DS.consult_note(f"python3 {CONSULT} live")
LIVE_KINDS["live_manage"] = {
    "tools": ["Bash", "Read"], "model": "sonnet", "effort": "high",
    "allow": _LIVE_READ + [f"Bash(python3 {LIVE_BROKER} close*)", f"Bash(python3 {LIVE_BROKER} modify*)",
                           f"Bash(python3 {LIVE_BROKER} journal*)", f"Bash(python3 {LIVE_MARKET} watch*)",
                           _LIVE_CONSULT, f"Read({ABS}/manual/**)"],
    "budget_usd": 0.40, "may_spawn": ["live_risk"],
    "notes": (LIVE_NOTES + " You manage OPEN positions only (this assembly's own): there is no order verb for you, "
              "and a wider stop is the live_risk kind's decision (next_demands live_risk). " + LIVE_CONSULT_NOTE)}
LIVE_KINDS["live_risk"]["allow"] = LIVE_KINDS["live_risk"]["allow"] + [f"Bash(python3 {LIVE_BROKER} widen*)", _LIVE_CONSULT]
LIVE_KINDS["live_risk"]["notes"] = LIVE_KINDS["live_risk"]["notes"] + " " + _DS.WIDEN_RULES + " " + LIVE_CONSULT_NOTE
for _k in ("live", *LIVE_SYMBOL_TRADER_KINDS):
    LIVE_KINDS[_k]["allow"] = LIVE_KINDS[_k]["allow"] + [_LIVE_CONSULT]
    LIVE_KINDS[_k]["notes"] = LIVE_KINDS[_k]["notes"] + " " + LIVE_CONSULT_NOTE
LIVE_KINDS["live_vault"] = {"tools": ["Bash", "Read"], "model": "sonnet", "effort": "medium",
                            "allow": [f"Bash(python3 {VAULT} report*)", f"Bash(python3 {VAULT} similar --desk live *)",
                                      *_LIVE_READ],
                            "budget_usd": 0.40, "may_spawn": [], "notes": LIVE_NOTES}

# ---- DREAMS in TRAINING (2026-10-11, operator: "training can use and test them and also modify them as
# per will"). Every training kind that consults its knowledge can READ them; the reflective kinds
# (review, write, evolve) can also NOTE a test result on a dream, REVISE its rule or RETIRE it. All
# through tools/dreams.py -- no kind has a raw Edit on knowledge/ -- so even training's own edits stay
# timeless (no date, price, instrument): the replay wraps epochs, and a dated note would leak into a
# later replay of an earlier window. Live kinds get none of this (live_dream only proposes).
DREAMS_NOTE = (f"DREAMS: python3 {DREAMS} read [-n N] lists short process intuitions of unknown origin. They are "
               "hypotheses, never facts: test them against your own record, and your own data always wins. If your "
               f"allow list grants them: python3 {DREAMS} note ID --text \"how it held up\" | revise ID --text \"new rule\" "
               "--why \"...\" | retire ID --why \"...\" (ID like 3 or \"dream 003\"). Keep them timeless: the tool refuses "
               "dates, prices, instruments and market claims.")
_DREAM_READERS = ("trade", "review", "risk", "scout", "chart", "timing", "write", "evolve", *SYMBOL_TRADER_KINDS)
_DREAM_EDITORS = ("review", "write", "evolve")
for _k in _DREAM_READERS:
    KINDS[_k]["allow"] = KINDS[_k]["allow"] + [f"Bash(python3 {DREAMS} read*)"]
    KINDS[_k]["notes"] = KINDS[_k].get("notes", "") + " " + DREAMS_NOTE
for _k in _DREAM_EDITORS:
    KINDS[_k]["allow"] = KINDS[_k]["allow"] + [f"Bash(python3 {DREAMS} note*)", f"Bash(python3 {DREAMS} revise*)",
                                               f"Bash(python3 {DREAMS} retire*)"]

# The live engine's own caps, on its OWN queue (state/live_selfcall.db). Spend is not a constraint
# (operator, 2026-10-11: the subscription's usage window is what matters, and a live day uses a
# fraction of one -- todos/003). The engine's caps are cumulative and would eventually halt live
# for good, so they are set out of reach; the one real stop is LIVE_DAILY_MAX_CALLS, a RUNAWAY
# guard per Prague day (a normal day is ~25-60 calls; only a spawn loop gets near this). Same
# depth/children as training so the decider -> scout -> trader -> review chain fits.
LIVE_LIMITS = {"max_depth": 3, "max_children": 4, "max_runs": 10**9, "max_total_cost_usd": 10.0**9,
               "timeout_s": 900, "max_prompt_chars": 40_000}
LIVE_DAILY_MAX_CALLS = 400
