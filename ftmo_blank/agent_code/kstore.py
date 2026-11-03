"""KNOWLEDGE STORE: recall a fact, a lesson or a past trade in one command.

No index on disk: every call reads the sources fresh (field/, knowledge/, papers/, manual/, state/ledger.jsonl),
so it can never be stale or out of sync. The sources are small; a full read takes milliseconds.

  kstore.py                                   what is indexed, and how much
  kstore.py spec SYMBOL                       spec, feed span and caveats of one symbol (any case, prefix ok)
  kstore.py trades [--result loss|win] [--symbol S] [-n 10]
                                              last N closed trades, then what they share (counts and mean R)
  kstore.py day YYYY-MM-DD                    the journal and every ledger event of one Prague day, papers naming it
  kstore.py find WORD... [--in SOURCE] [-n 8] sections ranked by how many of the words they contain
                                              SOURCE: field, knowledge, papers, manual, ledger

It holds no opinion: only what the sources say and numbers measured from the ledger.
"""
import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
DAY_TZ = "Europe/Prague"  # guard/rules.py DAY_TZ: the FTMO day turns at Prague midnight
SOURCES = ("field", "papers", "manual", "knowledge", "ledger")
STOP = set("the and for was with this that from not are but has had have its into then than when what "
           "why which will would there their they them been were also only very just".split())
TRADE_KEYS = ("_line", "id", "symbol", "side", "lots", "entry", "exit", "sl", "tp", "reason", "r_net", "net_usd",
              "risk_usd", "opened_utc", "closed_utc", "why_open", "close_note")


# ---- sources ---------------------------------------------------------------
def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def ledger(home=HOME) -> list[dict]:
    """Every ledger record, with its 1-based line number as _line. Bad lines are skipped.
    Raw: includes rows from a later window of an earlier epoch, which can be dated AFTER the
    current simulated time. Use visible_ledger() for anything the agent reasons from."""
    path = Path(home) / "state" / "ledger.jsonl"
    out = []
    if not path.exists():
        return out
    for i, line in enumerate(path.read_text().splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rec["_line"] = i
            out.append(rec)
    return out


def now_utc(home=HOME) -> datetime | None:
    """The current simulated time: the most recently WRITTEN ledger row's own timestamp. The ledger
    is append-only in the order the replay visits bars, so the last row written is always "now" --
    even right after an epoch wrap, when it jumps back to an earlier calendar date than rows already
    sitting earlier in the file. Nothing after this has happened yet, from the agent's point of view."""
    rows = ledger(home)
    return _utc(rows[-1]["utc"]) if rows else None


def visible_ledger(home=HOME) -> list[dict]:
    """ledger(), minus any row describing an event after the current simulated time (see now_utc).
    This is the ledger as the agent may reason from it: blind to its own future."""
    now = now_utc(home)
    if now is None:
        return ledger(home)
    return [r for r in ledger(home) if (_utc(r.get("closed_utc") or r.get("utc")) or now) <= now]


def visible_text(text: str, now: "datetime | None") -> bool:
    """False if the text names an ISO date (YYYY-MM-DD) strictly after now -- a paper about a day
    that, from the current simulated position, has not happened yet."""
    if now is None:
        return True
    for d in re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text):
        try:
            if datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) > now:
                return False
        except ValueError:
            continue
    return True


def _utc(s) -> datetime | None:
    try:
        return datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def rec_day(rec: dict) -> str | None:
    """The Prague day a record belongs to: its own 'day' field, else its close or log time converted."""
    if rec.get("day"):
        return rec["day"]
    t = _utc(rec.get("closed_utc") or rec.get("utc"))
    return t.astimezone(ZoneInfo(DAY_TZ)).date().isoformat() if t else None


def _md(src: str, path: Path, home: Path):
    rel = path.relative_to(home).as_posix()
    title, start, buf = path.stem, 1, []
    for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
        if line.startswith("#"):
            if buf:
                yield src, f"{rel}:{start}", title, "\n".join(buf)
                buf, start = [], i
            title = line.lstrip("#").strip()
        buf.append(line)
    if buf:
        yield src, f"{rel}:{start}", title, "\n".join(buf)


def chunks(home=HOME):
    """(source, ref, title, text) for every section: a markdown heading, a symbol, a JSON key, a ledger line.
    Never yields a papers/ section dated after the current simulated time (see visible_text)."""
    home = Path(home)
    now = now_utc(home)
    for src in ("field", "papers", "manual"):
        for p in sorted((home / src).glob("*")) if (home / src).is_dir() else []:
            if p.suffix in (".md", ".txt"):
                for row in _md(src, p, home):
                    if src != "papers" or visible_text(row[3], now):
                        yield row
            elif p.suffix == ".json":
                yield src, p.relative_to(home).as_posix(), p.stem, p.read_text(errors="ignore")
    for p in sorted((home / "knowledge").glob("*.json")) if (home / "knowledge").is_dir() else []:
        data = _json(p)
        if not isinstance(data, dict):
            continue
        rel = p.relative_to(home).as_posix()
        for k, v in (data.get("symbols") or {}).items() if p.name == "symbols.json" else []:
            yield "knowledge", f"{rel}#{k}", k, f"{k} {json.dumps(v)}"
        for k, v in data.items():
            if p.name == "symbols.json" and k == "symbols":
                continue
            text = " ".join(map(str, v)) if isinstance(v, list) else json.dumps(v) if isinstance(v, dict) else str(v)
            yield "knowledge", f"{rel}#{k}", k, f"{k}: {text}"
    for r in visible_ledger(home):
        yield "ledger", f"ledger:{r['_line']}", str(r.get("type", "")), \
            json.dumps({k: v for k, v in r.items() if k != "_line"})


# ---- questions -------------------------------------------------------------
def overview(home=HOME) -> dict:
    sections = Counter(src for src, *_ in chunks(home))
    return {"home": str(home), "sections": {s: sections.get(s, 0) for s in SOURCES},
            "ledger_types": dict(Counter(str(r.get("type")) for r in visible_ledger(home)))}


def spec(symbol: str, home=HOME) -> dict:
    data = _json(Path(home) / "knowledge" / "symbols.json") or {}
    syms = data.get("symbols") or {}
    q = symbol.lower()
    hits = [k for k in syms if k.lower() == q] or [k for k in syms if k.lower().startswith(q)]
    if len(hits) == 1:
        k = hits[0]
        return {"symbol": k, "replayable": k in data.get("replayable", []), **syms[k]}
    return {"symbol": None, "candidates": hits or sorted(k for k in syms if q[:3] in k.lower())}


def common(rows: list[dict]) -> dict:
    """What a set of closed trades share, counted. Words of why_open count once per trade."""
    if not rows:
        return {"n": 0}
    r = [x["r_net"] for x in rows if isinstance(x.get("r_net"), (int, float))]
    mean = sum(r) / len(r) if r else None
    se = math.sqrt(sum((v - mean) ** 2 for v in r) / (len(r) - 1) / len(r)) if len(r) > 1 else None
    opened = [_utc(x.get("opened_utc")) for x in rows]
    words = Counter(w for x in rows for w in set(re.findall(r"[a-z][a-z0-9.]{2,}", str(x.get("why_open", "")).lower()))
                    if w not in STOP)
    risk = [x["risk_usd"] for x in rows if isinstance(x.get("risk_usd"), (int, float))]

    def top(c, k=5):
        return dict(c.most_common(k))
    return {"n": len(rows),
            "symbol": top(Counter(x.get("symbol") for x in rows)),
            "side": top(Counter(x.get("side") for x in rows)),
            "exit_reason": top(Counter(x.get("reason") for x in rows)),
            "open_hour_utc": top(Counter(t.hour for t in opened if t)),
            "open_weekday_utc": top(Counter(t.strftime("%a") for t in opened if t)),
            "r_net_mean": round(mean, 3) if mean is not None else None,
            "r_net_se": round(se, 3) if se is not None else None,
            "net_usd_sum": round(sum(x.get("net_usd") or 0 for x in rows), 2),
            "risk_usd_mean": round(sum(risk) / len(risk), 2) if risk else None,
            "why_open_words_in_k_trades": top(words, 8)}


def trades(home=HOME, result: str | None = None, symbol: str | None = None, n: int = 10) -> dict:
    rows = [r for r in visible_ledger(home) if r.get("type") == "trade_closed"]
    if symbol:
        rows = [r for r in rows if str(r.get("symbol", "")).lower() == symbol.lower()]
    if result == "loss":
        rows = [r for r in rows if (r.get("net_usd") or 0) < 0]
    elif result == "win":
        rows = [r for r in rows if (r.get("net_usd") or 0) > 0]
    rows = rows[-n:] if n > 0 else rows
    return {"trades": [{k: r.get(k) for k in TRADE_KEYS} for r in rows], "common": common(rows)}


def day(date: str, home=HOME) -> dict:
    now = now_utc(home)
    recs = [r for r in visible_ledger(home) if rec_day(r) == date]
    pdir = Path(home) / "papers"
    papers = [f"papers/{p.name}" for p in sorted(pdir.glob("*")) if p.is_file()
              and date in (text := p.read_text(errors="ignore")) and visible_text(text, now)] \
        if pdir.is_dir() else []
    return {"day": date, "journal": [r.get("text") for r in recs if r.get("type") == "note"],
            # scan rows and alert bookkeeping left out, fired alerts without their repeated plan (2026-10-18)
            "events": [{k: v for k, v in r.items() if k != "bar" and not (r.get("type") == "level_alert" and k in
                        ("why", "ref", "expires_utc", "set_utc", "by"))}
                       for r in recs if r.get("type") not in ("note", "setup_candidate", "alert_expired", "field_read")],
            "papers": papers}


def _snippet(line: str, terms: list[str], width: int = 200) -> str:
    line = line.strip()
    if len(line) <= width:
        return line
    low = line.lower()
    i = min([low.find(t) for t in terms if t in low] or [0])
    a = max(0, i - 60)
    return ("..." if a else "") + line[a:a + width] + "..."


def find(words: list[str], home=HOME, where: str | None = None, n: int = 8) -> list[dict]:
    terms = [w.lower() for w in words if w.strip()]
    if not terms:
        return []
    hits = []
    for order, (src, ref, title, text) in enumerate(chunks(home)):
        if where and src != where:
            continue
        low = f"{ref} {text}".lower()
        counts = [low.count(t) for t in terms]
        distinct = sum(1 for c in counts if c)
        if distinct:
            hits.append((-distinct, -sum(counts), order, ref, title, text))
    hits.sort()
    out = []
    for d, _, _, ref, title, text in hits[:n]:
        lines = [_snippet(x, terms) for x in text.splitlines() if any(t in x.lower() for t in terms)][:3]
        out.append({"ref": ref, "title": title, "matched": f"{-d}/{len(terms)}", "lines": lines})
    return out


# ---- command line ----------------------------------------------------------
def main(argv=None, home=HOME) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="kstore.py", description="knowledge store: field, knowledge, papers, ledger")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("spec", help="one symbol's spec, feed span, caveats")
    s.add_argument("symbol")
    t = sub.add_parser("trades", help="last N closed trades and what they share")
    t.add_argument("--result", choices=["loss", "win"])
    t.add_argument("--symbol")
    t.add_argument("-n", type=int, default=10)
    d = sub.add_parser("day", help="journal and events of one Prague day")
    d.add_argument("date")
    f = sub.add_parser("find", help="ranked sections containing the words")
    f.add_argument("words", nargs="+")
    f.add_argument("--in", dest="where", choices=SOURCES)
    f.add_argument("-n", type=int, default=8)
    a = p.parse_args(argv)

    if a.cmd is None:
        print(__doc__)
        print(json.dumps(overview(home), separators=(",", ":")))
    elif a.cmd == "spec":
        out = spec(a.symbol, home)
        print(json.dumps(out, separators=(",", ":")))
        return 0 if out["symbol"] else 1
    elif a.cmd == "trades":
        print(json.dumps(trades(home, a.result, a.symbol, a.n), separators=(",", ":")))
    elif a.cmd == "day":
        print(json.dumps(day(a.date, home), separators=(",", ":")))
    elif a.cmd == "find":
        hits = find(a.words, home, a.where, a.n)
        for h in hits:
            print(f"[{h['matched']}] {h['ref']}  {h['title']}")
            for line in h["lines"]:
                print(f"    {line}")
        if not hits:
            print("no section contains any of those words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
