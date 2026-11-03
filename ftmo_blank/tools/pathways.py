"""PATHWAYS: the lessons a worker needs for the situation in front of it -- and a map of all the rest.

Operator, 2026-10-18: "if I'm about to fall from a bridge and I see water, I replay how to dive, not my
third-grade exam". Before this, live's decider read the last 10 training lessons by DATE (~12k characters,
any symbol, any setup), training's decider read none, no scout read any, and the papers were reached only
through a keyword search (kstore.py find) that no worker ran in the measured week.

Every lesson (a review's board post) and every paper is a CARD. Code tags each card with the keys a trader
walks by -- symbol, setup / anti-pattern code (knowledge/vocab.json S1-S4, A1-A5), phase of the trade
(entry, stop, target, manage, size, exit) and session (asia, london, us_open, us, rollover, friday, event)
-- and, for a situation (which symbols, what phase, what time, which codes the plan names), walks them:

  1. every card about the situation's own symbol, then its close relatives (gold in USD <-> gold in EUR,
     the US indices together, FX pairs sharing a currency),
  2. symbol-free process cards that share a code, a session or a phase,
  3. cards about another symbol only when they share a setup/anti-pattern code or a session,
  ranked by how many keys they share, newest first among equals.

The best cards are printed IN FULL -- never shortened, never summarised -- as many as fit the budget; every
other card on the path is NAMED with its keys (one command away: --show ID), and a one-line map lists every
card by symbol. Nothing is hidden from a worker: what is not printed is named.

Corpus per desk (the boundary is the dreams rule, todos/004: live reaches training only as timeless dreams):
  training  the training board's lessons + papers/, blind to the replay's own future (board.read's clock,
            kstore.visible_text)
  live      the training board's lessons (all of them) + papers/ (all) + the live board's own lessons

The CLI is the `lessons` verb of tools/board.py (training) and tools/live_board.py (live).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(HOME / "agent_code"))
sys.path.insert(0, str(HOME))

LESSON_TOPICS = ("lesson", "attempt_passed_review")   # board topics that hold a review's conclusion
# 2026-11-01 (operator: "trim it, it is not using all it is fed"): measured on the 11-01 replay -- 325 calls, 0 orders --
# no worker ran `lessons --show` or `lessons --symbol` once, so a lesson block is read only as far as it is printed;
# the budgets below are what a worker actually reads. The library map and the long "also" list went with them.
BUDGET = {"scout": 1_800, "trader": 3_500, "decider": 3_000, "review": 5_000, "cli": 9_000}
MAX_FULL = {"scout": 2, "trader": 3, "decider": 3, "review": 5, "cli": 8}
ALSO_SHOWN = {"cli": 12, "review": 6}             # other roles: one count line, no names
# 2026-11-01, measured by ablation (todos/009, the same 21 market moments with one prompt layer changed at a time): the
# lesson library is the strongest DISMISS driver of all -- blank desk + lessons: 19 of 21 dismissed, 15 even with every
# calibration card taken out. Three kinds of card are verdicts on skipping, not lessons about trading: the calibration
# record and its reviews (a blind bracket loses both ways on any mover), the flat-day reviews and pass gradings built on
# that same ruler ("rejections kept losers out", "pass was RIGHT"), and the vault's rules ("enter only when the last hour
# is flat": the blind version of that class is the WORST on the replay, -0.27R, n=16,349). They stay on the review path
# and on the CLI; the vault still answers on call (consult.py). What reaches an entry decision: reviews of real trades.
ENTRY_ROLES = ("scout", "trader", "decider")

# ---- keys -------------------------------------------------------------------------------------------
ALIASES = {"gold": "XAUUSD", "silver": "XAGUSD", "nasdaq": "US100.cash", "nas100": "US100.cash",
           "dow": "US30.cash", "s&p": "US500.cash", "spx": "US500.cash", "dax": "GER40.cash",
           "ftse": "UK100.cash", "wti": "USOIL.cash", "brent": "UKOIL.cash"}
GROUPS = [{"XAUUSD", "XAUEUR"}, {"XAGUSD", "XAGEUR"}, {"XPTUSD", "XPDUSD"},
          {"US100.cash", "US30.cash", "US500.cash"}, {"GER40.cash", "UK100.cash"},
          {"USOIL.cash", "UKOIL.cash"}]
CODE_WORDS = {   # the vocabulary's own names, plus the plain words the reviews use for the same thing
    "S1": [r"accept-cont", r"continuation after acceptance"], "S2": [r"failed-ctr", r"failed counter"],
    "S3": [r"event-retrace", r"spike-and-fade", r"post-event retrace"], "S4": [r"break-accept", r"breakout"],
    "A1": [r"\bchas(e|ed|es|ing)\b"], "A2": [r"early[- ]pullback"], "A3": [r"\bpacing\b"],
    "A4": [r"thin[ /-]?(hour|gap)", r"thin hours"], "A5": [r"\bcorrelated\b"],
}
PHASE_WORDS = {
    "entry": [r"\bentr(y|ies|ed)\b", r"\btrigger", r"\bretest", r"\bsetup"],
    "stop": [r"\bstop", r"\bsl\b", r"sl@s", r"clearance", r"\bwick"],
    "target": [r"\btarget", r"\btp\b", r"take profit"],
    "manage": [r"break-?even", r"\bbe\b", r"\block\b", r"\btrail", r"\bpartial", r"\bmfe\b", r"management"],
    "size": [r"\blots?\b", r"\bsizing\b", r"risk_pct", r"risk cap", r"\bsized?\b"],
    "exit": [r"\bexit", r"manual close", r"flatten", r"closed early"],
}
SESSION_WORDS = {
    "asia": [r"\basia", r"\btokyo\b", r"before 0?7:00 utc", r"pre-0?7:00"],
    "london": [r"\blondon\b", r"\bfrankfurt\b", r"\beu open"],
    "us_open": [r"us open", r"us-open", r"cash open", r"ny open", r"new york open", r"13:30", r"14:30"],
    "us": [r"us session", r"new york session", r"\bny session"],
    "rollover": [r"rollover", r"\bswap\b"],
    "friday": [r"\bfriday", r"\bweekend"],
    "event": [r"\bcpi\b", r"\bnfp\b", r"\bfomc\b", r"\bnews\b", r"\bevent\b", r"\becb\b"],
}
MADRID, LONDON, NEWYORK, PRAGUE = (ZoneInfo(z) for z in ("Europe/Madrid", "Europe/London", "America/New_York",
                                                        "Europe/Prague"))


def _symbols() -> list[str]:
    try:
        from guard import rules as R
        syms = set(R.INSTRUMENTS)
    except Exception:
        syms = set()
    try:
        syms |= set(json.loads((HOME / "knowledge" / "symbols.json").read_text()).get("symbols", {}))
    except (OSError, ValueError):
        pass
    return sorted(syms)


SYMBOLS = _symbols()
_CANON = {s.upper(): s for s in SYMBOLS}


def canon(sym) -> str | None:
    """'US100.CASH' / 'us100' / 'US100.cash' -> 'US100.cash' (the broker's spelling)."""
    if not sym:
        return None
    s = str(sym).strip().upper()
    if s in _CANON:
        return _CANON[s]
    hits = [v for k, v in _CANON.items() if k.split(".")[0] == s.split(".")[0]]
    return hits[0] if len(hits) == 1 else str(sym)


def symbols_in(text: str) -> set[str]:
    low = text.lower()
    out = set()
    for s in SYMBOLS:
        base = s.split(".")[0].lower()
        if re.search(rf"(?<![a-z0-9]){re.escape(base)}(?![a-z0-9])", low):
            out.add(s)
    for word, s in ALIASES.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", low):
            out.add(s)
    return out


def _words(text: str, table: dict) -> set[str]:
    low = text.lower()
    return {k for k, pats in table.items() if any(re.search(p, low) for p in pats)}


def codes_in(text: str) -> set[str]:
    return set(re.findall(r"\b([SA][1-5])\b", text)) & set(CODE_WORDS) | _words(text, CODE_WORDS)


def relatives(sym: str) -> set[str]:
    """Close relatives: the same instrument in another currency or the same index family, and FX pairs that
    share a NON-USD currency (EURUSD <-> EURJPY moves together; EURUSD <-> USDJPY share only the dollar,
    which half the list shares)."""
    out = set()
    for g in GROUPS:
        if sym in g:
            out |= g - {sym}
    fx = lambda s: len(s) == 6 and s.isalpha() and not s.startswith(("XA", "XP", "XC"))
    if fx(sym):
        ccys = {sym[:3], sym[3:]} - {"USD"}
        out |= {s for s in SYMBOLS if fx(s) and s != sym and ({s[:3], s[3:]} - {"USD"}) & ccys}
    return out


def sessions_at(when: dt.datetime | None) -> set[str]:
    """The session keys of a moment (DST-correct): Asia, London morning, the US cash open, the US session,
    the rollover hour, Friday."""
    if when is None:
        return set()
    when = when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
    lon, ny = when.astimezone(LONDON), when.astimezone(NEWYORK)
    lm, nm = lon.hour * 60 + lon.minute, ny.hour * 60 + ny.minute
    out = set()
    if lm < 7 * 60 or ny.hour >= 18:
        out.add("asia")
    elif lm < 12 * 60:
        out.add("london")
    if 9 * 60 <= nm < 11 * 60:
        out.add("us_open")
    elif 11 * 60 <= nm < 16 * 60 + 30:
        out.add("us")
    elif 16 * 60 + 30 <= nm < 18 * 60:
        out.add("rollover")
    if when.astimezone(MADRID).weekday() == 4:
        out.add("friday")
    return out


# ---- cards ------------------------------------------------------------------------------------------
def _card(cid: str, kind: str, text: str, when: str | None, symbol=None, source: str = "") -> dict:
    syms = {x for x in ({canon(symbol)} if symbol else set()) | symbols_in(text) if x}
    return {"id": cid, "kind": kind, "source": source, "when": when or "", "text": text.strip(),
            "symbols": sorted(syms), "own_symbol": canon(symbol) if symbol else None,
            "codes": sorted(codes_in(text)), "phases": sorted(_words(text, PHASE_WORDS)),
            "sessions": sorted(_words(text, SESSION_WORDS))}


def _board_rows(path: Path):
    if not path.exists():
        return
    for i, line in enumerate(path.read_text().splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield i, rec


def _is_lesson(rec: dict) -> bool:
    t = str(rec.get("topic", "")).lower()
    return any(k in t for k in LESSON_TOPICS)


def cards(desk: str = "training", home=HOME) -> list[dict]:
    """Every card this desk may see, oldest first."""
    home = Path(home)
    import kstore as K
    now = K.now_utc(home) if desk == "training" else None
    out = []
    for i, rec in _board_rows(home / "state" / "board.jsonl"):
        if not _is_lesson(rec):
            continue
        t = K._utc(rec.get("utc"))
        if now is not None and t is not None and t > now:
            continue  # a later epoch's lesson: the replay has not lived it yet (board.read's rule)
        if desk == "live" and str(rec.get("text", "")).startswith("CASTIGATED"):
            continue  # 2026-11-01: the training desk's usage punishment (tools/castigation.py) never reaches live
        out.append(_card(f"L{i}", "lesson", f"[{rec.get('utc', '')}] {rec.get('worker', '?')}/{rec.get('topic', '?')}: "
                         f"{rec.get('text', '')}", rec.get("utc"), rec.get("symbol"), "training board"))
    if desk == "live":
        for i, rec in _board_rows(home / "state" / "live_board.jsonl"):
            if _is_lesson(rec):
                out.append(_card(f"LL{i}", "live lesson", f"[{rec.get('utc', '')}] {rec.get('worker', '?')}/"
                                 f"{rec.get('topic', '?')}: {rec.get('text', '')}", rec.get("utc"), rec.get("symbol"),
                                 "live board"))
    # calibration cards (tools/opportunities.py): the code-graded record of what the desk did NOT trade, rebuilt
    # every day; training's are blind to the replay's future; the live desk sees training's and its own
    try:
        import opportunities as OPP
        sources = [("training", now)] + ([("live", None)] if desk == "live" else [])
        for src, upto in sources:
            for cid, sym, codes, text, when in OPP.cards(src, home, upto):
                c = _card(("CL" + cid[1:]) if src == "live" else cid, "calibration", text, when, sym,
                          f"{src} calibration")
                c["codes"] = sorted(set(c["codes"]) | set(codes))
                out.append(c)
    except Exception:
        pass   # a calibration file that cannot be read never blocks the lessons
    pdir = home / "papers"
    for p in sorted(pdir.glob("*.md")) if pdir.is_dir() else []:
        text = p.read_text(errors="ignore")
        if now is not None and not K.visible_text(text, now):
            continue  # names a day the replay has not reached
        days = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text)   # the latest day it is about (desk calendar)
        when = max(days) if days else ""   # undated: no recency either way (a file time is the real clock, not the desk's)
        stem_syms = symbols_in(p.stem.replace("_", " "))
        out.append(_card(f"P:{p.stem}", "paper", text, when, sorted(stem_syms)[0] if len(stem_syms) == 1 else None,
                         f"papers/{p.name}"))
    return out


# ---- the walk ---------------------------------------------------------------------------------------
def situation(symbols=(), phases=(), when=None, codes=(), text: str = "") -> dict:
    """What the worker is facing. `text` (a plan, an alert's why, a trade's rationale) adds the codes and
    sessions it names."""
    syms = [s for s in (canon(x) for x in symbols) if s]
    return {"symbols": syms, "phases": sorted(set(phases)),
            "codes": sorted(set(codes) | (codes_in(text) if text else set())),
            "sessions": sorted(sessions_at(when) | (_words(text, SESSION_WORDS) if text else set()))}


SESSION_WEIGHT = {"friday": 0.5}   # every other session key 1.5: a Friday is weak evidence of relevance


def _age_days(when: str, newest: str) -> float:
    try:
        a = dt.datetime.strptime(when[:10], "%Y-%m-%d")
        b = dt.datetime.strptime(newest[:10], "%Y-%m-%d")
        return max(0.0, (b - a).days)
    except ValueError:
        return 365.0


def score(card: dict, sit: dict, newest: str = "") -> tuple[float, list[str]]:
    """(score, why). 0 = not on this path. Symbol first (own > names > related > symbol-free), then the
    keys it shares, then how recent it is: a later lesson usually refines an earlier one (half-life 3 weeks
    of the desk's own calendar)."""
    why, s = [], 0.0
    cs, ss = set(card["symbols"]), set(sit["symbols"])
    rel = set().union(*(relatives(x) for x in ss)) if ss else set()
    shared = {"codes": set(card["codes"]) & set(sit["codes"]), "sessions": set(card["sessions"]) & set(sit["sessions"]),
              "phases": set(card["phases"]) & set(sit["phases"])}
    anti = sorted(c for c in card["codes"] if c.startswith("A")) if "entry" in sit["phases"] else []
    if card["own_symbol"] in ss:
        s += 6
        why.append("about " + card["own_symbol"])
    elif cs & ss:
        s += 3
        why.append("names " + ",".join(sorted(cs & ss)))
    elif cs & rel:
        s += 2
        why.append("related " + ",".join(sorted(cs & rel)))
    elif not cs:
        s += 1
        why.append("any symbol")
    elif ss and not (shared["codes"] or set(shared["sessions"]) - {"friday"} or anti):
        return 0.0, []           # another symbol's lesson with nothing else in common: not this path
    if not ss and cs:
        s += 1                   # no symbol in the situation: every card is fair, by its other keys
    s += 2 * len(shared["codes"]) + sum(SESSION_WEIGHT.get(k, 1.5) for k in shared["sessions"])
    s += 0.5 * min(3, len(shared["phases"]))
    if anti and not shared["codes"]:
        s += 1                   # an entry is where every anti-pattern (chase, pacing, thin hour...) bites
        why.append("entry anti-pattern " + " ".join(anti))
    for k in ("codes", "sessions"):
        if shared[k]:
            why.append(" ".join(sorted(shared[k])))
    if newest:
        s += 2.0 * 0.5 ** (_age_days(card["when"], newest) / 21)
    if "/vault_lesson:" in card["text"][:80] and set(sit["phases"]) & {"entry", "stop", "manage"}:
        s += 4          # 2026-10-20: the vault's rules are code-graded evidence across every runner -- on every entry path
        why.append("vault: every runner's record")
    if card["kind"] == "calibration" and why != ["any symbol"]:
        s += 3          # the desk's own graded record goes first: facts about its filters, rebuilt every day
        why.append("graded record")
    if why == ["any symbol"]:
        return 0.0, []           # a symbol-free card that shares nothing with the situation
    return round(s, 2), why


def walk(sit: dict, desk: str = "training", home=HOME, corpus=None) -> list[tuple[float, list[str], dict]]:
    cs = corpus if corpus is not None else cards(desk, home)
    # recency within each KIND of card: a board lesson against the newest board lesson, a paper against the
    # newest paper, a live lesson against the newest live lesson. One shared anchor would mix three calendars --
    # replay days, the days a paper names (any stretch the replay has played), real days -- and on the live
    # desk every training lesson would look a year old, its own order lost
    cal = lambda c: c["kind"]
    newest = {}
    for c in cs:
        if c["when"] and c["when"] > newest.get(cal(c), ""):
            newest[cal(c)] = c["when"]
    ranked = [(sc, why, c) for c in cs for sc, why in [score(c, sit, newest.get(cal(c), ""))] if sc > 0]
    ranked.sort(key=lambda x: x[2]["when"], reverse=True)   # newest first among equal scores (stable sort)
    ranked.sort(key=lambda x: -x[0])
    return ranked


def _interleave(path, symbols):
    """Several symbols in one situation (a decision turn): the best card of each symbol in turn, so one symbol
    with many lessons does not crowd out the others; a card on none of them joins by its own score."""
    if len(symbols) < 2:
        return path
    queues = {s: [] for s in symbols}
    rest = []
    for item in path:
        c = item[2]
        key = c["own_symbol"] if c["own_symbol"] in queues else next((s for s in symbols if s in c["symbols"]), None)
        (queues[key] if key else rest).append(item)
    rr = []
    while any(queues.values()):
        for s in symbols:
            if queues[s]:
                rr.append(queues[s].pop(0))
    out = []
    while rr or rest:
        if rest and (not rr or rest[0][0] > rr[0][0]):
            out.append(rest.pop(0))
        else:
            out.append(rr.pop(0))
    return out


def _head(c: dict, why=None) -> str:
    keys = " ".join(x for x in (" ".join(c["codes"]), " ".join(c["sessions"])) if x)
    return (f"[{c['id']} | {c['kind']} | {', '.join(c['symbols']) or 'any symbol'} | {c['when'][:10] or 'undated'}"
            + (f" | {keys}" if keys else "") + (f" | why here: {'; '.join(why)}" if why else "") + "]")


def card_map(corpus: list[dict]) -> str:
    """How many cards each symbol has: the whole library on one line. `lessons --symbol S` walks any of them."""
    by: dict[str, int] = {}
    for c in corpus:
        k = c["own_symbol"] or (c["symbols"][0] if len(c["symbols"]) == 1 else
                                "several symbols" if c["symbols"] else "any symbol")
        by[k] = by.get(k, 0) + 1
    order = sorted(by, key=lambda k: (k in ("any symbol", "several symbols"), -by[k], k))
    return (" | ".join(f"{k} {by[k]}" for k in order) + f" (total {len(corpus)})") if by else "(no cards yet)"


ALSO_MAX = 12


RULER_VERDICT = re.compile(r"rejected movers|rejections kept|pass was right|kept losers out", re.I)


def skip_verdict(c: dict) -> bool:
    """A card that grades skipping rather than teaching a trade: never in front of an entry decision."""
    return is_calibration(c) or "/vault_lesson:" in c["text"][:80] or bool(RULER_VERDICT.search(c["text"]))


def is_calibration(c: dict) -> bool:
    """A calibration card, or a review's calibration lesson (its text opens with CALIBRATION after the header)."""
    return c["kind"] == "calibration" or bool(re.match(r"\[[^\]]*\] [a-z_]+/[a-z_]+: CALIBRATION\b", c["text"]))


def brief(sit: dict, desk: str = "training", role: str = "decider", home=HOME, cmd: str = "lessons",
          budget: int | None = None, max_full: int | None = None, corpus=None) -> str:
    """The text block a worker gets: matching cards in full (best first) up to the budget, then how many
    others match (named in full only for the CLI and the reviews)."""
    corpus = corpus if corpus is not None else cards(desk, home)
    if role in ENTRY_ROLES:
        corpus = [c for c in corpus if not skip_verdict(c)]
    budget = BUDGET.get(role, 7_000) if budget is None else budget
    max_full = MAX_FULL.get(role, 5) if max_full is None else max_full
    path = _interleave(walk(sit, desk, home, corpus), sit["symbols"])
    desc = ", ".join(x for x in (" ".join(sit["symbols"]), " ".join(sit["codes"]), "/".join(sit["phases"]),
                                 " ".join(sit["sessions"])) if x) or "no keys"
    lines = [f"LESSONS ON THIS PATH ({desc}): the best {MAX_FULL.get(role, 5) if max_full is None else max_full} of "
             f"{len(path)} matching cards, in full ({cmd} --show ID opens another). Earlier reviews' conclusions: "
             "test them against the bars; the bars win."]
    used, shown, rest = sum(map(len, lines)), 0, []
    for _, why, c in path:
        block = _head(c, why) + "\n" + c["text"]
        if shown < max_full and used + len(block) + 1 <= budget:
            lines.append(block)
            used += len(block) + 1
            shown += 1
        else:
            rest.append(f"{c['id']} ({', '.join(c['symbols']) or 'any symbol'}"
                        + (f" {' '.join(c['codes'])}" if c["codes"] else "")
                        + (f" {' '.join(c['sessions'])}" if c["sessions"] else "") + f", {len(c['text'])} ch)")
    if not path:
        lines.append("(no card matches this situation yet)")
    also = ALSO_SHOWN.get(role, 0)
    if rest and also:
        more = len(rest) - also
        lines.append("ALSO ON THIS PATH, not printed (lower rank or over the budget -- open with --show ID): "
                     + "; ".join(rest[:also])
                     + (f"; and {more} weaker match(es) -- {cmd} with the same keys lists them" if more > 0 else ""))
    elif rest:
        lines.append(f"({len(rest)} more on this path: {cmd} --symbol S lists them.)")
    if role == "cli":
        lines.append("THE WHOLE LIBRARY, cards per symbol: " + card_map(corpus))
    return "\n".join(lines)


def show(cid: str, desk: str = "training", home=HOME) -> str:
    for c in cards(desk, home):
        if c["id"].lower() == cid.strip().lower() or c["id"].lower() == f"p:{cid.strip().lower()}":
            return _head(c) + "\n" + c["text"]
    return f"no card {cid!r} on this desk; `lessons --map` lists every id"


def cli(argv, desk: str, home=HOME, cmd: str = "lessons") -> str:
    """`board.py lessons ...` / `live_board.py lessons ...`."""
    import argparse
    p = argparse.ArgumentParser(prog=cmd)
    p.add_argument("--symbol", action="append", default=[])
    p.add_argument("--codes", default="")
    p.add_argument("--phase", action="append", default=[])
    p.add_argument("--session", action="append", default=[])
    p.add_argument("--show")
    p.add_argument("--map", action="store_true")
    p.add_argument("-n", type=int, default=None, help="at most N cards printed in full")
    a = p.parse_args(argv)
    if a.show:
        return show(a.show, desk, home)
    corpus = cards(desk, home)
    if a.map:
        return "THE WHOLE LIBRARY, cards per symbol: " + card_map(corpus) + "\n" + "\n".join(
            _head(c) for c in sorted(corpus, key=lambda c: (c["own_symbol"] or "~", c["when"])))
    sit = situation(a.symbol, a.phase, None, [c.strip().upper() for c in a.codes.split(",") if c.strip()])
    sit["sessions"] = sorted(set(sit["sessions"]) | set(a.session))
    return brief(sit, desk, "cli", home, cmd, max_full=a.n, corpus=corpus)
