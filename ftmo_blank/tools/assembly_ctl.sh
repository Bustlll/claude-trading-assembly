#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  ASSEMBLY CONTROL — bring the ftmo_blank assembly up or down IN THE STATE IT WAS LEFT IN.
#  Called by deltaForceFTMO/toboot/up.sh and down.sh (operator, 2026-10-11: "the assembly starts
#  with toboot up and down ... it should come UP, but paused or off if that was the last option").
#
#      tools/assembly_ctl.sh up [--dry]   dashboard + watchdog, then the service the master switch
#                                         selects (TRAINING -> ftmo-field, LIVE -> ftmo-live, OFF ->
#                                         none) unless the dashboard left it paused or killed
#      tools/assembly_ctl.sh down         stop all of it (dashboard, watchdog, field, live)
#      tools/assembly_ctl.sh status       one line per piece
#
#  Never changes state/MODE, MANUAL_PAUSE, KILLED or LIVE_ARMED: those are the operator's, set from
#  the dashboard. --dry (toboot's "start everything disarmed") leaves a LIVE assembly's loop down.
# ══════════════════════════════════════════════════════════════════════════════
set -u
HOME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ST="$HOME_DIR/state"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
say() { echo "  $*"; }
FAIL=0   # any piece that should be running but is not -> exit 1, so toboot's report shows it
unit_up()   { systemctl --user is-active --quiet "$1"; }
start_u()   { systemctl --user cat "$1" >/dev/null 2>&1 || { say "✗ $1 not installed"; return 1; }
              unit_up "$1" || systemctl --user start "$1" 2>/dev/null; sleep 1
              if unit_up "$1"; then say "✓ $1 running"; else say "✗ $1 NOT running (systemctl --user status $1)"; FAIL=1; fi; }
stop_u()    { if unit_up "$1"; then systemctl --user stop "$1" 2>/dev/null
                if unit_up "$1"; then say "✗ $1 STILL RUNNING"; FAIL=1; else say "✓ $1 stopped"; fi
              else say "· $1 already stopped"; fi; }
mode()      { local m; m="$(tr -d '[:space:]' < "$ST/MODE" 2>/dev/null)"
              case "$m" in OFF|TRAINING|LIVE) echo "$m" ;; *) echo TRAINING ;; esac; }
# a monitor started by hand before the unit existed would hold :8766 -- stop it by PID (never pkill -f)
hand_monitor_pids() { pgrep -f "tools/monitor_ui.py --port 8766" 2>/dev/null | while read -r p; do
                        [[ "$(systemctl --user show -p MainPID --value ftmo-monitor.service 2>/dev/null)" == "$p" ]] || echo "$p"; done; }

cmd="${1:-status}"; DRY=0; [[ "${2:-}" == "--dry" ]] && DRY=1
case "$cmd" in
up)
    for p in $(hand_monitor_pids); do kill "$p" 2>/dev/null; done
    start_u ftmo-monitor.service
    start_u ftmo-live-watchdog.service
    M="$(mode)"
    if   [[ -e "$ST/KILLED" ]];       then say "· mode $M, but KILLED from the dashboard: nothing started (operator restarts it)"
    elif [[ -e "$ST/MANUAL_PAUSE" ]]; then say "· mode $M, PAUSED from the dashboard: left paused (Resume on :8766)"
    elif [[ "$M" == "TRAINING" ]];    then start_u ftmo-field.service
    elif [[ "$M" == "LIVE" ]]; then
        if [[ $DRY -eq 1 ]]; then say "· mode LIVE but toboot --dry: live loop NOT started"
        elif ! ss -tln 2>/dev/null | grep -q "127.0.0.1:8001"; then
            say "✗ mode LIVE but the MT5 bridge :8001 is down: live loop NOT started"; FAIL=1
        else start_u ftmo-live.service; fi
    else say "· mode OFF: nothing to run (switch on :8766)"; fi
    ;;
down)
    for u in ftmo-live.service ftmo-field.service ftmo-live-watchdog.service ftmo-monitor.service; do stop_u "$u"; done
    for p in $(hand_monitor_pids); do kill "$p" 2>/dev/null && say "✓ hand-started dashboard (pid $p) stopped"; done
    ;;
status)
    say "mode $(mode)$([[ -e $ST/MANUAL_PAUSE ]] && echo ', PAUSED')$([[ -e $ST/KILLED ]] && echo ', KILLED')$([[ -e $ST/LIVE_ARMED ]] && echo ', LIVE_ARMED')"
    for u in ftmo-monitor.service ftmo-live-watchdog.service ftmo-field.service ftmo-live.service; do
        say "$(unit_up "$u" && echo '●' || echo '○') $u"; done
    ;;
*) echo "usage: $0 up [--dry] | down | status"; exit 2 ;;
esac
exit $FAIL
