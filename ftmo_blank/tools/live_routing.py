"""LIVE ROUTING: which live event wakes which live kind, and what it is told.

The live twin of agent_code/routing.py, deliberately OPERATOR-owned (tools/, not agent_code/):
the evolve kind may rewrite training's routing, but nothing an agent run can edit decides what
the workers are told while real money is on. Kinds and their tools are guard/policy.py
LIVE_KINDS; nothing here can widen them.

route(event, payload) returns (kind, prompt) or None (recorded, no model call). Every prompt
carries LIVE_CONTEXT, because each invocation starts cold.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_loop  # noqa: E402  (the window and cadence are defined once, there)
import vocab  # noqa: E402  (the desk vocabulary block, shared word for word with agent_code/routing.py)
import desk_shared as DS  # noqa: E402  (what a twin worker SEES, shared with agent_code/routing.py -- parity_check info.*)

HOME = str(Path(__file__).resolve().parents[1])
LB = f"python3 {HOME}/tools/live_broker.py"
LM = f"python3 {HOME}/tools/live_market.py"
BOARD = f"python3 {HOME}/tools/live_board.py"
KSTORE = f"python3 {HOME}/agent_code/kstore.py"
CONSULT = f"python3 {HOME}/tools/consult.py live"
import manage_core as MC  # noqa: E402  (the manager's wake: the same rules as the replay)
LIVE_TRADER_SYMBOLS = ("XAUUSD", "EURUSD", "USDJPY", "US100.cash")  # guard/policy.py SYMBOL_TRADERS


def _rails_text() -> str:
    """The assembly's own rails as they stand now (tools/live_broker.py levers, set by live_risk)."""
    try:
        rf = live_loop.LB.risk_factor()
        lv = rf["levers"]
        return (f"day-loss halt {lv['day_halt_pct']['value'] * 100:g}% of the initial balance, giveback halt "
                f"{lv['giveback_halt_pct']['value'] * 100:g}% from your own peak, at most {lv['max_positions']['value']} "
                f"open positions of your own, risk {rf['risk_pct'] * 100:g}% per trade.")
    except Exception as exc:
        return f"(could not read the levers: {exc}; status shows them)."


def live_context(p: dict) -> str:
    snap = p.get("snapshot") or {}
    cur = snap.get("currency", "the account currency")
    return f"""LIVE CONTEXT (you start cold each time; this is everything you need to orient):
- THIS IS THE REAL ACCOUNT. You are the same worker set that trained on the paper replay, now trading
  account {snap.get('login', '?')} ({cur}) on FTMO's MT5 server. Every fill is real; every loss is real.
- Rules that bind it (FTMO, field/real_progression.md): daily loss 5% from the day's starting balance at
  the Prague midnight reset, total loss 10% from the initial balance, both measured on THIS account
  ({cur}); the guard refuses any order whose worst case (every stop hit) would reach either limit.
  The evaluation's target: +10% with at least 2 trading days, and NO deadline -- no window, no reset, no
  traded-days quota (the training replay practises exactly these terms). Not trading is always allowed.
- """ + DS.OBJECTIVE + f"""
- What is different from paper -- read this, do not assume paper's habits carry over unchanged:
  * A market order fills NOW at the current ask (buy) / bid (sell), with possible slippage -- not at the
    next M5 bar open. Stops and targets are executed by the broker tick by tick.
  * The account is SHARED with other automated runners. You see their positions only as
    other_runners_positions; you may never open the opposite side of ANY open position in the same
    symbol (self-hedge guard, account-wide).
  * Your own rails, on your own P&L only -- LEVERS the live_risk worker sets from evidence (2026-10-20), bounded
    only by the two hard lines: """ + _rails_text() + f""" The per-trade cap is risk_pct
    of the real balance (status shows the current value).
  * Friday: tools/live_watchdog.py closes every one of your positions at 20:00 UTC and no new entry is
    accepted after that until the Sunday reopen. At the daily rollover (17:00 New York = 23:00 Madrid most
    of the year, 22:00 Madrid in the late-October and March weeks; the broker's midnight) the watchdog
    temporarily widens your SHORT stops by that instrument's measured rollover spike and restores them
    once its spread is back to normal (two hours at most).
  * Spreads are real costs: field/measured_spreads.md has each instrument's typical spread and its
    rollover spike. Spread cost in R = spread / stop distance -- check it before a wide-spread entry.
- WHAT YOU CAN TRADE -- every one of these can be ordered: {", ".join(sorted(live_loop.R.INSTRUMENTS))}.
  Four have a DEDICATED trader ({", ".join(LIVE_TRADER_SYMBOLS)}): a specialist to hand them to, NOT a
  limit -- every other instrument on this list goes to the decider (kind "live"). Never call a listed
  instrument "not tradable". Anything NOT on the list is read-only context (bars works, ordering does not).
  * Trading window: {live_loop.window_text()}. A screen hit wakes a scout, which can hand
    a confirmed setup straight to a symbol trader or to the decider, so a trade can happen on any 5-minute
    bar of the window, not only at a decision turn. A trade closing at the broker wakes a review.
- Tools (one plain command per Bash call, exactly as written):
    {LB} status                       mode/armed/connected, the account, YOUR open positions, what would refuse
    {LM} symbols | bars SYMBOL -n N --tf M5|M15|H1|H4|D1 | trades | ledger | alert ... | alerts
    {LB} order|close|modify|journal   only if YOUR kind has them (the decider, the live symbol traders; the manager
                                      has close|modify|journal): close [--volume V] = all or part; modify [--sl P] [--tp P]
    {CONSULT} chart|timing --symbol S --question "..."   a specialist's answer INSIDE your turn (kinds that have it)
    {BOARD} read -n 15 | lessons -n 10 | post --from WORKER --topic T --text "..."
    {KSTORE} find WORDS | trades | day YYYY-MM-DD    what training learned on paper (papers, field, ledger)
""" + DS.alerts_brief(f"{LM} alert") + """
- A refusal from live_broker.py is the guard doing its job. Never retry a variant of the same order to get
  around a refusal; say what refused it and why in your summary.
- Do not invent prices. Do not claim a fill the tool did not confirm. Check each symbol you trade yourself.
""" + DS.entry_rules("live") + "\n" + vocab.prompt_block() + """
- Your reply must match the output schema: status, summary, findings, sources, next_demands."""


def _snap_for_prompt(snap: dict) -> dict:
    """The snapshot minus nothing: it is already bounded (34 last closes, own positions, counts)."""
    return snap


def _reassess(p: dict) -> str:
    """The decision turn; its lessons give way to a one-line pointer if the prompt would pass the size cap."""
    return DS.fit_lessons(lambda lessons: _reassess_text(p, lessons), p.get("lessons_text") or "",
                          f"{BOARD} lessons")


def _reassess_text(p: dict, lessons: str) -> str:
    snap = p.get("snapshot") or {}
    traders = ", ".join(f"live_trade_{s.lower().replace('.', '_')}" for s in LIVE_TRADER_SYMBOLS)
    nxt = p.get("next_decision_utc")
    return ("LIVE DECISION TURN. You decide what the real account does now.\n\n" + live_context(p) + "\n\n"
            f"STATE (live_broker snapshot at {snap.get('utc')}):\n" + json.dumps(_snap_for_prompt(snap), indent=1)
            + "\n\nTOP MOVERS OVER THE LAST HOUR (code-level, 12 closed M5 bars; a move is not a setup):\n"
            + json.dumps(p.get("movers", []))
            + "\n\nEVENTS SINCE THE LAST DECISION TURN:\n" + json.dumps(p.get("recent_events", []), indent=1)
            + "\n\n" + (lessons + "\n\n" if lessons else "")
            + DS.DECIDER_TASK + "\n"
            + DS.plan_line(f"{LM} alert") + "\n\n" + (f"The next decision turn is at {nxt}; " if nxt else "This is the last decision turn of today's "
                          "window; ") + "the 5-minute screen keeps running in between.\n\n"
            "HOW TO ACT THIS TURN:\n"
            f"  1. {LB} status                                 confirm what is open and what would refuse right now\n"
            f"  2. {BOARD} read -n 15                          what the live specialists already found today\n"
            f"  3. the LESSONS above are the ones on this turn's path; {BOARD} lessons --symbol S walks another symbol's\n"
            f"  4. {LM} bars SYMBOL -n 24 --tf H1             real candles for anything you consider; last_close and the\n"
            "     movers are prices, not trends. Or next_demands live_scout (breadth). For a level or a time you are\n"
            f"     unsure of on a trade you are deciding: {CONSULT} chart|timing --symbol S --question \"...\" -- the\n"
            "     specialist answers INSIDE this turn (20-60 s); no need to wait for the next turn.\n"
            "  5. decide: hold; or manage an open position of yours (close all or --volume part / modify the stop toward\n"
            "     profit or the target; the live_manage worker is also woken by code for every open position of yours at\n"
            "     R milestones, its plan's invalidation, a stall, before the rollover); or\n"
            f"     {LB} order --symbol S --side buy|sell --lots L --sl P [--tp P] --why \"...\" -- small,\n"
            "     stop-protected, the stop at a real structure level, risk within the per-trade cap; or hand a\n"
            f"     dedicated symbol to its own trader ({traders}) with your levels.\n"
            f"  6. {LB} journal --text \"...\"            one note: what you saw and why you did or did not act\n"
            f"  7. {BOARD} post --from trade --topic decision --text \"...\"\n"
            "There is another decision turn soon: a skip now costs nothing.")


def _risk_review(p: dict) -> str:
    return ("LIVE RISK CHECKPOINT (end of the trading window" + (", weekly" if p.get("weekly") else "") + ").\n\n"
            + live_context(p) + "\n\nYOU OWN THE LIVE RISK LEVERS (per-trade risk and the assembly's own rails). Now:\n"
            + json.dumps(p.get("lever", {}), indent=1)
            + "\n\nLIVE RESULTS (this worker's own closed trades, from broker deal history):\n"
            + json.dumps({k: p.get(k) for k in ("day", "closed_today", "today", "last_20", "all_time")}, indent=1)
            + "\n\nREAD MORE IF YOU NEED IT:\n"
            f"  {LM} trades -n 30          every closed live trade with its R and exit reason\n"
            f"  {BOARD} read --topic lesson -n 15     what the live reviews concluded\n"
            f"  {KSTORE} trades -n 30        the paper record from training, for comparison (different conditions)\n"
            f"  {LB} risk-factor [--set PCT] [--day-halt F] [--giveback F] [--max-positions N] --why \"...\"\n"
            "     fractions (0.003 = 0.3%): per-trade risk in [0.001, 0.05]; own day-loss halt up to 0.05; giveback halt\n"
            "     up to 0.10; open positions 1-100.\n"
            + DS.RISK_TASK + f"\n  ({BOARD} post --from review --topic risk --text \"...\")")


def route(event, payload):
    if event == "live_reassess":
        return "live", _reassess(payload)

    if event == "live_setup_candidate":
        cands = payload.get("candidates") or []
        if not cands:
            return None
        names = ", ".join(f"{c['symbol']} ({c['pct_move']:+.2f}% over {c['bars']} M5 bars)" for c in cands)
        dedicated = ", ".join(f"live_trade_{s.lower().replace('.', '_')}" for s in LIVE_TRADER_SYMBOLS)
        nxt = payload.get("next_decision_utc")
        missing = ("" if payload.get("bars_text") else
                   f"THE CANDLES could not be attached this time ({payload.get('view_error', 'no view')}): read them "
                   f"yourself -- {LM} bars SYMBOL -n 24 --tf M15, -n 24 --tf H1, -n 3 --tf D1.\n\n")
        return "live_scout", (
            f"LIVE HOURLY SCAN (code-level, free until now): {names} moved past the threshold.\n"
            + live_context(payload) + "\n\n" + missing
            + "".join(payload[k] + "\n\n" for k in ("bars_text", "threads_text", "lessons_text") if payload.get(k))
            + DS.SCOUT_TASK + "\n"
            + DS.plan_line(f"{LM} alert") + " HANDING IT ON: next_demands the matching dedicated trader "
            f"({dedicated}) with the levels, or the decider kind \"live\" for any other symbol -- NOW, rather than waiting "
            "for the next decision turn" + (f" ({nxt})" if nxt else "") + ".")

    if event == "live_trade_closed":
        sym = payload.get("symbol", "")
        manual = payload.get("reason") in ("manual", "watchdog_flatten")
        record = DS.record(payload, ("snapshot",))
        return "live_review", (
            f"LIVE TRADE CLOSED ({payload.get('result')}, reason {payload.get('reason')}, "
            f"R={payload.get('r_price')}, net {payload.get('net')} {payload.get('currency', '')}).\n"
            + live_context(payload) + "\n\n"
            "TRADE RECORD (from the broker's own deal history):\n" + json.dumps(record, indent=1) + "\n\n"
            f"FIRST: {BOARD} read --topic pre_trade --symbol {sym} -n 10 -- consulted_analysis="
            f"{payload.get('consulted_analysis')} was set by the tool at order time, not self-reported. If true, quote "
            "the post and say whether the trade followed it and whether it was right about the market.\n"
            + (payload["lessons_text"] + "\nDid this trade FOLLOW or IGNORE the lessons above that applied to it? "
               "Name the card ids.\n" if payload.get("lessons_text") else
               f"THEN: {BOARD} lessons --symbol {sym} -- did this trade follow or ignore what was concluded?\n") +
            "QUESTIONS:\n"
            "  a. Was the decision sound at the time, judged from its rationale and the candles before entry?\n"
            "     Judge the process, not the outcome.\n"
            "  b. Did the real fill differ from what paper would have assumed (entry slippage vs ref_price,\n"
            "     exit vs the stop level)? Real-vs-paper differences are the most valuable thing this review finds.\n"
            + ("  c. THIS WAS NOT A STOP OR TARGET: it was closed by a decision (or the Friday flatten). Say whether\n"
               "     that close was justified by something the market did, or was a nervous exit. Manual closes have\n"
               "     no paper track record to compare against, which is exactly why each one is reviewed.\n" if manual else
               "  c. Was the exit what the plan said (stop, target)?\n")
            + f"Post your conclusion: {BOARD} post --from review --topic lesson --symbol {sym or 'SYMBOL'} --text \"...\". "
            "Propose a write (a paper) only if this teaches something new; there is no evolve while live. If this\n"
            "trade changes the risk picture (a streak, a halt, sizing that looked wrong), next_demands live_risk with why.")

    if event == "live_day_closed":
        if not (payload.get("traded") or payload.get("journal_notes") or payload.get("graded_n")):
            return None  # an idle day needs no model call
        return "live_review", (
            f"LIVE DAY CLOSED {payload.get('day')}: own realized {payload.get('own_realized')} "
            f"{payload.get('currency', '')}, {payload.get('closed_trades')} closed trade(s), "
            f"{payload.get('orders_placed')} order(s) placed.\n" + live_context(payload) + "\n\n"
            "DAY RECORD:\n" + json.dumps(DS.record(payload, ("snapshot", "graded_text", "graded_n")), indent=1) + "\n\n"
            + (payload["graded_text"] + "\n\n" if payload.get("graded_text") else "") +
            f"Read the day's live record ({LM} ledger -n 60) and answer: a. is a journal note present and honest?\n"
            "b. how much of the daily limit and of the worker's own 1.5% halt was used, and when was the worst\n"
            "moment? c. anything tomorrow's first decision turn must know? Post it to the live board (topic\n"
            "day_review) -- that is what the next turn reads.")

    if event == "live_risk_review":
        return "live_risk", _risk_review(payload)

    if event == "live_risk_warning":
        return "live", (
            f"LIVE RISK WARNING: the account's {payload.get('kind')} loss has used {payload.get('used_pct_of_limit')}% "
            "of its FTMO limit.\n" + live_context(payload) + "\n\n"
            "WARNING RECORD:\n" + json.dumps(DS.record(payload, ("snapshot",)), indent=1) + "\n\n"
            "The loss may be other runners' on this shared account, not yours. The guard already refuses any order\n"
            f"whose worst case would reach the limit. Check {LB} status. You may hold, tighten a stop of YOUR OWN\n"
            "with modify, or close one of yours. Do not add exposure to recover. Journal the decision.")

    if event == "live_dream":
        lessons = payload.get("lessons") or []
        if not lessons:
            return None
        D = f"python3 {HOME}/tools/dreams.py"
        return "live_dream", (
            "DREAMING. Below are one day's process lessons from a trading desk. Turn the ones that hold a "
            "TIMELESS, REUSABLE PROCESS RULE into dreams: short intuitions another trader could test without "
            "knowing where they came from.\n\n"
            "Each dream must be ONE plain rule about process -- entries, stops, targets, sizing, timing within "
            "a session, when to stand aside, how to manage an open trade. It must hold on any market and any "
            "date. So: no dates, years, months or clock times (say 'the US cash open', 'the first hour of "
            "London'); no prices or price levels (small multiples like 1.5 ATR or 2R are fine); no instrument, "
            "ticker or market name; no claim about what any market did (no 'X rallied'); no 'today', 'this "
            "week'; and nothing about where it came from (no 'live', 'real', 'account'). A specific trade is "
            "NOT a dream: the generalisation behind it might be.\n\n"
            f"1. {D} read            the dreams that already exist -- do not repeat one\n"
            f"2. {D} propose --text \"...\"   once per new dream, at most 3; a refusal tells you why\n"
            "If nothing in these lessons generalises, propose nothing -- an honest empty day is better than a "
            "weak dream.\n\n"
            "THE DAY'S LESSONS:\n" + "\n".join(f"- {l}" for l in lessons))

    if event == "live_calibration_review":
        import opportunities as OPP
        return "live_review", (f"LIVE CALIBRATION REVIEW after {payload.get('days')} trading days.\n"
                               + live_context(payload) + "\n\n" + str(payload.get("text", "")) + "\n\n"
                               + OPP.CALIBRATE_TASK)

    if event == "live_level_alert":
        sym = payload.get("symbol", "")
        kind = f"live_trade_{sym.lower().replace('.', '_')}" if sym in LIVE_TRADER_SYMBOLS else "live"
        return kind, (
            f"LIVE PRICE ALERT {payload.get('id')} MET: {payload.get('condition')} -- on the bar ending with the M5 bar "
            f"opening {payload.get('touched_utc')} (the {payload.get('tf') or 'M5'} bar: {json.dumps(payload.get('bar'))}). "
            f"It was set by {payload.get('by')} at {payload.get('set_utc')} with price at {payload.get('ref')}, and this "
            f"was its plan:\n  \"{payload.get('why')}\"\n" + DS.plan_levels(payload) + live_context(payload) + "\n\n"
            + (payload["symbol_file"] + "\n\n" if payload.get("symbol_file") else
               f"(the symbol file could not be built: {payload.get('view_error', '?')} -- read {LM} bars {sym} yourself)\n\n")
            + DS.ALERT_TASK)

    if event == "live_vault_review":
        return "live_vault", (
            f"THE VAULT'S DAILY VALUE EXTRACTION ({payload.get('day')}).\n" + live_context(payload) + "\n\n"
            + (payload.get("vault_text") or f"(the vault could not be built: {payload.get('view_error', '?')})") + "\n\n"
            + DS.VAULT_REVIEW_TASK + "\n"
            f"TOOLS: python3 {HOME}/tools/vault.py report [--symbol S] | python3 {HOME}/tools/vault.py similar --desk live "
            f"--symbol S --side buy|sell [--entry P --sl P] | {BOARD} read --topic vault_lesson -n 20 | {BOARD} post --from "
            "vault --topic vault_lesson [--symbol S] --text \"...\".")

    if event == "live_swing":
        snap = payload.get("snapshot") or {}

        def build(lessons):
            return ("LIVE ONCE-A-DAY SWING TURN (" + str(payload.get("key")) + ").\n\n" + live_context(payload) + "\n\n"
                    f"STATE (live_broker snapshot at {snap.get('utc')}):\n" + json.dumps(snap, indent=1)
                    + "\n\nTHE DAY'S MOVERS (code: first M5 open of the trading day to now; close_in_range 1 = at the day's "
                    "high):\n" + json.dumps(payload.get("movers", []))
                    + "\n\nEVENTS SINCE THE LAST DECISION TURN:\n" + json.dumps(payload.get("recent_events", []), indent=1)
                    + "\n\n" + (payload.get("bars_text") or f"(candles not attached: {payload.get('view_error', '?')})")
                    + "\n\n" + (lessons + "\n\n" if lessons else "") + DS.SWING_TASK + "\n"
                    f"TOOLS: {LB} status | {LB} order --symbol S --side buy|sell --risk-pct P --sl P --tp P --why \"SWING ...\" "
                    f"| {LM} bars SYMBOL -n N --tf D1|H4|H1 | {CONSULT} chart|timing --symbol S --question \"...\" | "
                    f"{LB} journal --text \"...\" | {BOARD} post --from trade --topic swing --text \"VERDICT ...\".")
        return "live", DS.fit_lessons(build, payload.get("lessons_text") or "", f"{BOARD} lessons")

    if event == "live_manage":
        sym = payload.get("symbol", "")
        tk = payload.get("ticket")
        trig = payload.get("triggers") or []
        return "live_manage", (
            f"POSITION MANAGER WAKE: {sym} {payload.get('side')} position {tk} -- "
            + ", ".join(x["code"] for x in trig) + ".\n" + live_context(payload) + "\n\n"
            + MC.describe(payload, payload.get("info") or {}, trig) + "\n\n"
            + (payload["symbol_file"] + "\n\n" if payload.get("symbol_file") else
               f"(the symbol file could not be built: {payload.get('view_error', '?')} -- read {LM} bars {sym} yourself)\n\n")
            + DS.MANAGE_TASK + "\n"
            f"YOUR TOOLS: {LB} status | {LB} close --symbol {sym} {tk} [--volume V] --why \"...\" | {LB} modify --symbol "
            f"{sym} {tk} [--sl P] [--tp P] --why \"...\" | {LB} journal --text \"...\" | {BOARD} post --from manage --topic "
            f"manage --symbol {sym} --text \"MANAGE ...\" | {CONSULT} chart|timing --symbol {sym} --question \"...\" | "
            f"{LM} watch --symbol {sym} {tk} [--above P] [--below P] [--in MIN] [--quiet [--for MIN]] [--clear] --why \"...\" | "
            "next_demands live_risk for a widen (the position id, the symbol, the proposed stop and why).")

    if event in ("live_halt", "live_trade_opened", "live_alert_error", "live_manage_error"):
        return None  # recorded; the next decision turn sees it in its events

    raise ValueError(f"no live route for event {event!r}")
