"""DREAMS: what live trading learned, handed to the training replay as intuitions of unknown origin.

Operator, 2026-10-11: live must learn the way training does, but live must not poison the blind
replay (training sits at a 2025 date; a live lesson about October 2026 would tell it its own future).
So live does not hand training its lessons. It hands it DREAMS: short, timeless process rules, with
no date, no price, no market and no trace of where they came from -- "helpful, but training doesn't
know precisely where they come from".

  dreams.py propose --text "..."               LIVE (live_dream kind only): submit one rule
  dreams.py read [-n N]                        every dream, its test notes, as training sees them
  dreams.py note ID --text "..."               TRAINING: record how a dream held up when tested
  dreams.py revise ID --text "..." --why "..."  TRAINING: rewrite a dream's rule
  dreams.py retire ID --why "..."              TRAINING: drop a dream that did not hold up

Operator, same day: "training can use and test them and also modify them as per will". Training owns
the dreams once they land: it reads them, tests them against its own record, notes the results,
rewrites or retires them. Every write -- live's proposals and training's notes and revisions alike --
passes the SAME timeless-content rules, because the replay wraps epochs: a training note naming a
2025 date would hand a later replay of 2024 its own future, the same leak as live's.

Who does what:
  * tools/live_loop.py, at each Prague-day rollover in LIVE, gathers that day's live process lessons
    (live board topics lesson / day_review / risk -- never pre_trade or scan posts, which carry
    levels) and wakes the live_dream kind (tools/live_routing.py) to distil them.
  * live_dream may only call this tool. check() is the guarantee, not the prompt: whatever the model
    writes, a dream that names a date, a year, a month, a clock time, a price-like number, any
    instrument, an asset nickname, a market-regime claim, its own provenance or a time anchor is
    REFUSED, as is a near-duplicate and anything past MAX_PER_DAY.
  * accepted dreams go to knowledge/dreams.json -- numbered, no date, no source. training reads
    knowledge/** and kstore indexes it. No kind has a raw Edit on knowledge/ (guard/policy.py): every
    change, from either side, goes through this tool and its checks.
  * the provenance (when, which day, every refusal and why) goes to state/dreams_audit.jsonl, which
    no training kind can read -- the operator can always trace a dream back; training never can.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
MAX_PER_DAY = 3
MAX_KEPT = 60
MIN_LEN, MAX_LEN = 30, 400
DUP_JACCARD = 0.75
DAY_TZ = ZoneInfo("Europe/Prague")

ABOUT = ("Intuitions of unknown origin. Each is a short process rule that proved useful somewhere. None "
         "carries a date, a price or a market. Treat every one as a HYPOTHESIS to test against your own "
         "record (kstore.py trades, the ledger) -- never as a fact, and never over what your own data shows.")


def _paths(home: Path):
    return Path(home) / "knowledge" / "dreams.json", Path(home) / "state" / "dreams_audit.jsonl"


# ---------------------------------------------------------------- the validator
_MONTHS = ("january february march april june july august september october november december "
           "jan feb mar apr jun jul aug sep sept oct nov dec").split()          # "may" is left out: a common verb
_ANCHORS = (r"\btoday\b", r"\byesterday\b", r"\btomorrow\b", r"\btonight\b", r"\bthis (week|month|year|morning)\b",
            r"\blast (week|month|year|night)\b", r"\bnext (week|month|year)\b", r"\brecently\b", r"\blately\b")
_PROVENANCE = (r"\blive\b", r"\breal (account|money|fills?|trades?|broker)\b", r"\bfunded\b", r"\bdemo\b",
               r"\bftmo-demo\b", r"\bpaper\b", r"\breplay\b", r"\bbacktest", r"\bsimulat")
_NICKNAMES = ("gold silver platinum palladium copper oil crude brent wti euro yen pound sterling franc aussie kiwi "
              "loonie yuan rand peso nasdaq dow dax ftse spx nikkei bitcoin btc ether ethereum dollar greenback "
              "cable").split()
_CURRENCIES = "usd eur jpy gbp aud nzd cad chf cnh mxn sgd zar xau xag xpt xpd xcu eth".split()
_CLASSES = r"\b(metals?|indices|index|fx|forex|currenc(y|ies)|crypto|commodit(y|ies)|energ(y|ies)|equit(y|ies)|stocks?)\b"
_MOVES = (r"\b(rall(y|ies|ied)|surg(e|ed|es)|plung(e|ed|es)|crash(ed|es)?|soar(ed|s)?|rose|risen|fell|fallen|"
          r"sold off|sell-?off|bull market|bear market|uptrend|downtrend|bullish|bearish)\b")


def _instrument_words(home: Path) -> set:
    try:
        names = json.loads((Path(home) / "guard" / "instruments.json").read_text()).keys()
    except Exception:
        names = []
    out = set()
    for n in names:
        out.add(n.lower())
        out.add(n.lower().split(".")[0])
    return out


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z]+", text.lower()))


def check(text: str, existing: list[str], accepted_today: int, home: Path = HOME,
          origin: str = "live") -> tuple[bool, list[str]]:
    """(accepted?, every reason it is not). Pure: reads only the instrument list. origin "live" (a
    proposal) adds the provenance rule and the daily cap; "training" (a note or a revision) may say it
    tested something, but must stay just as timeless: no date, price, instrument or market claim."""
    t = " ".join(text.split())
    low = t.lower()
    why = []
    if not (MIN_LEN <= len(t) <= MAX_LEN):
        why.append(f"length {len(t)} outside {MIN_LEN}-{MAX_LEN} characters")
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", t) or re.search(r"\b(19|20)\d{2}\b", t):
        why.append("names a date or a year")
    if any(re.search(rf"\b{m}\b", low) for m in _MONTHS):
        why.append("names a month")
    if re.search(r"\b\d{1,2}:\d{2}\b", t):
        why.append("names a clock time (say the session instead: 'the US cash open')")
    for num in re.findall(r"\d+(?:[.,]\d+)?", t):
        whole, _, frac = num.replace(",", ".").partition(".")
        if int(whole or 0) >= 100 or len(frac) > 2:
            why.append(f"price-like number {num}")
            break
    words = _tokens(t) | set(re.findall(r"[a-z0-9]+(?:\.[a-z]+)?", low))
    hit = sorted(words & (_instrument_words(home) | set(_CURRENCIES)))
    if hit:
        why.append(f"names an instrument ({', '.join(hit)})")
    nick = sorted(w for w in _NICKNAMES if re.search(rf"\b{w}\b", low))
    if nick:
        why.append(f"names a market ({', '.join(nick)})")
    if re.search(_CLASSES, low) and re.search(_MOVES, low):
        why.append("claims what a market did (an asset class next to a market-move word)")
    if any(re.search(a, low) for a in _ANCHORS):
        why.append("anchors itself in time ('today', 'this week', ...)")
    if origin == "live" and any(re.search(p, low) for p in _PROVENANCE):
        why.append("says where it came from (live / real / demo / paper / replay ...)")
    if origin == "training" and re.search(r"\blive\b|\breal (account|money)\b", low):
        why.append("names the live account")
    toks = _tokens(t)
    for e in existing:
        et = _tokens(e)
        if toks and et and len(toks & et) / len(toks | et) >= DUP_JACCARD:
            why.append("near-duplicate of an existing dream")
            break
    if origin == "live" and accepted_today >= MAX_PER_DAY:
        why.append(f"already {accepted_today} dreams today (cap {MAX_PER_DAY})")
    return not why, why


# ---------------------------------------------------------------- storage
def _load(home: Path) -> dict:
    path, _ = _paths(home)
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"_about": ABOUT}


def _save(home: Path, data: dict) -> None:
    path, _ = _paths(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def _audit(home: Path, rec: dict) -> None:
    _, audit = _paths(home)
    audit.parent.mkdir(parents=True, exist_ok=True)
    with open(audit, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _today() -> str:
    return dt.datetime.now(DAY_TZ).date().isoformat()


def _accepted_today(home: Path) -> int:
    _, audit = _paths(home)
    if not audit.exists():
        return 0
    day, n = _today(), 0
    for line in audit.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += int(r.get("event") == "accepted" and r.get("day") == day)
    return n


def _rule(v) -> str:
    return v.get("rule", "") if isinstance(v, dict) else str(v)


def dreams(home: Path = HOME) -> dict:
    return {k: v for k, v in _load(home).items() if k.startswith("dream ")}


def propose(text: str, home: Path = HOME) -> dict:
    text = " ".join(str(text).split())
    data = _load(home)
    existing = [_rule(v) for k, v in data.items() if k.startswith("dream ")]
    ok, why = check(text, existing, _accepted_today(home), home, origin="live")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not ok:
        _audit(home, {"event": "refused", "utc": now, "day": _today(), "text": text, "reasons": why})
        return {"ok": False, "reasons": why}
    nums = [int(k.split()[1]) for k in data if k.startswith("dream ") and k.split()[1].isdigit()]
    nxt = max(nums + [_last_number(home)]) + 1
    key = f"dream {nxt:03d}"
    data.setdefault("_about", ABOUT)
    data[key] = {"rule": text, "notes": []}
    retired = []
    while len([k for k in data if k.startswith("dream ")]) > MAX_KEPT:
        oldest = min((k for k in data if k.startswith("dream ")), key=lambda k: int(k.split()[1]))
        retired.append({oldest: data.pop(oldest)})
    _save(home, data)
    _audit(home, {"event": "accepted", "utc": now, "day": _today(), "id": key, "text": text})
    for r in retired:
        _audit(home, {"event": "retired", "utc": now, "day": _today(), "dream": r})
    return {"ok": True, "id": key}


def _training_edit(home: Path, dream_id: str, text: str, kind: str, why: str = "") -> dict:
    data = _load(home)
    key = dream_id if dream_id.startswith("dream ") else f"dream {int(dream_id):03d}" if dream_id.isdigit() else dream_id
    if key not in data:
        return {"ok": False, "reasons": [f"no {key}; see dreams.py read"]}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    cur = data[key] if isinstance(data[key], dict) else {"rule": str(data[key]), "notes": []}
    if kind == "retire":
        if len(why.strip()) < 10:
            return {"ok": False, "reasons": ["a reason is required (at least 10 characters) in --why"]}
        data.pop(key)
        _save(home, data)
        _audit(home, {"event": "retired_by_training", "utc": now, "id": key, "dream": cur, "why": why.strip()})
        return {"ok": True, "retired": key}
    text = " ".join(str(text).split())
    others = [_rule(v) for k, v in data.items() if k.startswith("dream ") and k != key] if kind == "revise" else []
    ok, reasons = check(text, others, 0, home, origin="training")
    if kind == "revise" and len(why.strip()) < 10:
        ok, reasons = False, reasons + ["a reason is required (at least 10 characters) in --why"]
    if not ok:
        _audit(home, {"event": f"{kind}_refused", "utc": now, "id": key, "text": text, "reasons": reasons})
        return {"ok": False, "reasons": reasons}
    if kind == "note":
        cur["notes"] = (cur.get("notes") or []) + [text]
        cur["notes"] = cur["notes"][-12:]
    else:
        cur["rule"] = text
        cur["revised"] = int(cur.get("revised", 0)) + 1
    data[key] = cur
    _save(home, data)
    _audit(home, {"event": f"{kind}d_by_training", "utc": now, "id": key, "text": text, "why": why.strip()})
    return {"ok": True, "id": key}


def note(dream_id: str, text: str, home: Path = HOME) -> dict:
    return _training_edit(home, dream_id, text, "note")


def revise(dream_id: str, text: str, why: str, home: Path = HOME) -> dict:
    return _training_edit(home, dream_id, text, "revise", why)


def retire(dream_id: str, why: str, home: Path = HOME) -> dict:
    return _training_edit(home, dream_id, "", "retire", why)


def _last_number(home: Path) -> int:
    """Numbers keep counting after a dream is retired (the audit remembers the highest)."""
    _, audit = _paths(home)
    best = 0
    if audit.exists():
        for line in audit.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") == "accepted" and str(r.get("id", "")).startswith("dream "):
                best = max(best, int(r["id"].split()[1]))
    return best


def main(argv=None, home: Path = HOME) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="dreams.py", description="live lessons, as dreams for the training replay")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("propose")
    pr.add_argument("--text", required=True)
    rd = sub.add_parser("read")
    rd.add_argument("-n", type=int, default=0)
    no = sub.add_parser("note")
    no.add_argument("id")
    no.add_argument("--text", required=True)
    rv = sub.add_parser("revise")
    rv.add_argument("id")
    rv.add_argument("--text", required=True)
    rv.add_argument("--why", required=True)
    rt = sub.add_parser("retire")
    rt.add_argument("id")
    rt.add_argument("--why", required=True)
    a = p.parse_args(argv)
    if a.cmd == "propose":
        print(json.dumps(propose(a.text, home), indent=1, ensure_ascii=False))
    elif a.cmd == "note":
        print(json.dumps(note(a.id, a.text, home), indent=1, ensure_ascii=False))
    elif a.cmd == "revise":
        print(json.dumps(revise(a.id, a.text, a.why, home), indent=1, ensure_ascii=False))
    elif a.cmd == "retire":
        print(json.dumps(retire(a.id, a.why, home), indent=1, ensure_ascii=False))
    elif a.cmd == "read":
        items = list(dreams(home).items())
        print(json.dumps(dict(items[-a.n:] if a.n > 0 else items), separators=(",", ":"), ensure_ascii=False))
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
