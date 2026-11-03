"""THE VAULT: every closed trade we have, on every account, with what the market looked like at the entry and what
happened after -- the paperwork the value-extraction worker reads (2026-10-20).

Operator, 2026-10-20: "a worker that is in charge of value extracting ... we give it all the closed trades like
paperwork ... all the trades from all the accounts that we have stored ... it can go back to the vault and see if any
trade that it's taking or the assembly has taken follows some of the patterns ... it also has WHAT happened after ...
candle patterns, timing, zones, gaps, whatever ... and traders can request vault checks". And: "can it see what it is
doing wrong on the entries, and what is optimal to do? ... an algo or worker to see the path to the light ... what's
actually optimal entries FOR the assembly".

Sources (normalised into one row per trade, state/vault/trades.jsonl):
  * every archived FTMO account: deltaForceFTMO data/accounts/*/analysed_trades_with_R.json (runner, R, levels)
  * the current account: the broker's deal history, every runner (paired by position; R from the runners'
    placement log deltaforce/data/ledgers/live_trades.jsonl); this assembly's own trades come from its ledger
  * this assembly: state/live_ledger.jsonl (live, with the plan it was opened on) and state/ledger.jsonl (training)
Bars: the M5 feeds (feed/*_M5.csv) plus a cache of the broker's recent M5 (state/vault/bars/), merged.

Per trade, by code (state/vault/analysed.jsonl):
  f (at the entry, from closed bars before it): session, NY hour, weekday; the hour's move into the entry and whether the
    trade went WITH it (chase) or AGAINST it (fade); where the entry sat in the last 6 hours' M15 range (breakout,
    pullback, mid); H1 and H4 trend agreement; the last M5 candle (pin, marubozu, doji, normal; with or against);
    stop size in M15 ATR, the stop's buffer past the nearest M15 swing against the typical sweep depth there; planned RR
  o (after the entry, up to 24 h): best and worst price in R at 1 h / 4 h / 24 h; +1R / +2R before the stop; for a
    stopped trade, whether price then reached the target (or +1R) within 24 h; the better entry that was available
    within the next hour (slack, in R); the stop that would have survived to the best point (in R)

  python3 tools/vault.py build [--refresh-bars]      gather, analyse what is new, write the report
  python3 tools/vault.py report [--desk live|training] [--source S]
  python3 tools/vault.py similar --desk live|training --symbol S --side buy|sell [--entry P] [--sl P] [--tp P]

The training desk sees the real accounts' trades WITHOUT dates or prices (features and R only), and its own replay
trades only up to the replay's clock -- the vault never shows the replay a market it has not reached.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import glob
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME / "agent_code"))
from guard import rules as R  # noqa: E402

DELTA = Path(os.environ.get("FTMO_BLANK_DELTA_HOME", str(Path.home() / "deltaForceFTMO")))
VAULT = HOME / "state" / "vault"
TRADES = VAULT / "trades.jsonl"
ANALYSED = VAULT / "analysed.jsonl"
REPORT = VAULT / "report.md"
BARS_DIR = VAULT / "bars"
UTC = dt.timezone.utc
NY = None
ASSEMBLY_MAGIC = 777042


def _ny():
    global NY
    if NY is None:
        from zoneinfo import ZoneInfo
        NY = ZoneInfo("America/New_York")
    return NY


def _ts(text) -> float | None:
    """'YYYY-MM-DD HH:MM[:SS]' / ISO / epoch -> UTC epoch seconds."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).replace("T", " ").replace("Z", "").replace(" UTC", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s[:19] if fmt.endswith("%S") else s[:16], fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    return None


def _iso(t: float | None) -> str | None:
    return dt.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%S") if t else None


def _side(x) -> str:
    return "buy" if str(x).lower() in ("buy", "long", "0") else "sell"


def _runner(comment: str) -> str:
    c = str(comment or "").split(":")[0].strip()
    return c or "?"


# ---- sources ------------------------------------------------------------------------------------------
def from_archives() -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(DELTA / "data" / "accounts" / "*" / "analysed_trades_with_R.json"))):
        acct = Path(f).parent.name
        login = acct.split("_")[1] if "_" in acct else acct
        try:
            rows = json.load(open(f))
        except Exception:
            continue
        for r in rows if isinstance(rows, list) else []:
            if "pid" in r:   # the older format
                t_in, t_out = r.get("t_in"), r.get("t_out")
                rec = {"pos": r["pid"], "runner": _runner(r.get("comment") or r.get("tag")), "symbol": r.get("sym"),
                       "side": _side(r.get("side")), "open": t_in, "close": t_out, "entry": r.get("px_in"),
                       "exit": r.get("px_out"), "sl": r.get("sl"), "tp": r.get("tp"), "R": r.get("R"),
                       "net": r.get("profit"), "exit_reason": r.get("out_comment")}
            else:
                if r.get("kind") != "closed":
                    continue
                rec = {"pos": r.get("pos"), "runner": _runner(r.get("entry_comment") or r.get("runner")),
                       "symbol": r.get("symbol"), "side": _side(r.get("dirn")), "open": _ts(r.get("open_utc")),
                       "close": _ts(r.get("close_utc")), "entry": r.get("entry"), "exit": r.get("exit"), "sl": r.get("sl"),
                       "tp": r.get("tp"), "R": r.get("R"), "net": r.get("net"), "exit_reason": r.get("close_comment")}
            if not rec["symbol"] or rec["open"] is None:
                continue
            out.append(_norm(rec, f"account:{login}"))
    return out


def _norm(rec: dict, source: str, plan: str | None = None, sim: bool = False) -> dict:
    er = str(rec.get("exit_reason") or "").lower()
    reason = "sl" if ("sl" in er or er == "4") else "tp" if ("tp" in er or er == "5") else (er or "other")
    o, c = rec.get("open"), rec.get("close")
    return {"id": f"{source}:{rec.get('pos')}", "source": source, "runner": rec.get("runner"), "symbol": rec["symbol"],
            "side": rec["side"], "open_utc": _iso(o), "close_utc": _iso(c), "entry": _f(rec.get("entry")),
            "exit": _f(rec.get("exit")), "sl": _f(rec.get("sl")) or None, "tp": _f(rec.get("tp")) or None,
            "R": _f(rec.get("R")),
            "net": _f(rec.get("net")), "hold_h": round((c - o) / 3600, 2) if (o and c) else None, "exit_reason": reason,
            "plan": plan, "sim": sim}


def _f(x):
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def from_assembly_live(home=HOME) -> list[dict]:
    p = Path(home) / "state" / "live_ledger.jsonl"
    out = []
    for line in p.read_text().splitlines() if p.exists() else []:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") != "trade_closed":
            continue
        rec = {"pos": r.get("ticket"), "runner": "assembly", "symbol": r.get("symbol"), "side": r.get("side"),
               "open": _ts(r.get("opened_utc")), "close": _ts(r.get("closed_utc")), "entry": r.get("entry"),
               "exit": r.get("exit"), "sl": r.get("initial_sl"), "tp": r.get("tp"), "R": r.get("r_net"),
               "net": r.get("net"), "exit_reason": r.get("reason")}
        out.append(_norm(rec, "assembly_live", plan=r.get("why_open")))
    return out


def from_assembly_training(home=HOME) -> list[dict]:
    p = Path(home) / "state" / "ledger.jsonl"
    out = []
    for line in p.read_text().splitlines() if p.exists() else []:
        if '"trade_closed"' not in line:
            continue
        r = json.loads(line)
        rec = {"pos": f"{r.get('attempt')}-{r.get('id')}", "runner": "assembly", "symbol": r.get("symbol"),
               "side": r.get("side"), "open": _ts(r.get("opened_utc")), "close": _ts(r.get("closed_utc")),
               "entry": r.get("entry"), "exit": r.get("exit"), "sl": r.get("initial_sl") or r.get("sl"),
               "tp": r.get("tp"), "R": r.get("r_net"), "net": r.get("net_usd"), "exit_reason": r.get("reason")}
        out.append(_norm(rec, "assembly_training", plan=r.get("why_open"), sim=True))
    return out


def from_current_account(m) -> list[dict]:
    """Every runner's closed trades on the account logged in now, from the broker's deals (read-only). R comes from
    the runners' placement log (their stop at the order); this assembly's own trades are skipped (its ledger has them,
    with the plan)."""
    import live_broker as LB
    ai = m.account_info()
    login = int(ai.login) if ai else 0
    deals = LB._deals_between(m, dt.datetime(2020, 1, 1, tzinfo=UTC))
    place = {}
    lt = DELTA / "deltaforce" / "data" / "ledgers" / "live_trades.jsonl"
    for line in lt.read_text().splitlines() if lt.exists() else []:
        try:
            r = json.loads(line)
            place[str(r.get("ticket"))] = r
        except json.JSONDecodeError:
            continue
    by = defaultdict(list)
    for d in deals:
        pid = int(getattr(d, "position_id", 0) or 0)
        if pid and getattr(d, "symbol", ""):
            by[pid].append(d)
    out = []
    for pid, ds in by.items():
        ins = [d for d in ds if int(d.entry) == 0]
        outs = [d for d in ds if int(d.entry) in (1, 3)]
        if not ins or not outs or int(getattr(ins[0], "magic", 0) or 0) == ASSEMBLY_MAGIC:
            continue
        d0 = ins[0]
        vol = sum(float(d.volume) for d in outs) or 1.0
        exit_px = sum(float(d.price) * float(d.volume) for d in outs) / vol
        side = "buy" if int(d0.type) == 0 else "sell"
        pl = place.get(str(pid)) or {}
        sl, tp = _f(pl.get("sl_price")), _f(pl.get("tp_price"))
        entry = float(d0.price)
        r_mult = None
        if sl and abs(entry - sl) > 0:
            r_mult = ((exit_px - entry) if side == "buy" else (entry - exit_px)) / abs(entry - sl)
        rec = {"pos": pid, "runner": _runner(d0.comment) if d0.comment else pl.get("source", "?"), "symbol": d0.symbol,
               "side": side, "open": R.broker_to_utc(int(d0.time)).timestamp(),
               "close": R.broker_to_utc(int(max(outs, key=lambda d: int(d.time)).time)).timestamp(), "entry": entry,
               "exit": exit_px, "sl": sl, "tp": tp, "R": r_mult,
               "net": sum(float(getattr(d, "profit", 0) or 0) + float(getattr(d, "commission", 0) or 0)
                          + float(getattr(d, "swap", 0) or 0) for d in ds),
               "exit_reason": str(getattr(outs[-1], "comment", "") or getattr(outs[-1], "reason", ""))}
        out.append(_norm(rec, f"account:{login}"))
    return out


# ---- bars ---------------------------------------------------------------------------------------------
class Bars:
    """M5 bars per symbol (UTC epoch, o, h, l, c): the feed, plus the broker's recent cache past the feed's end."""

    def __init__(self, home=HOME):
        self.home = Path(home)
        self._t, self._b = {}, {}

    def _load(self, sym: str):
        if sym in self._t:
            return
        rows = {}
        f = self.home / "feed" / f"{sym}_M5.csv"
        if f.exists():
            with open(f) as fh:
                rd = csv.reader(fh)
                next(rd, None)
                for r in rd:
                    try:
                        t = R.broker_to_utc(int(float(r[0]))).timestamp()
                        rows[t] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                    except (ValueError, IndexError):
                        continue
        c = BARS_DIR / f"{sym}.csv"
        if c.exists():
            with open(c) as fh:
                for line in fh:
                    p = line.strip().split(",")
                    if len(p) == 5:
                        rows[float(p[0])] = tuple(float(x) for x in p[1:])
        ts = sorted(rows)
        self._t[sym], self._b[sym] = ts, [rows[t] for t in ts]

    def window(self, sym: str, t0: float, before_min: int, after_min: int) -> tuple[list, list]:
        """(bars that CLOSED at or before t0, bars that opened at or after t0), each as (t, o, h, l, c)."""
        self._load(sym)
        ts, bs = self._t.get(sym) or [], self._b.get(sym) or []
        i = bisect.bisect_right(ts, t0 - 300)          # bars whose open + 5 min <= t0
        j = bisect.bisect_left(ts, t0)
        lo = bisect.bisect_left(ts, t0 - before_min * 60)
        hi = bisect.bisect_right(ts, t0 + after_min * 60)
        before = [(ts[k], *bs[k]) for k in range(lo, i)]
        after = [(ts[k], *bs[k]) for k in range(j, hi)]
        return before, after


def refresh_bars(m, symbols, days: int = 75):
    """Cache the broker's recent M5 for the symbols (only what the feed does not reach is needed; cheap to redo)."""
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    for sym in sorted(set(symbols)):
        try:
            rates = m.copy_rates_from_pos(sym, m.TIMEFRAME_M5, 0, days * 288)
        except Exception:
            continue
        if rates is None or not len(rates):
            continue
        with open(BARS_DIR / f"{sym}.csv", "w") as f:
            for r in rates:
                t = R.broker_to_utc(int(r["time"])).timestamp()
                f.write(f"{t},{float(r['open'])},{float(r['high'])},{float(r['low'])},{float(r['close'])}\n")


def _resample(bars, minutes: int):
    out, cur, key = [], None, None
    for t, o, h, l, c in bars:
        k = int(t // (minutes * 60))
        if k != key:
            if cur:
                out.append(cur)
            cur, key = [k * minutes * 60, o, h, l, c], k
        else:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
    if cur:
        out.append(cur)
    return [tuple(x) for x in out]


# ---- features and outcomes ----------------------------------------------------------------------------
def session_of(t: float) -> str:
    h = dt.datetime.fromtimestamp(t, UTC).hour
    return "asia" if h < 7 else "london" if h < 12 else "us" if h < 20 else "late"


def sweep_stats(m15) -> tuple[float, float]:
    """Median and max distance wicks ran past an M15 swing before closing back (desk_shared.sweep_depth, numeric)."""
    found = []
    for i in range(2, len(m15) - 2):
        lo, hi = m15[i][3], m15[i][2]
        if lo < min(m15[j][3] for j in (i - 2, i - 1, i + 1, i + 2)):
            for r in m15[i + 3:]:
                if r[3] < lo and r[4] > lo:
                    found.append(lo - r[3]); break
                if r[4] < lo:
                    break
        if hi > max(m15[j][2] for j in (i - 2, i - 1, i + 1, i + 2)):
            for r in m15[i + 3:]:
                if r[2] > hi and r[4] < hi:
                    found.append(r[2] - hi); break
                if r[4] > hi:
                    break
    if not found:
        return 0.0, 0.0
    return statistics.median(found), max(found)


def features(side: str, entry: float, sl: float | None, tp: float | None, t0: float, before) -> dict | None:
    """What the market looked like at the entry, from closed M5 bars before it (both desks, and the live query)."""
    if len(before) < 60:
        return None
    buy = side == "buy"
    sgn = 1 if buy else -1
    m15 = _resample(before[-96 * 3:], 15)
    h1 = _resample(before, 60)
    h4 = _resample(before, 240)
    rng = [b[2] - b[3] for b in m15[-14:]]
    atr = sum(rng) / len(rng) if rng else 0.0
    if atr <= 0:
        return None
    last12 = before[-12:]
    move = (last12[-1][4] - last12[0][1]) / last12[0][1] * 100 if last12[0][1] else 0.0
    rel = "chase" if (move * sgn >= 0.2) else "fade" if (move * sgn <= -0.2) else "flat"
    win = m15[-24:]
    hi, lo = max(b[2] for b in win), min(b[3] for b in win)
    pos = (entry - lo) / (hi - lo) if hi > lo else 0.5
    p_dir = pos if buy else 1 - pos            # 1 = at the extreme in the trade's direction
    loc = "breakout" if p_dir > 1.0 else "with_extreme" if p_dir >= 0.8 else "pullback" if p_dir <= 0.35 else "mid"
    tr = lambda bars: (1 if bars[-1][4] > bars[-min(20, len(bars))][4] else -1) if len(bars) >= 5 else 0
    c = before[-1]
    body, crng = abs(c[4] - c[1]), (c[2] - c[3]) or 1e-12
    wick_against = (min(c[1], c[4]) - c[3]) if buy else (c[2] - max(c[1], c[4]))
    candle = ("pin" if wick_against >= 0.5 * crng else "marubozu" if body >= 0.8 * crng else "doji" if body <= 0.1 * crng
              else "normal")
    cdir = "with" if (c[4] - c[1]) * sgn > 0 else "against"
    f = {"session": session_of(t0), "ny_hour": dt.datetime.fromtimestamp(t0, _ny()).hour,
         "weekday": dt.datetime.fromtimestamp(t0, UTC).weekday(), "move_1h_pct": round(move, 3), "rel": rel,
         "loc": loc, "h1": "with" if tr(h1) == sgn else "against", "h4": "with" if tr(h4) == sgn else "against",
         "candle": candle, "candle_dir": cdir, "atr15": atr}
    if sl:
        stop = abs(entry - sl)
        swing = min(b[3] for b in m15[-6:]) if buy else max(b[2] for b in m15[-6:])
        buffer = (swing - sl) if buy else (sl - swing)
        smed, smax = sweep_stats(m15)
        f.update({"stop_atr": round(stop / atr, 2), "buffer_atr": round(buffer / atr, 2),
                  "sweep_med_atr": round(smed / atr, 2), "sweep_max_atr": round(smax / atr, 2),
                  "stop_vs_sweep": "inside" if buffer < smed else "past_median" if buffer < smax else "past_max"})
        if tp:
            f["rr"] = round(abs(tp - entry) / stop, 2) if stop else None
    return f


def outcome(side: str, entry: float, sl: float | None, tp: float | None, after, close_t: float | None,
            exit_reason: str) -> dict | None:
    if not sl or not after:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    buy = side == "buy"
    t0 = after[0][0]
    o = {}
    for name, mins in (("1h", 60), ("4h", 240), ("24h", 1440)):
        seg = [b for b in after if b[0] < t0 + mins * 60]
        if not seg:
            continue
        fav = (max(b[2] for b in seg) - entry) if buy else (entry - min(b[3] for b in seg))
        adv = (entry - min(b[3] for b in seg)) if buy else (max(b[2] for b in seg) - entry)
        o[f"mfe_{name}"], o[f"mae_{name}"] = round(fav / risk, 2), round(adv / risk, 2)
    first = {}
    for b in after:
        fav = (b[2] - entry) if buy else (entry - b[3])
        adv = (entry - b[3]) if buy else (b[2] - entry)
        if "stop" not in first and adv >= risk:
            first["stop"] = b[0]
        for k in (1, 2):
            if f"{k}r" not in first and fav >= k * risk:
                first[f"{k}r"] = b[0]
    for k in (1, 2):
        o[f"hit_{k}r_before_stop"] = bool(first.get(f"{k}r") and (not first.get("stop") or first[f"{k}r"] < first["stop"]))
    nxt = [b for b in after if b[0] < t0 + 3600]
    if nxt:
        best = min(b[3] for b in nxt) if buy else max(b[2] for b in nxt)
        o["slack_1h_r"] = round(min(1.0, max(0.0, ((entry - best) if buy else (best - entry)) / risk)), 2)
    seg = [b for b in after if b[0] < t0 + 86400]
    if seg:
        peak_i = max(range(len(seg)), key=lambda i: (seg[i][2] if buy else -seg[i][3]))
        pre = seg[:peak_i + 1]
        mae_pre = (entry - min(b[3] for b in pre)) if buy else (max(b[2] for b in pre) - entry)
        o["stop_to_survive_r"] = round(max(0.0, mae_pre) / risk, 2)
    if exit_reason == "sl" and close_t:
        aft = [b for b in after if close_t <= b[0] < close_t + 86400]
        tgt = tp if tp else (entry + risk if buy else entry - risk)
        o["swept_then_target"] = bool(aft and ((max(b[2] for b in aft) >= tgt) if buy else (min(b[3] for b in aft) <= tgt)))
    return o


def analyse(tr: dict, bars: Bars) -> dict:
    t0 = _ts(tr.get("open_utc"))
    out = {**tr, "f": None, "o": None}
    if not t0 or not tr.get("entry"):
        return out
    before, after = bars.window(tr["symbol"], t0, 4 * 24 * 60, 2 * 24 * 60)
    out["f"] = features(tr["side"], tr["entry"], tr.get("sl"), tr.get("tp"), t0, before)
    out["o"] = outcome(tr["side"], tr["entry"], tr.get("sl"), tr.get("tp"), after, _ts(tr.get("close_utc")),
                       tr.get("exit_reason") or "")
    return out


# ---- build --------------------------------------------------------------------------------------------
def gather(m=None, home=HOME) -> list[dict]:
    rows = from_archives() + from_assembly_live(home) + from_assembly_training(home)
    if m is not None:
        try:
            rows += from_current_account(m)
        except Exception as exc:
            print(f"current account not read: {exc}", file=sys.stderr)
    seen, out = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    return out


def build(m=None, refresh: bool = False, home=HOME) -> dict:
    VAULT.mkdir(parents=True, exist_ok=True)
    rows = gather(m, home)
    if m is not None and refresh:
        recent = {r["symbol"] for r in rows if (_ts(r["open_utc"]) or 0) > dt.datetime(2026, 9, 1, tzinfo=UTC).timestamp()}
        refresh_bars(m, recent | set(R.INSTRUMENTS))
    have = {}
    if ANALYSED.exists():
        for line in ANALYSED.read_text().splitlines():
            try:
                a = json.loads(line)
                if a.get("f") is not None:
                    have[a["id"]] = a
            except json.JSONDecodeError:
                continue
    if m is None:   # no broker this time (a training rebuild): keep the current account's trades read last time
        ids = {r["id"] for r in rows}
        rows += [{k: v for k, v in a.items() if k not in ("f", "o")} for i, a in have.items()
                 if i not in ids and str(a.get("source", "")).startswith("account:")]
    bars = Bars(home)
    analysed = [have.get(r["id"]) or analyse(r, bars) for r in rows]
    TRADES.write_text("".join(json.dumps(r) + "\n" for r in rows))
    ANALYSED.write_text("".join(json.dumps(a, default=float) + "\n" for a in analysed))
    REPORT.write_text(report(analysed, "live"))
    return {"trades": len(rows), "analysed": sum(1 for a in analysed if a.get("f")),
            "sources": dict(sorted(_count(rows, "source").items()))}


def _count(rows, key):
    c = defaultdict(int)
    for r in rows:
        c[r.get(key)] += 1
    return c


def load(desk: str = "live", now_sim: float | None = None) -> list[dict]:
    """The analysed vault as a desk may see it: training gets real trades without dates/prices, and its own replay trades
    only up to the replay's clock."""
    if not ANALYSED.exists():
        return []
    out = []
    for line in ANALYSED.read_text().splitlines():
        try:
            a = json.loads(line)
        except json.JSONDecodeError:
            continue
        if desk == "training":
            if a.get("sim"):
                if now_sim is not None and (_ts(a.get("close_utc")) or 0) > now_sim:
                    continue
            else:
                a = {k: v for k, v in a.items() if k not in ("open_utc", "close_utc", "entry", "exit", "sl", "tp", "net",
                                                              "plan")}
                if a.get("f"):
                    a["f"] = {k: v for k, v in a["f"].items() if k != "atr15"}
        out.append(a)
    return out


# ---- the report ---------------------------------------------------------------------------------------
def _stat(rows) -> str:
    rs = [r["R"] for r in rows if isinstance(r.get("R"), (int, float))]
    if not rs:
        return "n=0"
    wr = sum(1 for x in rs if x > 0) / len(rs)
    return f"n={len(rs)} WR {wr:.0%} mean {sum(rs) / len(rs):+.2f}R total {sum(rs):+.1f}R"


def _table(rows, key, title, min_n=5) -> list[str]:
    g = defaultdict(list)
    for r in rows:
        v = key(r)
        if v is not None:
            g[v].append(r)
    lines = [f"  {title}:"]
    for v, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        if len(rs) >= min_n:
            lines.append(f"    {str(v):22s} {_stat(rs)}")
    return lines if len(lines) > 1 else []


def report(rows: list[dict], desk: str = "live", focus: str | None = None) -> str:
    """The paperwork, summarised by code: who traded what and how it went, which entry conditions paid across every
    runner, and the assembly's own entry diagnosis (what went wrong at the entry, what the better entry was)."""
    rows = [r for r in rows if isinstance(r.get("R"), (int, float))]
    fx = [r for r in rows if r.get("f")]
    lines = [f"THE VAULT (code-built; {len(rows)} closed trades with R, {len(fx)} with the entry's market read)."]
    lines += _table(rows, lambda r: r.get("runner"), "by runner (all accounts, assembly = this desk)", 3)
    for name, key in (("trade vs the last hour's move", lambda r: r["f"]["rel"]),
                      ("where the entry sat in the last 6 h of M15", lambda r: r["f"]["loc"]),
                      ("H1 trend", lambda r: r["f"]["h1"]), ("H4 trend", lambda r: r["f"]["h4"]),
                      ("session (UTC)", lambda r: r["f"]["session"]),
                      ("entry candle", lambda r: f"{r['f']['candle']}/{r['f']['candle_dir']}"),
                      ("stop vs the sweep depth", lambda r: r["f"].get("stop_vs_sweep")),
                      ("planned RR", lambda r: None if r["f"].get("rr") is None else
                       ("<1.2" if r["f"]["rr"] < 1.2 else "1.2-1.8" if r["f"]["rr"] < 1.8 else "1.8-2.5" if r["f"]["rr"] < 2.5 else ">=2.5"))):
        lines += ["ALL RUNNERS -- " + name + ":"] + _table(fx, key, name, 8)[1:]
    combo = defaultdict(list)
    for r in fx:
        f = r["f"]
        for a, b in (("rel", "loc"), ("loc", "h4"), ("rel", "h4"), ("loc", "stop_vs_sweep"), ("session", "loc")):
            combo[f"{a}={f.get(a)} & {b}={f.get(b)}"].append(r)
    ranked = [(k, v) for k, v in combo.items() if len(v) >= 10]
    ranked.sort(key=lambda kv: -sum(x["R"] for x in kv[1]) / len(kv[1]))
    if ranked:
        lines.append("BEST AND WORST ENTRY COMBINATIONS (n >= 10, all runners):")
        lines += [f"    {k:44s} {_stat(v)}" for k, v in ranked[:6]]
        lines += ["    ..."] + [f"    {k:44s} {_stat(v)}" for k, v in ranked[-6:]]
    mine = [r for r in fx if r.get("runner") == "assembly"]
    if mine:
        lines.append(f"THE ASSEMBLY'S OWN ENTRIES ({len(mine)}: live and training) -- what went wrong, what was better:")
        lines += _table(mine, lambda r: r["f"]["rel"], "vs the last hour's move", 2)
        lines += _table(mine, lambda r: r["f"]["loc"], "entry location", 2)
        lines += _table(mine, lambda r: r["f"].get("stop_vs_sweep"), "stop vs the sweep depth", 2)
        lines += _table(mine, lambda r: r["f"]["h4"], "H4 trend", 2)
        o = [r for r in mine if r.get("o")]
        losers = [r for r in o if r["R"] <= -0.5]
        swept = [r for r in losers if r["o"].get("swept_then_target")]
        slack = [r["o"]["slack_1h_r"] for r in o if r["o"].get("slack_1h_r") is not None]
        need = [r["o"]["stop_to_survive_r"] for r in o if r["o"].get("stop_to_survive_r") is not None]
        if o:
            lines.append(f"  losers then reached the target (or +1R) within 24 h: {len(swept)}/{len(losers)}")
            if slack:
                lines.append(f"  a better entry within the next hour: median {statistics.median(slack):.2f}R, "
                             f"{sum(1 for x in slack if x >= 0.5)}/{len(slack)} entries had >= 0.5R better within the hour")
            if need:
                lines.append(f"  the stop that would have survived to the best point: median {statistics.median(need):.2f}R "
                             f"of the stop used ({sum(1 for x in need if x > 1)}/{len(need)} needed MORE than the stop used)")
            hit1 = sum(1 for r in o if r["o"].get("hit_1r_before_stop"))
            lines.append(f"  reached +1R before the stop: {hit1}/{len(o)}; banked at least +0.5R: "
                         f"{sum(1 for r in o if r['R'] >= 0.5)}/{len(o)}")
    if focus:
        lines.append(f"FOCUS {focus}: " + _stat([r for r in rows if r.get("symbol") == focus]))
    return "\n".join(lines)


# ---- the query ------------------------------------------------------------------------------------------
KEYS = ("rel", "loc", "h1", "h4", "session", "candle", "stop_vs_sweep")


def similar(f_now: dict, symbol: str, side: str, rows: list[dict], k: int = 8, desk: str = "live") -> str:
    """Past trades whose entry looked like this one, most alike first, and what happened after them."""
    scored = []
    for r in rows:
        f = r.get("f")
        if not f or not isinstance(r.get("R"), (int, float)):
            continue
        s = sum(1 for key in KEYS if f.get(key) == f_now.get(key)) + (1 if r.get("symbol") == symbol else 0)
        scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    close = [r for s, r in scored if s >= 5]
    lines = [f"VAULT CHECK {symbol} {side}: this entry reads as " + ", ".join(f"{k}={f_now.get(k)}" for k in KEYS),
             f"  look-alikes (>= 5 of 8 traits the same): {_stat(close)}"]
    for key in ("rel", "loc", "h4", "stop_vs_sweep"):
        same = [r for s, r in scored if r["f"].get(key) == f_now.get(key)]
        lines.append(f"  everything with {key}={f_now.get(key)}: {_stat(same)}")
    lines.append("  the closest ones (what happened after: best/worst in R at 4 h, swept-then-target):")
    for s, r in scored[:k]:
        o = r.get("o") or {}
        when = "" if desk == "training" and not r.get("sim") else f"{(r.get('open_utc') or '')[:16]} "
        lines.append(f"    [{s}/8] {when}{r.get('symbol')} {r.get('side')} {r.get('runner')}: R {r['R']:+.2f}; best "
                     f"{o.get('mfe_4h')}R worst {o.get('mae_4h')}R in 4 h; +1R first {o.get('hit_1r_before_stop')}; "
                     f"swept-then-target {o.get('swept_then_target')}")
    return "\n".join(lines)


def features_now(desk: str, symbol: str, side: str, entry: float | None, sl: float | None, tp: float | None,
                 m=None, paper=None) -> dict | None:
    """The same entry read for a trade being decided NOW (live: broker bars; training: the replay's bars)."""
    if desk == "live":
        import live_broker as LB
        import live_market as LM
        m = m or LB._mt5_client()
        rows = LM.m5_history(symbol, 4 * 288, m=m)
        now = LB._now_utc().timestamp()
    else:
        import paper as P
        paper = paper or P.Paper()
        rows = paper.recent_bars(symbol, 4 * 288, "M5")
        now = (P.bar_utc(paper.t_now()) + dt.timedelta(minutes=5)).timestamp()
    before = [(_ts(r["utc"]), r["o"], r["h"], r["l"], r["c"]) for r in rows if "error" not in r]
    if not before:
        return None
    entry = entry or before[-1][4]
    return features(side, entry, sl, tp, now, before)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vault.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--refresh-bars", action="store_true")
    b.add_argument("--no-broker", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--desk", choices=["live", "training"], default="live")
    r.add_argument("--symbol")
    s = sub.add_parser("similar")
    s.add_argument("--desk", choices=["live", "training"], required=True)
    s.add_argument("--symbol", required=True)
    s.add_argument("--side", choices=["buy", "sell"], required=True)
    s.add_argument("--entry", type=float)
    s.add_argument("--sl", type=float)
    s.add_argument("--tp", type=float)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        m = None
        if not a.no_broker:
            try:
                import live_broker as LB
                m = LB._mt5_client()
            except Exception as exc:
                print(f"no broker: {exc}", file=sys.stderr)
        print(json.dumps(build(m, a.refresh_bars), indent=1))
    elif a.cmd == "report":
        now_sim = None
        if a.desk == "training":
            import kstore as K
            t = K.now_utc(HOME)
            now_sim = t.timestamp() if t else None
        print(report(load(a.desk, now_sim), a.desk, a.symbol))
    elif a.cmd == "similar":
        f = features_now(a.desk, a.symbol, a.side, a.entry, a.sl, a.tp)
        if not f:
            print("VAULT CHECK: no bars to read this entry")
            return 0
        now_sim = None
        if a.desk == "training":
            import kstore as K
            t = K.now_utc(HOME)
            now_sim = t.timestamp() if t else None
        print(similar(f, a.symbol, a.side, load(a.desk, now_sim), desk=a.desk))
    return 0


if __name__ == "__main__":
    sys.exit(main())
