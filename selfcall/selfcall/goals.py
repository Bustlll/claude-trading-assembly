"""Reusable goal harness: keep calling Claude, round after round, until a verifier says the goal is met.

A Goal is four things, and none of them is specific to any one agent:
  objective   the standing instruction every round carries
  verify      function(home) -> (done: bool, evidence: str). Reads files, never the model's word.
  advance     optional function(home) -> list of (event, payload). Moves the world forward
              (a replay turn, a test run, a data pull) and returns what happened. Events are
              routed by the home's router, so they become demands on their own.
  kind        which kind of demand each round is (its tools and model come from the home's policy)

Each round: advance the world, route its events, enqueue one round demand that carries the
objective, the current evidence and the last summary, then drain. Follow-ups an answer spawns
run inside the round under the engine's policy. Stops when the verifier passes, a cap is hit,
or the engine halts (STOP file, cost cap, run cap).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Goal:
    name: str
    objective: str
    verify: Callable[[Path], tuple[bool, str]]
    advance: Optional[Callable[[Path], list]] = None
    kind: str = "work"
    max_rounds: int = 10
    seed: str = ""


@dataclass
class Result:
    status: str                      # met | not_met | halted
    rounds: int
    evidence: str
    history: list = field(default_factory=list)


def _summary(home: Path, demand_id: str) -> tuple[str, str]:
    p = home / "runs" / f"{demand_id}.json"
    if not p.exists():
        return "", "no record"
    rec = json.loads(p.read_text())
    ans = rec.get("answer") or {}
    return ans.get("summary") or rec.get("error") or "", rec.get("status", "")


def run_goal(engine, goal: Goal) -> Result:
    history: list[dict] = []
    last_summary = ""
    for rnd in range(1, goal.max_rounds + 1):
        if goal.advance is not None:
            for event, payload in goal.advance(engine.home):
                engine.emit(event, payload)
        done, evidence = goal.verify(engine.home)
        if done:
            return Result("met", rnd - 1, evidence, history)
        why = engine.halt_reason()
        if why:
            return Result("halted", rnd - 1, f"{evidence} | halted: {why}", history)
        prompt = (f"GOAL: {goal.name}\nOBJECTIVE: {goal.objective}\n"
                  f"ROUND {rnd} of {goal.max_rounds}\n"
                  f"CURRENT EVIDENCE (computed by the program, not by you):\n{evidence}\n"
                  f"LAST ROUND SUMMARY: {last_summary or '(first round)'}\n"
                  + (f"SEED:\n{goal.seed}\n" if rnd == 1 and goal.seed else "")
                  + "Take the single most valuable next step with your tools. Propose follow-ups only when required.")
        did = engine.enqueue(goal.kind, prompt, source=f"goal:{goal.name}")
        what, detail = engine.drain()
        summary, status = _summary(engine.home, did)
        last_summary = summary
        history.append({"round": rnd, "demand": did, "status": status, "summary": summary[:300]})
        if what == "halted":
            return Result("halted", rnd, f"{evidence} | halted: {detail}", history)
    done, evidence = goal.verify(engine.home)
    return Result("met" if done else "not_met", goal.max_rounds, evidence, history)
