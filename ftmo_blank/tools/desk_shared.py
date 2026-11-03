"""THE SHARED DESK: what a twin worker SEES, defined once for both desks (2026-10-18).

Operator: "training should have access to the same kind of data, if not, we are not training correctly ...
we can't have that drift, it will not trade live correctly". Training (driver.py turn + agent_code/routing.py)
and live (driver.py live-tick + tools/live_routing.py) used to build each worker's view separately, and the
views drifted apart without any test noticing:
  * a training scout got 12 M15 candles of the mover (3 hours); a live scout read 24 M15 + 24 H1 itself;
  * training's traders had a pre-order checklist (learned in training) that live's never saw;
  * live's decider read the last 10 lessons by date, training's read none; no scout read any;
  * the scout's task was worded differently on each desk.
Everything below is imported by BOTH routers, and tools/parity_check.py (info.*) drives both with the same
market and compares what lands in the prompt, so a difference fails the tests instead of going unseen.

  PACKET / packets()   the candles a scout look starts with, per moving symbol. Lossless: the same table a
                       bars call prints (paper.bars_text) -- every bar's time, session, open, high, low and
                       close at full precision. Nothing is rounded, summarised or derived; a worker that wants
                       another timeframe or a longer view still has its bars tool.
  thread()             today's board posts for one symbol, in full: the symbol's running conversation, so the
                       worker that wakes for it picks up where the last one left off.
  lessons()            the lessons on the situation's path (tools/pathways.py), in full, plus the rest named.
  SCOUT_TASK           what a scout is asked to do, word for word the same on both desks.
  entry_rules()        the pre-order checklist; rules 1-2 word for word, rule 3 says what pacing means on that
                       desk (training has a 10-day window, live has no deadline).
  trader_guide()       what a symbol trader reads first, same on both desks.

fetch(symbol, tf, n) -> rows shaped like paper.Paper.bars_view / live_market.bars (utc, day, tf, session,
o, h, l, c), or [{"error": ...}].
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PACKET = (("M15", 24), ("H1", 24), ("D1", 3))   # 6 hours of the move, a day of the trend, yesterday's range
TRADER_PACKET = (("M5", 24), ("M15", 24), ("H1", 24), ("D1", 3))   # + 2 hours of M5 for the entry itself
TRADER_THREAD_CHARS = 3_000
RECORD_N = 6                                    # the symbol's last closed trades on this desk, each in full
PACKET_SYMBOLS = 3                              # movers per scan with candles attached (a scan flags up to 5)
THREAD_N = 8                                    # most recent posts of a symbol's thread today
THREAD_CHARS = 2_000                            # per symbol: newest posts in full up to this, older ones counted


def bars_text(symbol: str, tf: str, rows: list[dict]) -> str:
    import paper as P
    return P.bars_text(symbol, tf, rows)


def packet(fetch, symbol: str, spec=PACKET) -> str:
    return "\n".join(bars_text(symbol, tf, fetch(symbol, tf, n)) for tf, n in spec)


def packets(fetch, symbols: list[str], bars_cmd: str) -> str:
    """The packet for the first PACKET_SYMBOLS symbols; any further one is named, with the command."""
    head = (f"THE CANDLES, ATTACHED (closed bars only, exactly the table `{bars_cmd} SYMBOL -n N --tf TF` prints; "
            "columns utc_open session open high low close): per mover "
            + ", ".join(f"{tf} x{n}" for tf, n in PACKET)
            + " -- M15 for the move itself, H1 for the trend it sits in, D1 for yesterday's high and low (PDH/PDL)."
            " Pull M5, H4 or a longer view with that command whenever the decision needs it.")
    out = [head] + [packet(fetch, s) for s in symbols[:PACKET_SYMBOLS]]
    if len(symbols) > PACKET_SYMBOLS:
        out.append("NOT ATTACHED (read before judging them): " + ", ".join(symbols[PACKET_SYMBOLS:])
                   + f" -- `{bars_cmd} SYMBOL -n 24 --tf M15` and `--tf H1`.")
    return "\n".join(out)


def thread(posts: list[dict], day: str | None, symbol: str, day_of, budget: int | None = None) -> str:
    """Today's posts for `symbol`, oldest first, each in full (board.read_text's one-line form).
    day_of(post) -> that post's trading day (Prague), so both desks filter by the same rule."""
    import board as TB
    # 2026-11-01: the reviews' lessons reach a worker on the lessons path (which keeps the calibration record away from
    # entry decisions); the thread is the symbol's running conversation -- scans, plans, decisions, consults
    today = [p for p in posts if (day is None or day_of(p) == day) and "lesson" not in str(p.get("topic", ""))]
    keep, used = [], 0
    for p in reversed(today[-THREAD_N:]):          # newest first, each post whole or not at all
        line = TB.read_text([p])
        if keep and used + len(line) > (budget or THREAD_CHARS):
            break
        keep.insert(0, p)
        used += len(line) + 1
    if not keep:
        return f"{symbol}: nothing posted for it yet today."
    older = len(today) - len(keep)
    return (f"{symbol} (last {len(keep)} post(s) today" + (f"; {older} earlier one(s) not shown -- board read "
            f"--symbol {symbol} -n {len(today)}" if older else "") + "):\n" + TB.read_text(keep))


def threads(read_symbol, symbols: list[str], day: str | None, day_of) -> str:
    """read_symbol(symbol) -> that symbol's posts on this desk's board (all days; filtered here)."""
    return ("TODAY ON THE BOARD FOR THESE SYMBOLS (what the desk already found -- start from it, do not redo it):\n"
            + "\n".join(thread(read_symbol(s), day, s, day_of) for s in symbols[:PACKET_SYMBOLS]))


def lessons(desk: str, role: str, symbols, phases, when, text: str = "", cmd: str = "lessons", home=None) -> str:
    import pathways as PW
    sit = PW.situation(symbols, phases, _when(when), text=text)
    return PW.brief(sit, desk, role, home or PW.HOME, cmd)


def _when(w):
    """'2025-05-30 08:55 UTC' / '2025-05-30 08:55' / a datetime -> an aware datetime (None stays None)."""
    import datetime as dt
    if w is None or isinstance(w, dt.datetime):
        return w if (w is None or w.tzinfo) else w.replace(tzinfo=dt.timezone.utc)
    try:
        return dt.datetime.strptime(str(w)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


# One move counted once (2026-11-01): A5 twins were 236 of the 11-01 replay's 725 verdicts -- a scout per twin, each
# saying "same move as the other". Both screens (paper.py and live_loop.py) route only the strongest mover of each
# group; the twins are still flagged, so they are not screened again for the hour. Same groups as the lessons' walk.
TWIN_GROUPS = ({"XAUUSD", "XAUEUR"}, {"XAGUSD", "XAGEUR"}, {"XPTUSD", "XPDUSD"},
               {"US100.cash", "US30.cash", "US500.cash"}, {"GER40.cash", "UK100.cash"}, {"USOIL.cash", "UKOIL.cash"})


def collapse_twins(cands: list[dict]) -> list[dict]:
    """The candidates with each correlated group reduced to its strongest |pct_move| (order kept)."""
    best: dict[int, dict] = {}
    for c in cands:
        g = next((i for i, grp in enumerate(TWIN_GROUPS) if c["symbol"] in grp), None)
        if g is not None and (g not in best or abs(c["pct_move"]) > abs(best[g]["pct_move"])):
            best[g] = c
    keep = {id(c) for c in best.values()}
    return [c for c in cands if id(c) in keep or not any(c["symbol"] in grp for grp in TWIN_GROUPS)]


VIEW_KEYS = ("bars_text", "threads_text", "lessons_text", "view_error")


def record(payload: dict, drop=()) -> dict:
    """An event's own record for a prompt's RECORD section: without the attached view (printed once, in its own
    section) and without `drop`."""
    return {k: v for k, v in payload.items() if k not in VIEW_KEYS and k not in drop}


PROMPT_FIT = 36_000   # both desks' decision turns stay under this (guard/policy.py max_prompt_chars 40k)


def fit_lessons(build, lessons_text: str, cmd: str, limit: int = PROMPT_FIT) -> str:
    """build(lessons) -> the whole prompt. If the prompt with its lessons would pass `limit`, the lessons
    block becomes a one-line pointer: a refused prompt would lose the whole turn; a pointer loses one tool call."""
    text = build(lessons_text)
    if len(text) <= limit or not lessons_text:
        return text
    return build(f"LESSONS: not attached this turn (the prompt was at its size cap) -- {cmd} --symbol S walks the "
                 "lessons for any symbol you consider, in full.")


# ---- the views, one per twin event, built the same way on both desks ----------------------------------------
def scout_view(desk: str, fetch, read_symbol, day, day_of, symbols: list[str], when, bars_cmd: str,
               lessons_cmd: str, home=None) -> dict:
    """What a scout's prompt carries for a scan: candles, today's thread and the lessons, per mover."""
    return {"bars_text": packets(fetch, symbols, bars_cmd),
            "threads_text": threads(read_symbol, symbols, day, day_of),
            "lessons_text": lessons(desk, "scout", symbols[:PACKET_SYMBOLS], ["entry"], when, "", lessons_cmd, home)}


def record_text(rows: list[dict], symbol: str) -> str:
    """A symbol's last closed trades on this desk, each in full (rationale included), and what they add up to:
    the trader sees how its own earlier trades in this symbol went, not only the lessons drawn from them."""
    rows = [r for r in rows if r][-RECORD_N:]
    if not rows:
        return f"{symbol}: no closed trade on this desk yet."
    rs = [r.get("r_net", r.get("r_price", r.get("r_multiple"))) for r in rows]
    rs = [x for x in rs if isinstance(x, (int, float))]
    head = (f"{symbol}, last {len(rows)} closed trade(s) on this desk: {sum(1 for x in rs if x > 0)} won, "
            f"{sum(1 for x in rs if x <= 0)} lost, mean R {sum(rs) / len(rs):+.2f}" if rs else f"{symbol}, last {len(rows)}")
    lines = [head]
    for r in rows:
        r_ = r.get("r_net", r.get("r_price", r.get("r_multiple")))
        lines.append(f"- {r.get('opened_utc', '?')} -> {r.get('closed_utc', '?')} {r.get('side', '?')} entry {r.get('entry')} "
                     f"sl {r.get('sl', r.get('initial_sl'))} tp {r.get('tp')} exit {r.get('exit')} ({r.get('reason')}) "
                     f"R {r_} | why: {r.get('why_open') or ''}")
    return "\n".join(lines)


def trader_view(desk: str, fetch, read_symbol, day, day_of, symbol: str, plan_text: str, when, record_rows,
                bars_cmd: str, lessons_cmd: str, home=None) -> str:
    """Everything a symbol trader reads first, built by code when it is woken (by an alert, or handed the
    symbol by a scout or the decider): it is the one that EXECUTES, so it gets the deepest view of all --
    candles down to M5, today's thread, the lessons on its plan's path and its own record in this symbol."""
    return "\n\n".join([
        f"THE SYMBOL FILE FOR {symbol} (built by code the moment you were woken; read it before anything else):",
        f"CANDLES (closed bars, exactly the table `{bars_cmd} {symbol} -n N --tf TF` prints): "
        + ", ".join(f"{tf} x{n}" for tf, n in TRADER_PACKET) + "\n" + packet(fetch, symbol, TRADER_PACKET),
        "TODAY ON THE BOARD FOR IT:\n" + thread(read_symbol(symbol), day, symbol, day_of, TRADER_THREAD_CHARS),
        lessons(desk, "trader", [symbol], ["entry", "stop", "target", "manage"], when, plan_text, lessons_cmd, home),
        "YOUR RECORD IN IT: " + record_text(record_rows, symbol)])


# The alert paragraph a desk's context carries, and what a woken trader is asked (both word for word on
# both desks; only the tool path differs).
def alerts_brief(alert_cmd: str, who: str = "that symbol's trader (or the decider)") -> str:
    return (f"- PRICE ALERTS turn \"wait for the retest\" into something that actually happens: {alert_cmd} --symbol S "
            "--above P|--below P --sl P --tp P --why \"the plan at that level\" [--when touch|close|hold|reject --tf M15 "
            "--bars N] (--sl/--tp: the plan's MOCK stop and target, required, opposite sides of the level -- the trader "
            "that wakes sets its own at entry) "
            f"[--hours H] [--from WORKER]. Code checks every closed bar for free; {who} wakes ONCE, when the CONDITION "
            "is met inside the window: touch = price trades there; close = a closed tf bar closes beyond; hold = N "
            "closes beyond; reject = trades through and closes back (T-TOUCH/T-CLOSE/T-HOLD/T-REJECT). Put the real "
            "condition in the alert, not in a note to re-check: a touch next to price is refused as a poll. At most 3 "
            "wait at once, each expires (default 8 hours); `alerts` lists them, `alert --cancel ID --why \"...\"` drops one.")


def plan_line(alert_cmd: str) -> str:
    return (f"THE PLAN: a price alert at the level ({alert_cmd} with the condition, a MOCK --sl and --tp, and the plan in "
            "--why: entry, invalidation; it wakes the trader there at zero cost until then, and the trader sets the real "
            "stop and target) plus the pre_trade post -- never chase an extended move.")


def plan_levels(alert: dict) -> str:
    """The alert's mock levels, as the woken trader is shown them (both desks)."""
    if alert.get("plan_sl") is None or alert.get("plan_tp") is None:
        return "  (this alert was set before mock levels were required: no plan stop/target)\n"
    return (f"  plan's MOCK levels: {str(alert.get('plan_side') or '?').upper()}, stop {alert['plan_sl']}, target "
            f"{alert['plan_tp']} -- the planner's sketch; yours come from the bars at entry\n")


ALERT_TASK = """You are at the level now, as planned. Check the plan on the closed bars of the symbol file above and take it
-- your own stop and target from the structure (the plan's are the planner's MOCK sketch), sized under the cap -- unless
(a) its invalidation printed, (b) price already covered half the way to the plan's target, or (c) with your stop at real
structure (past the sweeps) the first target no longer pays the risk. Re-anchoring the levels is your job; the plan was
written earlier, with less information than you have now. If the condition needs one more step, set a NEW alert.
Journal the decision and post it either way; a decline carries one line VERDICT SYM PASS [codes] | (a), (b) or (c) and
the numbers (the code grades it at the day's end)."""

# 2026-11-01, operator: "castigation only runs on training, not on live trading -- live doesn't get usage or nothing, it
# needs to trade perfectly, no rushing". The shared texts above are the desk's judgment, word for word on both desks;
# the push to trade is the TRAINING desk's alone (it learns only from trades and pays in usage) and rides in
# agent_code/routing.py after the shared text. tools/live_routing.py never carries these.
# Measured on the same 21 moments (todos/009 section 10): "most movers are not worth it" gave NOW 1, this gives about the blank desk's rate.
TRAINING_SCOUT_NOTE = ("ON THE TRAINING DESK: when you wake a move has always already happened, and a retest often never comes: "
                       "that alone is no reason to dismiss it.")
TRAINING_DECIDER_NOTE = ("ON THE TRAINING DESK: a turn that skips teaches nothing and still costs the operator's usage. When a "
                         "setup code fits at structure with a first target >= 1.5R, take it at the normal risk; the "
                         "alternative to a trade is a plan at its level, not a skip.")
TRAINING_ALERT_NOTE = ("ON THE TRAINING DESK: (c) means under 1.5R (the order is refused below that). Judge the plan on the closed bars: "
                       "declined plans so far are break-even on their own levels (18, mean +0.13R), so neither taking nor declining "
                       "is the safe habit; the structure in front of you decides.")


def decider_lessons(desk: str, positions: list[dict], movers: list[dict], when, cmd: str, home=None) -> str:
    """The decision turn's lessons: every symbol with an open position, then the top movers."""
    syms = []
    for s in [p.get("symbol") for p in positions or []] + [m.get("symbol") for m in (movers or [])[:PACKET_SYMBOLS]]:
        if s and s not in syms:
            syms.append(s)
    phases = ["entry", "manage"] + (["stop", "exit"] if positions else [])
    return lessons(desk, "decider", syms, phases, when, "", cmd, home)


def review_lessons(desk: str, rec: dict, cmd: str, home=None) -> str:
    """A closed trade's review: the lessons on that trade's own path (its symbol, the codes and sessions its
    rationale names, the hour it was opened) -- so the review can say whether it followed or ignored them."""
    text = " ".join(str(rec.get(k) or "") for k in ("why", "why_open", "rationale", "close_note", "comment"))
    when = rec.get("opened_utc") or rec.get("utc") or rec.get("closed_utc")
    return lessons(desk, "review", [rec.get("symbol")], ["entry", "stop", "target", "manage", "exit"], when, text,
                   cmd, home)


def alert_lessons(desk: str, alert: dict, cmd: str, home=None) -> str:
    """A trader woken at its alert's level: the lessons for that symbol and the plan's own codes."""
    return lessons(desk, "trader", [alert.get("symbol")], ["entry"], alert.get("touched_utc") or alert.get("utc"),
                   str(alert.get("why") or ""), cmd, home)


# Word for word on both desks: the job, with no tool path in it (the desk-specific lines come after it).
# 2026-11-01: back to what the scout's job was the week the live desk traded its winners ("if a move holds up, hand it on"), not
# a gatekeeper asked for a clean entry. Ablation on 21 moments: a "clean entry at this bar" task gave 0 NOW of 21 under
# every stack; this one gives NOW at about the blank desk's rate (todos/009). A hand-off is not an order: the trader decides.
SCOUT_TASK = """YOUR JOB: decide whether this move HOLDS UP on the candles attached (M15 the move, H1 the trend it sits in, D1
yesterday's range). You do not need a perfect entry: the trader you hand it to reads the M5 bars and its own record, sets the
entry, stop and target, and may still say no. One correlated move counts once (gold in USD and EUR, the US indices, the two oils).
  * NOW -- the move is real and still live: it broke something, it has not snapped back, it runs with H1 or makes a fresh
    extreme. Post it (topic pre_trade) and hand it on with what you see: the level it broke, where a stop would sit, the next
    target, what would kill it.
  * ALERT -- the move is real but already ran 3+ ATR(M15) from its base with no pause (A1), or the entry is a level still to
    come: set the alert at that level.
  * DISMISS -- noise, a spike already given back, mid-range drift, a correlated twin (A5), a gap (A4).
Post what you found either way, one line per mover in exactly this form (code grades it later):
  VERDICT SYM NOW|ALERT|WATCH|DISMISS [codes] | reason      e.g. VERDICT XAUUSD DISMISS A1 | 4 ATR impulse, no base"""


# Word for word on both desks' decision turns (2026-10-18: each desk had its own half of these -- training told
# its decider not to default to one symbol and never to chase, live told its decider that a skip is a good
# outcome and to check spreads). The desk-specific lines (training's alerts and pacing, live's status call)
# follow it in each router.
DECIDER_TASK = """HOW TO DECIDE (the same on both desks):
  * Three choices: a small stop-protected trade NOW where a setup code fits (stop at real structure, past the sweeps;
    first target >= 1.5R; risk within the cap), yourself or through the symbol's own trader; a PLAN at the level
    (an alert -- see THE PLAN below); or a skip with the reason, a normal outcome. No class of entry pays blind (every
    class loses about 0.2R on a blind bracket): what pays is your read of the structure in front of you.
  * Never chase an impulse that already ran 3+ ATR(M15) with no pause (A1): plan its retest instead.
  * Manage what is already open before adding anything: a stop only moves toward profit; an exit needs a reason the
    market gave.
  * Look past your usual symbol: a real move elsewhere deserves its candles.
  * Spread in R = spread / stop distance -- check it before a wide-spread entry. Each symbol on its own candles.
  * For every symbol you looked at and did NOT trade this turn, one line in your board post:
    VERDICT SYM SKIP [codes] | reason   -- the code grades every rejection by what price did next."""


ENTRY_RULES_CORE = """  1. Before 07:00 UTC: no order unless price has traded into the named zone AND a closed M15 bar rejected it.
  2. The stop clears the swing high/low by more than a typical M5 wick (XAUUSD: at least 6-8 pts), not 0-1 pt."""
# 2026-10-18: both desks trade the same terms (no deadline), and both have price alerts -> one text for both
_RULE3 = {d: "  3. Pacing is never the reason for an order -- and a skip needs a reason the bars show, not a habit." for d in
          ("training", "live")}
_INSTEAD = {d: "set an alert at the level or skip" for d in ("training", "live")}


def entry_rules(desk: str) -> str:
    """The pre-order checklist (2026-10-18: ids 26-29 were four XAUUSD Asia market entries at an untested
    level, stops 0.3-1 pt past the swing, each justified by pacing, each about -1R; prose lessons were cited
    and not applied, so the check rides in every prompt). Learned in training, word for word on both desks."""
    return (f"- PRE-ORDER CHECKLIST (every symbol; any NO = no order -- {_INSTEAD[desk]}):\n"
            + ENTRY_RULES_CORE + "\n" + _RULE3[desk])


def trader_guide(symbol: str, board_cmd: str, journal_cmd: str) -> str:
    """What every symbol trader does first, on both desks (its notes carry it, so a child gets it too)."""
    return (f"BEFORE DECIDING, for {symbol}: its thread today ({board_cmd} read --symbol {symbol} -n {THREAD_N}: "
            f"what the scouts and the decider already found) and its lessons ({board_cmd} lessons --symbol {symbol} "
            "--codes CODE, the setup code of the plan you were handed) -- both in full, test them against the bars. "
            f"Journal your decision ({journal_cmd} --text \"...\") when you trade or deliberately pass on a planned "
            "entry. ")


# ==== 2026-10-19: THE POSITION MANAGER, THE RISK WORKER'S WIDEN, THE SPECIALISTS ON CALL ====================
# Operator, 2026-10-19 (todos/006 sections 8, 12): a worker whose only job is the open trade; widening a stop
# belongs to the risk analyst; and a trader must be able to ask the chart and timing specialists and get a clear
# answer "near immediately", not at the next decision turn. Word for word on both desks (parity info.*).

MANAGE_TASK = """YOU MANAGE THIS OPEN POSITION -- nothing else. Code woke you because something changed (WHY YOU WERE
WOKEN above); the entry is done, your job is the best exit for the trade its plan described.
  * Judge from structure on the candles (the symbol file), not from the P/L number: is the plan still alive,
    did price reach a level that matters, is the move the plan wanted happening or failing?
  * Your actions, each with --why naming what the market did:
      HOLD -- do nothing; a normal result when the plan is on track.
      STOP TOWARD PROFIT -- break-even once the trade has room, or behind the newest structure (never wider).
      MOVE THE TARGET -- extend it to the next level when the move has room, or bring it in front of a level
        that is likely to hold.
      CLOSE PART -- bank some at a level and let the rest run (both parts must stay above the minimum size).
      CLOSE ALL -- the plan is invalid, or what is left to gain no longer pays for what is left to lose.
  * A STOP THAT IS ABOUT TO BE SWEPT: if the plan's invalidation is a CLOSE beyond a level and the hard stop sits on
    that level (or inside the reach of the wicks), only the risk worker may widen it -- next_demands the risk kind
    with the position id, the symbol, the proposed stop and why (once per trade; it decides, not you).
  * PRE_SWAP: the daily rollover is minutes away (spreads spike; a held position pays the swap): close, or keep it
    with a reason (the plan still valid, the stop clear of the rollover spike). A SWING trade (its plan says SWING)
    is meant to be held overnight: for it the rollover is part of the plan, a check, not a reason to leave.
  * PRE_FLATTEN: Friday's flatten closes everything soon -- take the exit you prefer before it does.
  * BURST (outside the trading window): price is moving hard right now. Does it threaten the plan (tighten or close)
    or is it the move the plan wanted (hold, or trail the stop behind it)? Never open anything.
  * When the decision turns on a level or a time, ask a specialist (CONSULT in your notes) -- the answer comes back
    inside this turn.
  * STAY AWAKE OR LET IT RIP -- your call, every time you are woken: does this trade need you again?
      watch --above P / --below P   wake me if price trades through a level that matters (the trigger of a trail,
                                    the edge of the range, the level a burst would break) -- at any hour;
      watch --in N                  wake me in N minutes (a move that needs one more look);
      watch --quiet [--for N]       let it rip: no automatic wake for this position (for N minutes, or until it
                                    closes) -- the broker's stop and target do the rest; the Friday flatten notice
                                    still comes. One standing watch per position; a new one replaces it; --clear drops it.
    Doing nothing leaves the normal wakes on (the next R milestone, the invalidation, the rollover, a night burst).
Journal what you did and why, and post ONE line to the board (topic manage, --symbol S):
  MANAGE SYM HOLD|BE|TRAIL|TP|PART|CLOSE|ASK_RISK [codes] | reason"""

WIDEN_RULES = """WIDENING A STOP IS YOURS ALONE (operator, 2026-10-19). A manager or trader may ask you to widen one open
position's stop because the plan's invalidation is a CLOSE beyond a level and the hard stop sits on that level, inside
the reach of the wicks (a stop on the level gets swept before the close that would prove the idea wrong). Decide from
the candles and the record, not from the asker's wish:
  * widen only to just past where the sweeps reach (the chart specialist's SWEEP DEPTH read: ask it), never further;
  * the code allows ONE widen per trade, at most twice the trade's risk at entry, never while the watchdog holds the
    rollover cushion, never past a halt, and only if every open stop could still hit inside the daily/total limits;
  * a widen makes this trade's loss bigger if it fails -- refuse when the plan is already invalid, and say why;
  * every widen is graded later against what price did: keep your reason concrete (the level, the wick reach)."""


def consult_note(consult_cmd: str) -> str:
    """The specialists-on-call paragraph, for every kind that may consult (traders, the decider, the manager, risk)."""
    return (f"CONSULT A SPECIALIST NOW -- its answer comes back INSIDE your turn, in about 20-60 seconds: {consult_cmd} chart "
            "--symbol S --question \"...\" [--side buy|sell] [--entry P] [--sl P] [--tp P] -> a multi-timeframe level "
            "read (is the level real structure, the invalidation close, a stop past the reach of the sweeps, the targets) | "
            f"{consult_cmd} timing --symbol S --question \"...\" -> the clock (session, how this hour trades for the "
            "symbol, the next open / rollover / flatten, now or later). Ask when the decision turns on a level or a time "
            "you are not sure of -- not as a ritual; at most 3 per turn. Its first line is a verdict (CHART VERDICT ... / "
            "TIMING VERDICT ...). The program posts it to the board for you (topic consult), which also marks a following "
            "order as consulted. "
            f"{consult_cmd} vault --symbol S --side buy|sell [--entry P] [--sl P] [--tp P] --question \"...\" -> the VAULT: "
            "every closed trade on every account whose entry looked like yours (trend, range location, the hour's move, "
            "candle, stop vs the sweep depth) and what happened after them (VAULT VERDICT ...).")


CHART_CONSULT_TASK = """YOU ARE THE CHART SPECIALIST, ASKED BY A TRADER WHO IS DECIDING RIGHT NOW. Answer in THIS reply,
from the candles attached (use a tool only if something essential is missing -- the trader is waiting):
  1. Is the level the trader names real structure on these candles (where it was made, how often it held)?
  2. The invalidation: the CLOSE that would prove the idea wrong, and the M15/H1 swing it sits behind.
  3. The STOP: past that swing by more than the sweeps reach -- the SWEEP DEPTH table says how far wicks have run
     past recent swings before closing back. Name a price.
  4. The targets: the next levels in the trade's direction, and whether the reward to the first one pays the stop.
The FIRST LINE of your summary, exactly in this form (the trader reads it first):
  CHART VERDICT TAKE|WAIT|SKIP | level L | invalidation: close <|> X | stop S | targets T1[, T2] | reason
then at most 5 short findings with the numbers. Do not post to the board and do not hand anything on (next_demands
empty): the program returns your answer to the trader and posts it."""

TIMING_CONSULT_TASK = """YOU ARE THE TIMING SPECIALIST, ASKED BY A TRADER WHO IS DECIDING RIGHT NOW. Answer in THIS reply,
from the clock and the hour table attached:
  1. Where we are: the session, how this hour usually trades for this symbol (the HOUR TABLE: its usual M5 range
     against the last bars), and what comes next (a session open, the rollover, the Friday flatten, the window end).
  2. Is NOW a good time for this trade, or is there a better one soon (after an open settles, not into the rollover,
     not into a dead hour)?
The FIRST LINE of your summary, exactly in this form:
  TIMING VERDICT NOW|WAIT UNTIL HH:MM UTC|AVOID | reason
then at most 4 short findings with the numbers. Do not post to the board and do not hand anything on."""


def sweep_depth(m15: list[dict], lookback: int = 96) -> str:
    """How far wicks have run PAST recent M15 swings before price closed back -- the stop question, measured by code.
    A swing low = a bar whose low is below the two bars on each side; a sweep = a later bar that traded below it and
    CLOSED back above (and the mirror for swing highs). Lossless where it matters: each sweep is listed."""
    rows = [r for r in (m15 or []) if "error" not in r][-lookback:]
    if len(rows) < 10:
        return "SWEEP DEPTH: not enough M15 bars."
    rng = sorted(float(r["h"]) - float(r["l"]) for r in rows[-24:])
    atr = sum(rng) / len(rng) if rng else 0.0
    found = []
    for i in range(2, len(rows) - 2):
        lo, hi = float(rows[i]["l"]), float(rows[i]["h"])
        if lo < min(float(rows[j]["l"]) for j in (i - 2, i - 1, i + 1, i + 2)):
            for r in rows[i + 3:]:
                if float(r["l"]) < lo and float(r["c"]) > lo:
                    found.append(("low", rows[i]["utc"], lo, r["utc"], lo - float(r["l"])))
                    break
                if float(r["c"]) < lo:
                    break
        if hi > max(float(rows[j]["h"]) for j in (i - 2, i - 1, i + 1, i + 2)):
            for r in rows[i + 3:]:
                if float(r["h"]) > hi and float(r["c"]) < hi:
                    found.append(("high", rows[i]["utc"], hi, r["utc"], float(r["h"]) - hi))
                    break
                if float(r["c"]) > hi:
                    break
    if not found:
        return (f"SWEEP DEPTH (last {len(rows)} M15 bars): no swing was swept and reclaimed; mean M15 range {atr:.6g}.")
    depths = sorted(x[4] for x in found)
    med = depths[len(depths) // 2]
    lines = [f"SWEEP DEPTH (last {len(rows)} M15 bars, code-measured): {len(found)} swing(s) swept and closed back; "
             f"how far past the swing the wick ran: median {med:.6g}, max {depths[-1]:.6g} "
             f"({med / atr:.2f} / {depths[-1] / atr:.2f} x the mean M15 range {atr:.6g})."]
    lines += [f"  swing {k} {lvl:.6g} ({t0}) swept {d:.6g} past it at {t1}, closed back" for k, t0, lvl, t1, d in found[-8:]]
    return "\n".join(lines)


def hour_table(m5: list[dict], last_n: int = 6) -> str:
    """This symbol's usual M5 range per UTC hour over the history given, and the last bars against it."""
    import statistics
    rows = [r for r in (m5 or []) if "error" not in r]
    if len(rows) < 48:
        return "HOUR TABLE: not enough M5 history."
    by: dict = {}
    for r in rows:
        by.setdefault(int(str(r["utc"])[11:13]), []).append(float(r["h"]) - float(r["l"]))
    days = round(len(rows) / 288, 1)
    head = f"HOUR TABLE (median M5 range per UTC hour, last ~{days} days of M5 bars; code-measured):"
    cells = [f"{h:02d}h {statistics.median(v):.6g}" for h, v in sorted(by.items())]
    tail = [f"  {r['utc']} range {float(r['h']) - float(r['l']):.6g}" for r in rows[-last_n:]]
    return head + "\n  " + " | ".join(cells) + "\nTHE LAST M5 BARS:\n" + "\n".join(tail)


def clock_text(now, window_text: str, in_window: bool, next_decision: str | None) -> str:
    """The timing specialist's clock: both desks build it from the same facts."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    import manage_core as MC
    roll = MC.rollover_utc(now)
    fri = MC.friday_flat_utc(now, MC.FRI_FLAT_HOUR_UTC)
    mad, ny = now.astimezone(ZoneInfo("Europe/Madrid")), now.astimezone(ZoneInfo("America/New_York"))
    mins = lambda t: int((t - now).total_seconds() // 60)
    return "\n".join([
        f"THE CLOCK: {now:%A %Y-%m-%d %H:%M} UTC = {mad:%H:%M} Madrid = {ny:%H:%M} New York.",
        f"  trading window: {window_text}; now {'INSIDE' if in_window else 'OUTSIDE'} it"
        + (f"; next decision turn {next_decision}" if next_decision else "") + ".",
        f"  daily rollover (17:00 New York, the broker's midnight; spreads spike, swap charged): {roll:%H:%M} UTC, in "
        f"{mins(roll)} min.",
        (f"  Friday flatten: {fri:%H:%M} UTC, in {mins(fri)} min." if fri else "  no Friday flatten ahead today."),
        "  sessions (UTC, approximate): Tokyo 00:00-06:00 | London open 07:00 (06:00 in UK summer time) | "
        "US cash open 13:30 (14:30 in US winter time) | US close 20:00 (21:00 in US winter time)."])


def manage_lessons(desk: str, pos: dict, when, cmd: str, home=None) -> str:
    """The manager's lessons: the position's symbol, on the manage / stop / target / exit path of its own plan."""
    return lessons(desk, "trader", [pos.get("symbol")], ["manage", "stop", "target", "exit"], when,
                   str(pos.get("why") or ""), cmd, home)


# Operator, 2026-10-20: "it has to just generate as much money as possible WITHIN the actual max dd daily and max dd
# total ... the target is just making as much money as possible with those hard lines". Word for word on both desks.
OBJECTIVE = """THE OBJECTIVE: make as much money as possible without ever crossing the two HARD LINES -- a 5% loss in a
  day (from the day's starting balance at the Prague midnight) and a 10% total loss (from the initial balance). Those
  two are enforced by code (the worst case of every open stop counts) and never change. Everything else -- your own
  halts, how many positions, risk per trade -- is a lever the risk worker sets from evidence. The +10% with at least
  2 traded days is the evaluation's pass mark: a milestone on the way, not a ceiling, and there is no deadline."""


SWING_PACKET = (("D1", 10), ("H4", 24), ("H1", 12))
SWING_SYMBOLS = 3


def swing_view(fetch, movers: list[dict], bars_cmd: str) -> dict:
    """The swing turn's candles: XAUUSD (the era's main symbol) plus the day's top movers, D1/H4/H1."""
    syms = ["XAUUSD"] + [m["symbol"] for m in movers if m["symbol"] != "XAUUSD"]
    syms = syms[:SWING_SYMBOLS]
    head = (f"THE HIGHER TIMEFRAMES, ATTACHED (closed bars, exactly the table `{bars_cmd} SYMBOL -n N --tf TF` prints): "
            + ", ".join(f"{tf} x{n}" for tf, n in SWING_PACKET) + f" for {', '.join(syms)}. Any other symbol: that command.")
    return {"bars_text": "\n".join([head] + [packet(fetch, s, SWING_PACKET) for s in syms])}


SWING_TASK = """THE ONCE-A-DAY SWING DECISION (Monday-Thursday, 75 minutes before the daily rollover, after the day's move).
This is how the desk traded when it did best in training: one look at the higher timeframes, a position held overnight
and into the next day (median hold ~17 h), the stop at H4/D1 structure (not M5 noise), the target at the next H4/D1
level -- about 2R or more. Gold carried most of it; read everything attached.
  * D1 for the trend and yesterday's range, H4 for the swing structure, H1 for where today closed inside it.
  * Take a swing where the higher-timeframe trend and today's close agree (a close near the day's extreme in the
    trend's direction, a reclaimed level, a breakout that held); otherwise skip -- a skip is a normal result.
  * The stop sits past H4 structure AND clear of the rollover spread spike and the night's wicks; size within the cap.
  * Write SWING in the order's --why, with the plan and its invalidation as "INV close <|> X" on H4/D1: the position
    manager reads it overnight (the rollover and the night are part of a swing's plan).
  * One swing per symbol; never against an open position in the same symbol (the self-hedge guard refuses anyway).
Journal it, and post one line per symbol you looked at: VERDICT SYM SWING|SKIP [codes] | reason."""


# ==== 2026-10-20: THE VAULT -- every closed trade on every account, with the entry's market read and what happened after
# (tools/vault.py). One worker reads it as paperwork every day; any deciding worker can ask it NOW (consult ... vault).
VAULT_CONSULT_TASK = """YOU ARE THE VAULT, ASKED BY A TRADER WHO IS DECIDING RIGHT NOW. Above: how the code reads this entry
(trend, where it sits in the range, the hour's move, the candle, the stop against the sweep depth), the past trades whose
entry looked most like it -- on every runner and account -- with what happened after them, and the vault's tables.
Answer in THIS reply, from that evidence only:
  1. Do look-alike entries pay? (n, win rate, mean R; say plainly when the sample is too small to mean anything.)
  2. What went wrong in the look-alikes that lost (a stop inside the sweep, a mid-range entry, a chase, ...)
     and what the ones that won did differently -- and what that means for THIS entry, stop and target.
The FIRST LINE of your summary, exactly:
  VAULT VERDICT TAKE|ADJUST|SKIP | n look-alikes, mean R | the one change that matters (e.g. stop past X, wait for Y)
then at most 4 short findings with the numbers. Do not post and do not hand anything on."""

VAULT_REVIEW_TASK = """YOU ARE THE VAULT'S VALUE EXTRACTOR. Above is the paperwork: every closed trade the desk and the
other runners have made, read by code at the entry and after it. Your job: find what is going wrong with THIS desk's
entries and what its optimal entries are -- the path to the light -- from the evidence, not from opinion:
  1. Today's trades of this desk: for each, which vault pattern it matched (its rel / loc / trend / candle / stop vs
     sweep) and how that pattern does across all the trades; what the better entry or stop would have been.
  2. Across the vault: which entry conditions pay and which lose (only with n >= 10 behind them, and say n), and where
     this desk's own habits sit against them -- especially the entry location, the stop against the sweep depth,
     chasing and fading, and trades that reached +1R and gave it back.
  3. Concrete, testable entry rules for this desk (each with its n and mean R): where to enter, where the stop goes,
     what to skip.
Post each finding as ONE board line (--from vault --topic vault_lesson --symbol S when it is about one symbol), in the
desk vocabulary: they reach every trader's lessons by situation. Do not repeat a vault lesson already on the board
unless the numbers changed. Nothing new and solid is a valid result: say so in one line."""


# 2026-10-20 (operator: "let the risk worker own them ... the only hard ones are the 5% daily and 10% total"): the risk
# checkpoint's task, word for word on both desks (live_risk_review at the live window's end, risk_review on the replay).
RISK_TASK = """YOU OWN THE RISK LEVERS: the risk per trade, and the desk's own rails -- its day-loss halt, its giveback-from-
peak halt, and how many positions it may hold at once. Only two lines are fixed: a 5% loss in a day and a 10% total loss
(code-enforced; the worst case of every open stop counts). The objective is the most money inside those two lines, so set
the levers for that, from the evidence above (and the vault's record, if you need more):
  * risk per trade: size UP when the record shows a real, repeatable edge over a meaningful sample (tens of trades,
    positive mean R net of costs, not one trade carrying it); size DOWN when it does not, and after a streak or a halt
    -- more reps on smaller bets until the edge shows;
  * the own halts: a halt that keeps cutting days that would have recovered costs money; one that never fires protects
    nothing. Put them where the record says they earn their keep -- never past the hard lines;
  * positions: more when the trades are independent (different symbols, not one correlated bet), fewer when they
    stack the same risk.
Leaving everything unchanged with a stated reason is a normal outcome. Post your conclusion (topic risk) either way."""
