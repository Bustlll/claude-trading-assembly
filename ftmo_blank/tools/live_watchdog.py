"""LIVE WATCHDOG: the clock-driven safety rails that cannot wait for the Claude-driven 'live'
kind to happen to run a turn -- Friday flatten and the rollover SL guard, both scoped to ONLY
this tool's own positions (filtered by MAGIC, see tools/live_broker.py). Mirrors deltaForceFTMO's
own proven flatten_daemon.py pattern: a small standalone process against the same shared MT5
bridge, polling its own clock, independent of any trading decision -- these two rules act on
EXISTING positions proactively, which a per-order gate inside live_broker.py's place_order()
cannot do (a gate only ever fires when a NEW order is proposed).

  python3 tools/live_watchdog.py [--poll-s 30] [--dry-run]

Polls adaptively, not on one fixed cadence: neither rule can fire outside its own narrow window
(Friday cutoff, or any day's rollover hour), so idling at a coarse interval (IDLE_POLL_S, 15 min)
almost all week and only switching to the tight --poll-s cadence within LEAD_MINUTES (60) of
either window is a straight resource saving with no coverage gap -- the idle interval is kept
well under the lead window so the switch itself is never missed.

Runs regardless of state/LIVE_ARMED: arming governs whether a NEW order can be placed
(live_broker.py's own gate), not whether an already-open real position still gets protected --
disarming the worker must never mean its open positions go unmanaged over a weekend.

NOT enforced here on purpose (operator's explicit call, 2026-10-10): a night-trading block, and
a max-hold/time-stop (both need fill data this worker does not have yet)."""
from __future__ import annotations
import argparse
import datetime as dt
from zoneinfo import ZoneInfo
import os
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "tools"))
import live_broker as LB  # noqa: E402
from guard import rules as R  # noqa: E402

ROLLOVER_STATE_PATH = HOME / "state" / "live_rollover.json"
# Matches deltaForceFTMO's own validated goldtrend_live.py cutoff (FRI_FLAT_H) and its
# ROLLOVER_HOURS_UTC default -- no stated reason to differ, so reused rather than re-guessed.
FRI_FLAT_HOUR_UTC = LB.FRI_FLAT_HOUR_UTC   # one constant: live_broker.py refuses new entries from the same hour
_ROLLOVER_HOUR_OVERRIDE = os.environ.get("FTMO_BLANK_ROLLOVER_HOUR_UTC")
_NY = ZoneInfo("America/New_York")


def rollover_hour_utc(now: dt.datetime | None = None) -> int:
    """The daily rollover = 17:00 New York (the broker's midnight): 21:00 UTC in US summer, 22:00 UTC in
    US winter (2026-10-13; the fixed 21 was summer-only -- checklist D11). FTMO_BLANK_ROLLOVER_HOUR_UTC,
    when set, pins it."""
    if _ROLLOVER_HOUR_OVERRIDE:
        return int(_ROLLOVER_HOUR_OVERRIDE)
    now = now or dt.datetime.now(dt.timezone.utc)
    ny = dt.datetime(now.year, now.month, now.day, 17, 0, tzinfo=_NY)
    return ny.astimezone(dt.timezone.utc).hour


def rollover_text(now: dt.datetime | None = None) -> str:
    """Today's rollover in the operator's clock AND in UTC. Verified 2026-10-13 against FOMC / ECB / CPI
    spikes in the broker's own bars: the broker clock follows NEW YORK daylight saving (FOMC 2025-10-29
    18:00 UTC sat at broker 21:00 while Madrid was already on winter time), so the swap is 17:00 New York:
    23:00 Madrid most of the year, 22:00 Madrid in the late-October and March weeks when the US and
    European clocks change on different dates. Anchoring it to 23:00 Madrid would miss it in those weeks."""
    now = now or dt.datetime.now(dt.timezone.utc)
    h = rollover_hour_utc(now)
    at = now.replace(hour=h, minute=0, second=0, microsecond=0)
    return f"{at.astimezone(ZoneInfo('Europe/Madrid')):%H:%M} Madrid = {h:02d}:00 UTC"


ROLLOVER_HOUR_UTC = rollover_hour_utc()   # today's, for the start-up line and back-compat
# Restore (operator, 2026-10-13: "until they un-widen, if possible; if not or not precise, just do timing"):
# after the rollover hour, a widened stop goes back once the LIVE spread has been normal -- at most
# RESTORE_SPREAD_MULT x the instrument's typical spread -- on RESTORE_NORMAL_POLLS polls in a row; and in
# any case ROLLOVER_HOLD_MAX_MIN after the rollover began (also when no typical spread is known).
RESTORE_SPREAD_MULT = float(os.environ.get("FTMO_BLANK_ROLLOVER_RESTORE_SPREAD_MULT", "2.0"))
RESTORE_NORMAL_POLLS = int(os.environ.get("FTMO_BLANK_ROLLOVER_RESTORE_NORMAL_POLLS", "2"))
ROLLOVER_HOLD_MAX_MIN = float(os.environ.get("FTMO_BLANK_ROLLOVER_HOLD_MAX_MIN", "120"))
MEASURED_SPREADS_PATH = HOME / "knowledge" / "measured_spreads.json"
# This tool's own cushion, built from guard/instruments.json's own spread snapshot (ftmo_blank's
# own data) rather than deltaForceFTMO's measured ROLLOVER_SPREAD_PIPS table -- the two projects
# stay separate on purpose (manual/LIVE_TRADING_IDEA.md).
ROLLOVER_WIDEN_MULT = float(os.environ.get("FTMO_BLANK_ROLLOVER_WIDEN_MULT", "2.0"))
# Adaptive polling (operator's explicit call, 2026-10-10): neither clock-driven rule can fire
# outside its own narrow window, so polling every --poll-s around the clock all week wastes a
# broker round-trip for nothing almost all the time. Idle most of the week at IDLE_POLL_S; switch
# to the tight --poll-s cadence only once within LEAD_MINUTES of the Friday cutoff or of ANY day's
# rollover hour (IDLE_POLL_S is kept well under LEAD_MINUTES so the switch itself is never missed).
IDLE_POLL_S = float(os.environ.get("FTMO_BLANK_WATCHDOG_IDLE_POLL_S", "900"))       # 15 min
LEAD_MINUTES = float(os.environ.get("FTMO_BLANK_WATCHDOG_LEAD_MINUTES", "60"))


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _friday_flatten_due(now: dt.datetime | None = None) -> bool:
    now = now or _now_utc()
    if now.weekday() == 4 and now.hour >= FRI_FLAT_HOUR_UTC:   # Friday, past the cutoff
        return True
    return now.weekday() == 5   # Saturday: the whole dead window, re-swept in case Friday failed


def _seconds_until_next_hour_utc(now: dt.datetime, hour: int, weekday: int | None = None) -> float:
    """Seconds until the clock next reads `hour`:00 UTC, optionally restricted to one weekday
    (0=Monday). Always strictly in the future (today/this-week's occurrence if still ahead, next
    week's/tomorrow's otherwise)."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if weekday is not None:
        candidate += dt.timedelta(days=(weekday - now.weekday()) % 7)
        if candidate <= now:
            candidate += dt.timedelta(days=7)
    elif candidate <= now:
        candidate += dt.timedelta(days=1)
    return (candidate - now).total_seconds()


def _seconds_until_next_critical_window(now: dt.datetime) -> float:
    """0.0 if a rule could fire RIGHT NOW (already in the Friday-flatten window, or the clock is
    already at the rollover hour); otherwise seconds until whichever comes first."""
    hour = rollover_hour_utc(now)
    if _friday_flatten_due(now) or now.hour == hour:
        return 0.0
    return min(_seconds_until_next_hour_utc(now, FRI_FLAT_HOUR_UTC, weekday=4),
               _seconds_until_next_hour_utc(now, hour))


def _set_sl(m, ticket: int, symbol: str, new_sl: float) -> bool:
    """A raw, UNGATED stop move -- deliberately not live_broker.py's modify_stop(), whose
    guard/rules.py modify_check() refuses any widening on purpose. The rollover guard's entire
    job is to widen temporarily (and then restore), the one legitimate exception to that rule."""
    try:
        # 2026-10-19: an SLTP request REPLACES both levels -- sent without "tp" it set the target to 0, so every
        # short held over a rollover lost its target when the cushion went on (the ETHUSD short, 21:05 UTC). The
        # position's own current target rides along; if the position cannot be read, nothing is sent.
        cur = m.positions_get(ticket=ticket)
        if not cur:
            return False
        tp = float(getattr(cur[0], "tp", 0) or 0)
        req = {"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol, "sl": float(new_sl), "tp": tp}
        res = m.order_send(req)
        return res is not None and LB._retcode_ok(m, res)
    except Exception:
        return False


def _close_one(m, p) -> bool:
    try:
        tick = m.symbol_info_tick(p.symbol)
        si = m.symbol_info(p.symbol)
        buy = int(p.type) == 0
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": float(p.volume),
               "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY, "position": int(p.ticket),
               "price": float(tick.bid if buy else tick.ask), "deviation": LB.DEVIATION_POINTS,
               "magic": LB.MAGIC, "comment": "ftmo_blank_fri_flat"[:31], "type_time": m.ORDER_TIME_GTC,
               "type_filling": LB._filling_mode(m, si)}
        res = m.order_send(req)
        if res is None:   # one stale-RPC retry, same convention as deltaForceFTMO's flatten_daemon
            res = m.order_send(req)
        return res is not None and LB._retcode_ok(m, res)
    except Exception as exc:
        LB.log("friday_flatten_close_failed", ticket=getattr(p, "ticket", None),
               symbol=getattr(p, "symbol", None), exc=str(exc))
        return False


def _friday_flatten_pass(m, dry_run: bool) -> None:
    try:
        positions = LB._own_positions_raw(m)
    except Exception as exc:
        LB.log("friday_flatten_error", msg=f"positions read failed: {exc}")
        return
    for p in positions:
        if dry_run:
            print(f"[watchdog][DRY] would flatten {p.symbol} {p.volume} ticket={p.ticket}", flush=True)
            continue
        ok = _close_one(m, p)
        print(f"[watchdog] friday-flatten {p.symbol} ticket={p.ticket} -> {'OK' if ok else 'FAILED'}", flush=True)
        LB.log("friday_flatten_close" if ok else "friday_flatten_close_failed", ticket=p.ticket, symbol=p.symbol)


def _rollover_pass(m, dry_run: bool) -> None:
    """Widens THIS tool's own SHORT positions' stop during the rollover hour (shorts trigger on
    the ask, which spikes at rollover; longs trigger on the bid and are left alone), restoring
    once the hour passes. Two safety rules carried over from deltaForceFTMO's own documented
    2026-08-30 incident with this exact pattern: (1) a falsy positions read is indistinguishable
    here from a dropped RPC, so it changes NOTHING -- no eviction, state left exactly as is; a
    stale entry only costs one wasted restore attempt later, while a wrongly-evicted one costs an
    unbounded stop ratchet the next time the ticket is (re-)widened from an already-wide base.
    (2) eviction only happens inside the per-ticket loop below, for a ticket missing from a
    genuinely NON-EMPTY read."""
    try:
        positions = LB._own_positions_raw(m)
    except Exception as exc:
        LB.log("rollover_guard_error", msg=f"positions read failed: {exc}")
        return
    state = LB._read_json(ROLLOVER_STATE_PATH, {})
    if not positions:
        # a GENUINE empty read (a failed one raised above): nothing of ours is open, so nothing can be
        # widened -- clear leftovers, or they would hold the watchdog on its tight poll for nothing
        if state:
            LB._write_json_atomic(ROLLOVER_STATE_PATH, {})
        return
    now = _now_utc()
    hour = rollover_hour_utc(now)
    in_window = (now.hour == hour)
    rollover_start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if rollover_start > now:
        rollover_start -= dt.timedelta(days=1)
    measured = LB._read_json(MEASURED_SPREADS_PATH, {})
    live_tickets = set()
    dirty = False
    for p in positions:
        tk = str(p.ticket)
        live_tickets.add(tk)
        if int(p.type) == 0:   # long -- not this guard's concern
            continue
        cur_sl = float(getattr(p, "sl", 0) or 0)
        if cur_sl <= 0:
            continue
        try:
            spec = R.spec(p.symbol)
        except KeyError:
            continue
        rec = state.get(tk)
        typical = float(spec.get("spread_price_snapshot") or 0.0)
        live_spread = None
        try:
            t = m.symbol_info_tick(p.symbol)
            if t is not None:
                live_spread = max(0.0, float(t.ask) - float(t.bid))
        except Exception:
            pass
        if in_window and not rec:
            # 2026-10-13: the cushion was 2 x the table spread, and 11 instruments had 0 there -- USDMXN's
            # "widen" was 18.285 -> 18.285, and a post-rollover spike took the stop while the bid never
            # reached it. Now: the WORST spread measured in the hours after the rollover for this
            # instrument (knowledge/measured_spreads.json; bar spreads understate intra-bar peaks, so the
            # max, not a percentile), or 2 x the typical / the live spread if larger. Never a widen of 0.
            spike = float((measured.get(p.symbol) or {}).get("rollover_max") or 0.0)
            cushion = max(spike, ROLLOVER_WIDEN_MULT * typical, ROLLOVER_WIDEN_MULT * (live_spread or 0.0))
            if cushion <= 0:
                LB.log("rollover_guard_error", ticket=tk, symbol=p.symbol,
                       msg="no spread to size the widen (table 0, no live quote): stop left as is")
                continue
            target = cur_sl + cushion   # a SHORT stop sits above price; moving it up widens it
            if dry_run:
                print(f"[watchdog][DRY] would widen {p.symbol} ticket={tk} sl {cur_sl:.5f}->{target:.5f}", flush=True)
                continue
            if _set_sl(m, int(p.ticket), p.symbol, target):
                state[tk] = {"orig": cur_sl, "wide": target, "at": now.timestamp(), "typical": typical,
                             "normal_polls": 0}
                dirty = True
                LB.log("rollover_sl_widen", ticket=tk, symbol=p.symbol, orig=cur_sl, wide=target,
                       rollover=rollover_text(now),
                       cushion=round(cushion, 8), typical_spread=typical, live_spread=live_spread)
                print(f"[watchdog] widened {p.symbol} ticket={tk} sl {cur_sl:.5f}->{target:.5f}", flush=True)
        elif (not in_window) and rec:
            pip = float(spec.get("tick_size", 0.0001))
            if abs(cur_sl - float(rec["wide"])) <= pip * 2:   # nothing else moved it meanwhile
                typ = float(rec.get("typical") or typical)
                normal = bool(typ > 0 and live_spread is not None and live_spread <= RESTORE_SPREAD_MULT * typ)
                rec["normal_polls"] = (int(rec.get("normal_polls") or 0) + 1) if normal else 0
                dirty = True
                timed_out = now >= rollover_start + dt.timedelta(minutes=ROLLOVER_HOLD_MAX_MIN)
                if rec["normal_polls"] < RESTORE_NORMAL_POLLS and not timed_out:
                    continue   # the spread has not settled yet: keep the cushion
                why = ("spread back to normal" if rec["normal_polls"] >= RESTORE_NORMAL_POLLS
                       else f"time cap ({ROLLOVER_HOLD_MAX_MIN:g} min after the rollover)")
                if dry_run:
                    print(f"[watchdog][DRY] would restore {p.symbol} ticket={tk} sl -> {rec['orig']} ({why})", flush=True)
                    continue
                if _set_sl(m, int(p.ticket), p.symbol, float(rec["orig"])):
                    LB.log("rollover_sl_restore", ticket=tk, symbol=p.symbol, to=rec["orig"], why=why,
                           live_spread=live_spread, typical_spread=typ)
                    print(f"[watchdog] restored {p.symbol} ticket={tk} sl -> {rec['orig']} ({why})", flush=True)
                    state.pop(tk, None); dirty = True
            else:
                state.pop(tk, None); dirty = True   # something else now owns this stop
    for tk in list(state.keys()):
        if tk not in live_tickets:
            state.pop(tk, None); dirty = True
    if dirty:
        LB._write_json_atomic(ROLLOVER_STATE_PATH, state)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--poll-s", type=float, default=30.0, help="tight cadence, used only within "
                    "--lead-minutes of a critical window")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    print(f"[watchdog] up. magic={LB.MAGIC} friday-flatten>={FRI_FLAT_HOUR_UTC}:00 UTC Fri "
          f"(through Saturday), rollover today {rollover_text()} (17:00 New York), "
          f"restore when the spread is back to <= {RESTORE_SPREAD_MULT:g}x typical or {ROLLOVER_HOLD_MAX_MIN:g} min -- idle poll "
          f"{IDLE_POLL_S:g}s, tight poll {a.poll_s:g}s within {LEAD_MINUTES:g}min of either "
          f"({'DRY-RUN' if a.dry_run else 'LIVE'})", flush=True)
    m = None
    while True:
        try:
            if m is None:
                m = LB._mt5_client()
            now = _now_utc()
            if _friday_flatten_due(now):
                _friday_flatten_pass(m, a.dry_run)
            _rollover_pass(m, a.dry_run)
        except Exception as exc:
            print(f"[watchdog] pass failed, reconnecting next poll: {exc}", flush=True)
            m = None
        tight = (_seconds_until_next_critical_window(_now_utc()) <= LEAD_MINUTES * 60
                 or bool(LB._read_json(ROLLOVER_STATE_PATH, {})))   # a widened stop is watched until restored
        time.sleep(a.poll_s if tight else IDLE_POLL_S)


if __name__ == "__main__":
    sys.exit(main())
