"""ROUTING: which event wakes which kind of Claude demand, and what it is told.

Editable by the agent (evolve kind). route(event, payload) returns (kind, prompt) or None.
None means "record it, do not wake the model". Kinds and their tools live in
guard/policy.py and cannot be widened from here.

Every prompt carries the operator context, because each invocation starts cold.
"""
import json
import sys

from pathlib import Path

HOME = str(Path(__file__).resolve().parents[1])  # the home this router belongs to
sys.path.insert(0, f"{HOME}/tools")
import vocab as _VOCAB  # noqa: E402  (the desk vocabulary block, shared word for word with live_routing.py)
PAPER = f"python3 {HOME}/tools/paper.py"
BOARD = f"python3 {HOME}/tools/board.py"
MANUAL = f"{HOME}/manual/OPERATOR_MANUAL.md"

import desk_shared as _DS  # noqa: E402  (what a twin worker SEES, shared with live_routing.py -- tools/parity_check.py info.*)
LESSONS = f"{BOARD} lessons"
CONSULT = f"python3 {HOME}/tools/consult.py training"
import manage_core as _MC  # noqa: E402  (the manager's wake: the same rules as the live loop)

# Hard rules, not advice (2026-10-18): ids 26-29 were four XAUUSD Asia market entries at an untested level,
# stops 0.3-1 pt past the swing, each justified by pacing, each about -1R (-190.09 USD). Prose lessons on
# the board after 26, 27 and 28 were cited and not applied, so the check rides in every trader's prompt.
# Since the same day it lives in tools/desk_shared.py: the live desk carries the same rules, and every
# symbol trader's own notes (guard/policy.py) carry them too, so a child sees them without being pasted them.
ENTRY_CHECKLIST = _DS.entry_rules("training")

# 2026-11-01 (operator: "trim it -- it is not using all the variables it is fed"): measured over the 11-01 replay's 325
# calls, no worker read the manual, the field notes, real_progression, the dreams or kstore find; chart/timing were
# never consulted. The long paragraphs on those, on the risk lever's philosophy (now in the risk kind's own prompt) and
# on the attempt-end close (now in that review's own prompt) went; what every kind uses stayed, one line each.
CONTEXT = f"""OPERATOR CONTEXT (you start cold each time):
- A PAPER REPLAY of real history: M5 bars, M15/H1/H4/D1 resampled from them, 34 symbols with their MT5 specs. Every
  bar you see is CLOSED: the replay stops right after an M5 close, as the live desk wakes. A simulated 10,000 USD
  account on the live evaluation's terms: +10% with at least 2 traded days, never a 5% day loss or a 10% total loss,
  no deadline. An attempt ends by passing or breaching; the next one starts on its own (history wraps into epochs).
- """ + _DS.OBJECTIVE + f"""
- THE CLOCK (the live desk's): weekdays 07:00-20:00 Europe/Madrid -- a decision turn every hour, the move screen on
  every closed M5 bar between turns (a scout wakes at the bar a symbol moved), your price alerts, risk warnings.
  Outside the window nothing wakes; open positions run on their stop and target. No weekend holding.
- PRICE ALERTS: {PAPER} alert --symbol S --above P|--below P --sl P --tp P --why "plan" [--when touch|close|hold|reject
  --tf M15 --bars N] [--hours H] -- code watches every bar for free and wakes the symbol's trader once when the
  condition is met inside the window (--sl/--tp: the plan's mock levels). At most 3 wait; alert --cancel ID --why.
- TOOLS: {PAPER} status|bars|order|modify|close|journal|alert|alerts|fitness|risk-factor (read) |
  {BOARD} read|post|lessons | python3 {HOME}/agent_code/kstore.py spec SYMBOL (lot spec) |
  {CONSULT} chart|timing|vault --symbol S --question "..." (a specialist's answer inside your turn).
- SYMBOL TRADERS trade_xauusd, trade_eurusd, trade_usdjpy, trade_us100_cash may trade only their own symbol: hand
  them that symbol via next_demands with your levels. Only the risk kind sets risk_pct.
- Every Bash call is ONE plain command with the absolute path: no cd, no && or ;, no pipes, no 2>&1. state/ is not
  yours to Read (paper.py and kstore.py filter the replay's future out). Papers: {HOME}/papers/; code:
  {HOME}/agent_code/ (back up before editing).
- DREAMS: python3 {HOME}/tools/dreams.py read -- process HYPOTHESES of unknown origin, never by faith: your own record
  wins. Yours to shape: note ID / revise ID / retire ID (--text "...", --why "..."), timeless (no date, price, instrument).
""" + ENTRY_CHECKLIST + "\n" + _VOCAB.prompt_block() + """
- Your reply must match the output schema: status, summary, findings, sources, next_demands.
  Put concrete numbers in findings. Keep next_demands to what truly needs doing."""

RISK_PHILOSOPHY = """THE RISK LEVER'S PHILOSOPHY: it persists across attempts (a lever for the account's whole life). Thin,
inconclusive trading -> LOWER risk_pct so more setups fit under the same caps (more reps, smaller bets); RAISE it only
when the record shows a real pattern over a meaningful sample, never because a few trades hit big."""

ATTEMPT_END_NOTE = """This exit was MECHANICAL: when an attempt passes or breaches, paper.py force-closes every open position
in code before any kind acts (guard/rules.py DEMO_AUTO_CLOSE_AT_WINDOW_END). Do not judge the exit or its timing."""


# What a scout needs to do ITS job (2026-10-18): a scout screens a move and posts or hands it on -- it never
# sizes, never touches the risk lever, never judges an attempt-end close -- so it gets this instead of the full
# CONTEXT above (~8k characters in every scout prompt, the most frequent call there is). The tool syntax,
# the one-plain-command rule and the board rules reach it anyway, through guard/policy.py notes.
SCOUT_CONTEXT = f"""OPERATOR CONTEXT (you start cold; this is what a scout needs):
- A paper replay of real history on the live evaluation's terms (+10% with at least 2 traded days, never a 5% day /
  10% total loss, no deadline), weekdays 07:00-20:00 Europe/Madrid. The replay is stopped right after THIS bar's close;
  every candle attached is closed. A child you spawn sees the same bar.
- Your tools: {PAPER} bars|alert|alerts, {BOARD} read|post. Orders are the traders' job: next_demands trade_<symbol>
  (trade_xauusd, trade_eurusd, trade_usdjpy, trade_us100_cash) or the decider kind "trade" for any other symbol, with
  entry, stop, target and invalidation in the prompt.
- PRICE ALERTS: {PAPER} alert --symbol S --above P|--below P --sl P --tp P --why "plan" [--when touch|close|hold|reject
  --tf M15 --bars N] [--hours H] [--from scout] -- free until the condition is met on a closed bar inside the window,
  then that symbol's trader (or the decider) wakes once with your plan. At most 3 wait at once.
- The candles, today's thread and the lessons for each attached mover are below: read more only for a symbol that
  is not attached.
- Your reply must match the output schema: status, summary, findings, sources, next_demands."""
# 2026-11-01: no pre-order checklist and no vocabulary block here. A scout places no order, the trader it hands to carries both,
# and on the same 21 moments they cut NOW from 5 to 3 (todos/009). tools/parity_check.py KNOWN_DRIFT records it; live is unchanged.


def _goals_text(goals):
    return "\n".join(f"  [{'PASS' if g['passed'] else '    '}] {g['goal']}: {g['evidence']}" for g in goals)


REASSESS_MAX_CHARS = 36_000   # guard/policy.py LIMITS max_prompt_chars 40k since 2026-10-18 (was 20k / 18.5k)
REASSESS_EVENTS_MAX_CHARS = 2_500   # the scan/alert events were already handled at their hour; the board has the rest


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _events_text(events, budget=6000):
    """The events since the last decision turn, one compact line each, newest kept when they would not
    fit the budget (the engine refuses a prompt over LIMITS max_prompt_chars, which would skip the turn)."""
    lines = [_compact(e) for e in events or []]
    out, used = [], 0
    for line in reversed(lines):
        if used + len(line) > budget:
            out.append(f"(... {len(lines) - len(out)} older events not shown; {PAPER} ledger has them)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(reversed(out)) if out else "(none)"


STATE_KEYS = ("now_utc", "prague_day", "balance", "equity", "day_pnl", "daily_loss_used_usd", "daily_loss_limit_usd",
              "total_loss_used_usd", "total_loss_limit_usd", "open_risk_usd", "floating_pnl_usd", "positions", "pending",
              "traded_days", "attempt", "risk_pct", "per_trade_cap_usd", "alerts_waiting", "breached")


def _state(snap: dict) -> str:
    """The account as a decision needs it (2026-11-01: the 34 last closes, the epoch/window counters and the pass
    streaks left the prompt -- the movers line and the attached candles carry the prices that matter)."""
    return _compact({k: snap.get(k) for k in STATE_KEYS if k in snap})


def _goal_line(snap: dict) -> str:
    target = round(10_000 * (1 + (snap.get("profit_target_pct") or 10) / 100), 2)
    return (f"GOAL: equity {snap.get('equity')} vs the +{snap.get('profit_target_pct', 10)}% target {target}, traded days "
            f"{snap.get('traded_days')}/{snap.get('min_trading_days', 2)}; attempt #{snap.get('attempt')} (pass rate "
            f"{snap.get('pass_rate')} over {snap.get('attempts_total')}).")


def _own(p: dict, scout: bool = False) -> str:
    """The desk's own context (tools/castigation.py), then the castigation directive while it stands."""
    out = (p.get("scout_text") if scout else p.get("self_text")) or ""
    if p.get("castigated") and p.get("castigation_text"):
        out = p["castigation_text"] + "\n\n" + out
    return out + "\n\n" if out else ""


def _reassess(p):
    snap = p["snapshot"]
    movers = p.get("movers") or []
    movers_text = ", ".join(f"{m['symbol']} {m['pct_move']:+.2f}%" for m in movers) or "(none)"
    nxt = p.get("next_decision_utc")
    now = snap.get("now_utc") or snap.get("utc")
    text = (_own(p) + f"TURN REASSESS at {now} (a DECISION TURN, one every hour inside the window, as on the live desk). "
            + (f"The next one is at {nxt}; " if nxt else "This is today's last one; ")
            + "the move screen and your alerts keep running in between.\n\n" + CONTEXT + "\n\n"
            f"STATE (paper.py status at {now}):\n" + _state(snap) + "\n" + _goal_line(snap) + "\n\n"
            f"TOP MOVERS OF THE LAST HOUR (pure code, all 34 symbols): {movers_text}\n\n"
            + (p["movers_bars"] + "\n\n" if p.get("movers_bars") else "")
            + (p["board_text"] + "\n\n" if p.get("board_text") else "")
            + (p["dreams_text"] + "\n\n" if p.get("dreams_text") else "")
            + "EVENTS SINCE THE LAST DECISION TURN (scouts and traders handled the scan and alert ones at their hour):\n"
            "<<EVENTS>>\n\n" + "<<LESSONS>>"
            + _DS.DECIDER_TASK + "\n" + _DS.TRAINING_DECIDER_NOTE + "\n"
            + _DS.plan_line(f"{PAPER} alert") + " Never to make a traded day.\n\n"
            "HOW TO ACT (each Bash call ONE plain command, absolute path, exactly as written):\n"
            f"  - more candles only if the attached ones are not enough: {PAPER} bars SYMBOL -n 24 --tf M5|M15|H1|H4|D1\n"
            f"  - trade: {PAPER} order --symbol S --side buy|sell --lots X --sl P [--tp P] --why \"CODE ...\" (fills at the next\n"
            f"    bar's open; lot spec: python3 {HOME}/agent_code/kstore.py spec S), or hand a symbol trader its symbol\n"
            f"  - plan: {PAPER} alert ... (see PRICE ALERTS); manage: {PAPER} close|modify --symbol S ID ... --why \"...\"\n"
            f"  - then ONE journal note ({PAPER} journal --text \"...\") and ONE board post ({BOARD} post --from trade\n"
            "    --topic decision --text \"VERDICT ...\" -- one line per symbol you looked at)\n"
            "  - next_demands: scout (breadth, same bar) or trade_<symbol> (that symbol's own full turn).\n"
            "Do not invent prices or claim fills the tool did not confirm; check each symbol on its own candles.")

    def build(lessons):
        body = text.replace("<<LESSONS>>", lessons + "\n\n" if lessons else "")
        room = max(800, min(REASSESS_EVENTS_MAX_CHARS, REASSESS_MAX_CHARS - len(body)))
        evs = p.get("recent_events") or []
        hits = sum(1 for e in evs if e.get("type") == "setup_candidate")
        rest = [e for e in evs if e.get("type") != "setup_candidate"]   # their verdicts are on the board, attached
        txt = _events_text(rest, budget=room) + (f"\n({hits} screen hit(s): what the scouts made of them is on the board "
                                                 "above)" if hits else "")
        return body.replace("<<EVENTS>>", txt)
    return _DS.fit_lessons(build, p.get("lessons_text") or "", LESSONS, REASSESS_MAX_CHARS)


def route(event, payload):
    if event == "turn_reassess":
        return "trade", _reassess(payload)

    if event == "trade_closed":
        streak = payload.get("consecutive_losses", 0)
        streak_note = ("THREE OR MORE LOSSES IN A ROW. Check whether the same mistake repeats before "
                       "deciding anything; a streak alone is not a reason to change code.\n"
                       if streak >= 3 else "")
        consulted = payload.get("consulted_analysis")
        sym = payload.get("symbol", "")
        if payload.get("reason") == "attempt_end":
            return "review", (
                f"TRADE CLOSED (reason attempt_end, {sym}, R={payload.get('r_multiple')}).\n{CONTEXT}\n\n"
                + ATTEMPT_END_NOTE + " Judge only the ENTRY:\n"
                "TRADE RECORD:\n" + json.dumps(_DS.record(payload, ("goals",)), indent=1) + "\n\n"
                "  a. Was opening this big, at this time, a sound choice given the attempt's equity and how close\n"
                "     it was to its target or its loss limit? Holding that size there is itself a decision, even\n"
                "     though the close was not.\n"
                "  b. Was it sized within the current risk-factor cap? Check with paper.py risk-factor if unsure.\n"
                f"If there is a lesson for next time an entry is opened this close to the target or a limit, post it: "
                f"{BOARD} post --from review --topic lesson --symbol {sym or 'SYMBOL'} --text \"...\".")
        return "review", (
            f"TRADE CLOSED ({payload.get('result')}, reason {payload.get('reason')}, "
            f"R={payload.get('r_multiple')}).\n{CONTEXT}\n\n{streak_note}"
            "TRADE RECORD:\n" + json.dumps(_DS.record(payload, ("goals",)), indent=1) + "\n\n"
            "FIRST, before judging anything else -- this is the meeting, and a specialist's work is not reviewed\n"
            "unless you actually go look it up, win or lose:\n"
            f"  {BOARD} read --topic pre_trade --symbol {sym} -n 10\n"
            f"paper.py already tells you consulted_analysis={consulted} for this trade (it checked the board "
            "itself at order time, this is not self-reported). If consulted_analysis is true, the read above "
            "must find that post: quote what it said, state plainly whether the trade FOLLOWED it (same "
            "direction/level or not) and whether the analysis itself was right about the market, regardless of "
            "whether the trade won or lost -- a correct read can still be stopped out, a wrong one can still win.\n"
            "If consulted_analysis is false, say that plainly too: no specialist analysis existed before this "
            "entry, so there is nothing to credit or blame beyond the trader's own reasoning. Never write this\n"
            "section as if you had not checked -- the board is read-only and permanent, checking it costs nothing.\n"
            + (payload["lessons_text"] + "\nDid this trade FOLLOW or IGNORE the lessons above that applied to it? Name "
               "the card ids.\n\n" if payload.get("lessons_text") else "") +
            "QUESTIONS (answer only after the board check above):\n"
            "  a. Was the decision process sound at the time, judged from the rationale and the data before the entry?\n"
            "     Judge the process, not the outcome. A good process can lose.\n"
            "  b. Did the order obey the guard (stop present, risk cap, correct side)? Check the ledger if unsure:\n"
            f"     {PAPER} ledger -n 40   (decisions only; --types trade_closed -n 15 for the trade history)\n"
            "  c. Was the exit what the plan said (stop, target, manual close, weekend_flatten)? Was it timely?\n"
            "  d. Does this trade teach something that should be written down? If yes, next_demands a write.\n"
            f"     Did its rationale apply a dream (python3 {HOME}/tools/dreams.py read)? Then note how it held up:\n"
            f"     python3 {HOME}/tools/dreams.py note ID --text \"...\" (timeless: no date, price or instrument).\n"
            "  e. Is there a repeated pattern across at least three trades in the ledger? Only then propose an evolve,\n"
            "     and say exactly which file and what change, with the reason tied to the goal.\n"
            "R here is r_multiple = price R (the DeltaForce dashboard definition, gross of costs); r_net is the net-of-costs R.\n"
            "Report findings with numbers (R, net USD, bars held). Mark your judgement as judgement.\n"
            f"Post your conclusion either way, including what the pre-trade analysis got right or missed if one "
            f"existed: {BOARD} post --from review --topic lesson --symbol {sym or 'SYMBOL'} --text \"...\" -- that is\n"
            "what the next trade turn and the next specialist for this symbol both read, not this review's own output.")

    if event == "day_closed":
        if not (payload.get("traded") or payload.get("graded_n") or payload.get("journal_notes_today")):
            # an untraded day does not need a model call (2026-10-18: before, a day with only a skip note
            # was reviewed too, and 5 of 7 such reviews said the same "flat day, honest note"). The next
            # decision turn still sees this day_closed among its events.
            return None
        return "review", (
            f"DAY CLOSED {payload.get('day')}: pnl {payload.get('day_pnl')} USD "
            f"({payload.get('day_pnl_pct')}%), traded={payload.get('traded')}, "
            f"traded days so far {payload.get('traded_days_total')}.\n{CONTEXT}\n\n"
            "DAY RECORD:\n" + json.dumps(_DS.record(payload, ("graded_text", "graded_n")), indent=1) + "\n\n"
            + (payload["graded_text"] + "\n\n" if payload.get("graded_text") else "") +
            "QUESTIONS:\n"
            "  a. Is today's journal note present and honest? If there is no note, say so; the goal P_journal needs one.\n"
            "     (You cannot write a journal note yourself -- it is the trader's own record; reporting the gap is the job.)\n"
            f"  b. How much of the daily limit was used, and what was the worst moment? See {PAPER} goals.\n"
            "  c. Anything tomorrow must watch for? Put it in findings, not in an order.\n"
            "  d. If the day was a loss streak or a limit scare, a write demand summarising it is worth it.")

    if event == "calibration_review":
        import opportunities as _OPP
        return "review", (f"CALIBRATION REVIEW after {payload.get('days')} trading days.\n{CONTEXT}\n\n"
                          + str(payload.get("text", "")) + "\n\n" + _OPP.CALIBRATE_TASK)

    if event == "setup_candidate":
        cands = payload.get("candidates") or []
        if not cands or payload.get("castigated"):
            return None   # 2026-11-01: a castigated desk's scouts are muted (tools/castigation.py); the movers still
                          # reach the next decision turn, which must trade
        names = ", ".join(f"{c['symbol']} ({c['pct_move']:+.2f}% over {c['bars']} bars)" for c in cands)
        own = [c["symbol"] for c in cands if c["symbol"] in ("XAUUSD", "EURUSD", "USDJPY", "US100.cash")]
        pace = ""   # 2026-10-18: no deadline, no traded-days pace -- a scout is never told to hand on for a quota
        return "scout", (
            _own(payload, scout=True) + f"MOVE SCREEN at the close of the M5 bar opened {payload.get('utc', 'now')}: {names} moved past "
            "the threshold IN THE LAST HOUR. The replay is stopped right after this close: you are looking at the move "
            "as it happens.\n" + SCOUT_CONTEXT + "\n\n"
            + ("" if payload.get("bars_text") else
               f"THE CANDLES could not be attached this time ({payload.get('view_error', 'no view')}): read them "
               f"yourself -- {PAPER} bars SYMBOL -n 24 --tf M15, -n 24 --tf H1, -n 3 --tf D1.\n\n")
            + "".join(payload[k] + "\n\n" for k in ("bars_text", "threads_text", "lessons_text") if payload.get(k))
            + _DS.SCOUT_TASK + "\n" + _DS.TRAINING_SCOUT_NOTE + "\n"
            + _DS.plan_line(f"{PAPER} alert") + " HANDING IT ON: next_demands the matching "
            "trade_<symbol>" + (f" ({', '.join(own)} have one)" if own else "") + " with the levels, or the decider "
            "kind \"trade\" for any other symbol -- NOW, rather than waiting for the next decision turn." + pace)

    if event == "level_alert":
        sym = payload.get("symbol", "")
        kind = f"trade_{sym.lower().replace('.', '_')}" if sym in ("XAUUSD", "EURUSD", "USDJPY", "US100.cash") else "trade"
        return kind, (
            _own(payload) + f"PRICE ALERT {payload.get('id')} MET: {payload.get('condition') or (sym + ' traded ' + str(payload.get('dir')) + ' ' + str(payload.get('level')))} "
            f"-- on the bar ending with the M5 bar opening {payload.get('touched_utc') or payload.get('utc')} "
            f"(the {payload.get('tf') or 'M5'} bar: {_compact(payload.get('bar'))}). It was set by "
            f"{payload.get('by')} at {payload.get('set_utc')} with price at {payload.get('ref')}, and this was its plan:\n"
            f"  \"{payload.get('why')}\"\n" + _DS.plan_levels(payload) + f"{CONTEXT}\n\n"
            + (payload["symbol_file"] + "\n\n" if payload.get("symbol_file") else
               (f"THE LAST 18 CLOSED M5 BARS:\n{payload['bars_text']}\n\n" if payload.get("bars_text") else "")
               + (payload["lessons_text"] + "\n\n" if payload.get("lessons_text") else ""))
            + (payload["dreams_text"] + "\n\n" if payload.get("dreams_text") else "")
            + "The replay is stopped on this bar. " + _DS.ALERT_TASK + "\n" + _DS.TRAINING_ALERT_NOTE)

    if event == "risk_review":
        return "risk", (
            "RISK CHECKPOINT (the end of the trading window" + (", weekly" if payload.get("weekly") else "") + "; the replay "
            "is past it).\n" + CONTEXT + "\n\nTHE LEVERS NOW:\n" + json.dumps(payload.get("lever", {}), indent=1)
            + "\n\nTHE RECORD (closed paper trades):\n"
            + json.dumps({k: payload.get(k) for k in ("day", "closed_today", "today", "last_20", "all_time")}, indent=1)
            + "\n\n" + RISK_PHILOSOPHY
            + "\n\nREAD MORE IF YOU NEED IT: " + f"{PAPER} fitness | {PAPER} ledger -n 30 | {PAPER} attempts | "
            f"python3 {HOME}/tools/vault.py report --desk training\n"
            f"  {PAPER} risk-factor [--set PCT] [--day-halt F] [--giveback F] [--max-positions N] --why \"...\"\n"
            "     fractions (0.003 = 0.3%): per-trade risk in [0.001, 0.05]; own day-loss halt up to 0.05; giveback halt\n"
            "     up to 0.10; open positions 1-100.\n"
            + _DS.RISK_TASK + f"\n  ({BOARD} post --from review --topic risk --text \"...\")")

    if event == "vault_review":
        return "vault", (
            f"THE VAULT'S DAILY VALUE EXTRACTION (simulated day {payload.get('day')}).\n{CONTEXT}\n\n"
            + (payload.get("vault_text") or f"(the vault could not be built: {payload.get('view_error', '?')})") + "\n\n"
            + _DS.VAULT_REVIEW_TASK + "\n"
            f"TOOLS: python3 {HOME}/tools/vault.py report --desk training [--symbol S] | python3 {HOME}/tools/vault.py similar "
            f"--desk training --symbol S --side buy|sell [--entry P --sl P] | {BOARD} read --topic vault_lesson -n 20 | "
            f"{BOARD} post --from vault --topic vault_lesson [--symbol S] --text \"...\".")

    if event == "turn_swing":
        snap = payload.get("snapshot") or {}

        def build(lessons):
            return (_own(payload) + "TURN SWING: the once-a-day swing decision (the replay is stopped right after this "
                    "bar's close).\n\n" + CONTEXT + "\n\n"
                    "STATE (paper.py status):\n" + _state(snap) + "\n" + _goal_line(snap) + "\n\n"
                    "THE DAY'S MOVERS (code: first M5 open of the trading day to now; close_in_range 1 = at the day's "
                    "high):\n" + json.dumps(payload.get("movers", []))
                    + "\n\nEVENTS SINCE THE LAST DECISION TURN:\n" + _events_text(payload.get("recent_events"), budget=3000)
                    + "\n\n" + (payload.get("bars_text") or f"(candles not attached: {payload.get('view_error', '?')})")
                    + "\n\n" + (lessons + "\n\n" if lessons else "")
                    + (payload["dreams_text"] + "\n\n" if payload.get("dreams_text") else "") + _DS.SWING_TASK + "\n"
                    f"TOOLS: {PAPER} status | {PAPER} order --symbol S --side buy|sell --lots L --sl P --tp P --why \"SWING ...\" "
                    f"(fills at the next bar's open) | {PAPER} bars SYMBOL -n N --tf D1|H4|H1 | {CONSULT} chart|timing --symbol S "
                    f"--question \"...\" | {PAPER} journal --text \"...\" | {BOARD} post --from trade --topic swing --text \"VERDICT ...\".")
        return "trade", _DS.fit_lessons(build, payload.get("lessons_text") or "", LESSONS, REASSESS_MAX_CHARS)

    if event == "manage_wake":
        sym = payload.get("symbol", "")
        pid = payload.get("id")
        trig = payload.get("triggers") or []
        return "manage", (
            f"POSITION MANAGER WAKE: {sym} {payload.get('side')} position {pid} -- "
            + ", ".join(x["code"] for x in trig) + f".\n{CONTEXT}\n\n"
            + _MC.describe(payload, payload.get("info") or {}, trig) + "\n\n"
            + (payload["symbol_file"] + "\n\n" if payload.get("symbol_file") else
               f"(the symbol file could not be built: {payload.get('view_error', '?')} -- read {PAPER} bars {sym} yourself)\n\n")
            + _DS.MANAGE_TASK + "\n"
            f"YOUR TOOLS: {PAPER} status | {PAPER} close --symbol {sym} {pid} [--volume V] --why \"...\" | {PAPER} modify --symbol "
            f"{sym} {pid} [--sl P] [--tp P] --why \"...\" | {PAPER} journal --text \"...\" | {BOARD} post --from manage --topic "
            f"manage --symbol {sym} --text \"MANAGE ...\" | {CONSULT} chart|timing --symbol {sym} --question \"...\" | "
            f"{PAPER} watch --symbol {sym} {pid} [--above P] [--below P] [--in MIN] [--quiet [--for MIN]] [--clear] --why \"...\" | "
            "next_demands risk for a widen (the position id, the symbol, the proposed stop and why). The replay is stopped "
            "on this bar; a close or a partial fills at the next bar's open.")

    if event == "risk_warning":
        return "trade", (
            f"RISK WARNING: {payload.get('kind')} loss has reached {payload.get('used_pct_of_limit')}% of its limit.\n"
            f"{CONTEXT}\n\nWARNING RECORD:\n" + json.dumps(_DS.record(payload), indent=1) + "\n\n"
            "The guard refuses any order whose worst case would reach the limit, so you cannot add risk past it.\n"
            f"Check {PAPER} status. You may hold, tighten a stop with modify, or close. Do not add exposure to 'recover'.\n"
            f"Then journal your decision with {PAPER} journal --text \"...\".")

    if event == "rule_breach":
        return "reflect", (
            f"RULE BREACH ({payload.get('kind')}) at equity {payload.get('equity')} on {payload.get('day')}. "
            "This attempt ends now and the next one starts on its own; there is nothing to act on this instant. "
            "Say in one line what happened, for the record. The full review comes with attempt_ended.")

    if event == "goal_passed":
        return "review", (
            f"GOAL PASSED: {payload.get('goal')}. Evidence: {payload.get('evidence')}.\n{CONTEXT}\n\n"
            "GOAL LADDER NOW:\n" + _goals_text(payload.get("goals", [])) + "\n\n"
            "Record the evidence in findings. Then decide whether the next rung needs a change in process "
            "(journal habits, review habits, routing). Only propose an evolve if a concrete change would have "
            "helped in the record so far, and name the file.")

    if event == "attempt_ended":
        if payload.get("outcome") == "passed":
            return None  # attempt_passed_review covers this one in full, no need to also do the light version
        if payload.get("outcome", "").startswith("breached") or payload.get("near_miss"):
            return None  # attempt_breach_review covers this one in full instead
        return "reflect", (
            f"ATTEMPT {payload.get('attempt')} ENDED ({payload.get('outcome')}), equity {payload.get('final_equity')}, "
            f"{payload.get('traded_days')} traded days, {payload.get('start_day')}..{payload.get('end_day')}.\n"
            "This reflect kind has no tools on purpose: the numbers above are deliberately ALL you get, so that "
            "this stays cheap. Do not report status \"blocked\" for lacking the ledger or more detail -- that is "
            "expected, not a problem. A fresh attempt has already started; nothing to act on. "
            "Answer status \"done\" with, in one or two lines, the single most useful thing to remember for the "
            "next attempt, reasoned only from the outcome and the numbers given here (an attempt that ran out with "
            "the history, data_end, is neither a pass nor a fail -- a real breach or near-miss gets its own deep "
            "review, not this one).")

    if event == "attempt_passed_review":
        streak = payload.get("consecutive_passes", 1)
        best = payload.get("best_pass_streak", streak)
        streak_line = (f"This is pass #{streak} IN A ROW (best streak so far: {best}). Continuity is the point: "
                       if streak > 1 else f"This breaks or starts a streak (best so far: {best}). ")
        return "review", (
            f"ATTEMPT {payload.get('attempt')} PASSED: equity {payload.get('final_equity')}, "
            f"{payload.get('traded_days')} traded days.\n{CONTEXT}\n\n"
            f"Read this attempt's record: {PAPER} fitness, and the ledger ({PAPER} ledger -n 200) if you need the "
            "detail (you may need a fresh review child with Read/Bash for that; this one has review tools).\n"
            "Work out WHAT WENT GOOD: which symbols, which setups, what sizing, what timing. Separate what you can "
            "credit to a repeatable process from what was luck (small samples lie).\n"
            f"{streak_line}name EXACTLY what must NOT change about the current process (sizing, symbol choice, "
            "entry timing, risk factor, use of the specialists) for this to keep happening attempt after attempt -- "
            "not a vague 'keep doing well', the specific habit. If something about THIS pass differs from the "
            "last one (board.py read --topic lesson is the place to check what the previous pass credited), say "
            "whether it still applies or whether the account is now relying on something new and less tested.\n"
            "Write the finding into a paper, and only propose an evolve if a specific, named change to agent_code/ "
            "would make repeating this more likely -- not a vague 'do more of this'.\n"
            f"This is one rehearsal of the live evaluation's own bar (+10%, at least 2 traded days, no deadline) -- "
            f"{PAPER} real-eval shows the running total. Ask in findings whether the SAME process repeats under "
            "varied symbols and conditions, not just whether this one attempt got there.")

    if event == "attempt_breach_review":
        outcome = payload.get("outcome", "")
        kind = "a BREACH" if outcome.startswith("breached") else "a NEAR MISS (no breach, but came close)"
        return "review", (
            f"ATTEMPT {payload.get('attempt')} ENDED IN {kind}: {outcome}, equity {payload.get('final_equity')}, "
            f"{payload.get('traded_days')} traded days, {payload.get('risk_warnings_this_attempt', 0)} risk "
            f"warning(s) fired, attempt {payload.get('start_day')}..{payload.get('end_day')}.\n{CONTEXT}\n\n"
            "This is data, not a verdict on anyone -- the goal stays +10%, regardless, and a bad attempt is as "
            "much a source of intel as a good one. Read this attempt's record in full: "
            f"{PAPER} ledger -n 200 (and fitness). Determine, from the actual trades and their timing, which of "
            "these it was, as specifically as the ledger supports:\n"
            "  a. MARKET CONDITIONS: a regime/volatility shift the process had no way to see coming (compare "
            "     the entries' own rationale against what price actually did after).\n"
            "  b. OVERTRADING: more risk on, or more frequent entries, than the setups actually justified --\n"
            "     check consecutive_losses, whether sizing grew after a loss, and whether a traded-day quota\n"
            "     pushed a weak entry.\n"
            "  c. UNDERTRADING / wrong caution: sitting out a real setup, or exiting a sound position early,\n"
            "     out of excess caution rather than the plan.\n"
            "  d. A SIZING OR PROCESS BREAK: the risk factor, a stop, or a rule that was not actually followed.\n"
            "Say which, with the specific trades as evidence, and if it is truly unclear say so rather than "
            "forcing an answer. Write the finding into a paper either way -- a well-understood bad attempt is "
            "worth as much as a well-understood good one. Only propose an evolve with a precise, named change "
            "tied to the actual cause found, never a reflexive 'be more careful'.\n"
            "This is also a legitimate risk-factor checkpoint: next_demands a \"risk\" kind call with what "
            "caused this, so it can decide whether the current per-trade cap fits -- do not set it yourself.")

    if event == "pass_rate_milestone":
        return "review", (
            f"PASS RATE MILESTONE: {payload.get('pass_rate')} ({int(payload.get('milestone', 0) * 100)}% crossed) "
            f"over {payload.get('attempts_total')} attempts, {payload.get('attempts_passed')} passed.\n{CONTEXT}\n\n"
            "Step back from any one attempt. Across everything so far: what is actually working, what is "
            "wasted motion, and what would make the NEXT stretch of attempts more efficient at passing -- not "
            "just lucky? You may propose a write (a dated progress paper) and, only with a concrete, evidenced "
            "reason, an evolve naming the exact file and change.\n"
            "This is also a checkpoint for the risk factor: next_demands a \"risk\" kind call with what you see "
            "in fitness/attempts (trade count, win rate, consistency) so it can decide, from the evidence, "
            "whether the current per-trade cap still fits -- lower if trading has been thin with no clear edge, "
            "higher only with a real pattern over a meaningful sample. Do not set it yourself.")

    if event == "epoch_review":
        return "review", (
            f"EPOCH {payload.get('epoch')} FINISHED: the whole history has been played once this epoch. "
            f"{payload.get('attempts_total')} attempts total, {payload.get('attempts_passed')} passed "
            f"(rate {payload.get('pass_rate')}). The replay is about to wrap back to the first window and start "
            f"epoch {int(payload.get('epoch', 0)) + 1}.\n{CONTEXT}\n\n"
            "This is the deep-review point. Use your tools to investigate: read papers/ for what earlier attempts "
            "and milestones concluded, check the ledger across symbols and time, and decide what, if anything, "
            "about your PROCESS (not a strategy you did not derive from data) should change before the next epoch. "
            "You may spawn an evolve with a precise, named change and a reason tied to the record, or a research "
            "demand if a field gap is blocking you, or simply write a paper saying the process is sound as is. "
            "Do not change the guard or the policy; they are not yours.\n"
            "An epoch boundary is also the natural point to next_demands a \"risk\" kind call: give it this "
            "epoch's attempts/fitness numbers and let it decide the per-trade risk factor from the evidence, "
            "balanced for the long run -- not a reach for the moon because a few trades hit big.")

    if event in ("field_ready", "order_rejected"):
        return None  # order_rejected shows up in the next reassess's recent events

    if event == "research_request":
        return "research", payload.get("prompt", "")

    raise ValueError(f"no route for event {event!r}")
