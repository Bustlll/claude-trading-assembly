"""Tests for the guard: policy.py builds a real, guard-enforced per-symbol boundary for the
symbol-trader kinds, not just a convention. The permission check itself is Claude Code's, so
what we can and must verify here is policy.py's OWN side: each symbol's rule names only that
symbol, one kind never accidentally inherits another's, and the spawn graph stays bounded."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guard import policy as P  # noqa: E402


class SymbolTraderScopeTest(unittest.TestCase):
    def test_every_configured_symbol_has_its_own_kind(self):
        for sym in P.SYMBOL_TRADERS:
            self.assertIn(f"trade_{sym.lower().replace('.', '_')}", P.KINDS)

    def test_a_symbols_rule_never_names_another_symbol(self):
        for sym in P.SYMBOL_TRADERS:
            kind = P.KINDS[f"trade_{sym.lower().replace('.', '_')}"]
            order_close_modify = [a for a in kind["allow"] if a.split("(")[0] == "Bash"
                                  and any(v in a for v in (" order ", " close ", " modify "))]
            self.assertTrue(order_close_modify, f"{sym} kind has no order/close/modify rule at all")
            for other in P.SYMBOL_TRADERS:
                if other == sym:
                    continue
                for rule in order_close_modify:
                    self.assertNotIn(f"--symbol {other} ", rule + " ",
                                      f"{sym}'s rule {rule!r} also names {other}")

    def test_each_symbol_kind_only_opens_closes_modifies_its_own_symbol(self):
        for sym in P.SYMBOL_TRADERS:
            kind = P.KINDS[f"trade_{sym.lower().replace('.', '_')}"]
            for verb in ("order", "close", "modify"):
                rule = next(a for a in kind["allow"] if f" {verb} --symbol {sym} " in a + " ")
                self.assertIn(f"--symbol {sym} ", rule + " ")

    def test_reading_is_not_symbol_restricted(self):
        """Looking at another symbol's price/status is not a safety question -- only moving
        money in it is -- so status/bars/symbols/goals/fitness carry no --symbol restriction."""
        kind = P.KINDS["trade_eurusd"]
        self.assertTrue(any(a == f"Bash(python3 {P.PAPER} bars*)" for a in kind["allow"]))
        self.assertTrue(any(a == f"Bash(python3 {P.PAPER} status*)" for a in kind["allow"]))

    def test_symbol_traders_may_delegate_analysis_but_not_fan_out_into_more_traders(self):
        for sym in P.SYMBOL_TRADERS:
            kind = P.KINDS[f"trade_{sym.lower().replace('.', '_')}"]
            # no "review" since 2026-10-18: a trader's review child always found its order still unfilled
            # (frozen clock); trade_closed routes the real review
            self.assertEqual(set(kind["may_spawn"]), {"scout", "chart", "timing"})

    def test_trade_review_and_scout_can_each_hand_off_to_every_symbol_trader(self):
        for parent in ("trade", "review", "scout"):
            for sym in P.SYMBOL_TRADERS:
                self.assertIn(f"trade_{sym.lower().replace('.', '_')}", P.KINDS[parent]["may_spawn"])

    def test_no_kind_may_spawn_opus_at_max_effort(self):
        """policy_check in the engine refuses this at call time; this is the static half of the
        same guarantee -- no kind table entry here should even try to configure it."""
        for name, kind in P.KINDS.items():
            if str(kind.get("model", "")).lower().find("opus") >= 0:
                self.assertNotEqual(kind.get("effort"), "max", f"{name} configures opus at max effort")

    def test_the_spawn_graph_fits_inside_max_depth(self):
        """trade(0) -> scout(1) -> trade_<symbol>(2) -> review(3) is the deepest real chain this
        roster creates; max_depth must be at least 3 for it not to be silently cut short."""
        self.assertGreaterEqual(P.LIMITS["max_depth"], 3)

    def test_max_children_fits_trade_or_chart_timing_scout_handoff(self):
        self.assertGreaterEqual(P.LIMITS["max_children"], 3)

    def test_training_has_no_cumulative_run_or_cost_cap(self):
        """Operator 2026-10-11: subscription usage, not API billing; stopping is by hand, not a cap."""
        self.assertGreaterEqual(P.LIMITS["max_runs"], 10**8)
        self.assertGreaterEqual(P.LIMITS["max_total_cost_usd"], 1e8)
        for name, kind in P.KINDS.items():  # a single call is still bounded
            self.assertLessEqual(kind["budget_usd"], 1.0, name)


class RiskKindTest(unittest.TestCase):
    """The per-trade risk lever is a real guard-enforced privilege, not a convention: only the
    dedicated 'risk' kind may call paper.py risk-factor --set, even though 'trade' still needs
    nearly everything else paper.py offers."""

    def test_only_the_risk_kind_can_set_the_risk_factor(self):
        owners = [k for k, v in P.KINDS.items() if any("risk-factor" in a for a in v.get("allow", []))]
        self.assertEqual(owners, ["risk"])

    def test_the_risk_kind_cannot_place_close_or_modify_orders(self):
        kind = P.KINDS["risk"]
        for verb in (" order", " close", " modify", " journal"):
            self.assertFalse(any(verb in a for a in kind["allow"]),
                              f"risk kind should not be able to {verb.strip()}")

    def test_trade_lost_its_old_blanket_paper_wildcard(self):
        """Regression guard: a bare Bash(python3 PAPER *) on trade would silently re-grant it
        risk-factor (or any future paper.py subcommand) the moment one is added."""
        self.assertNotIn(f"Bash(python3 {P.PAPER} *)", P.KINDS["trade"]["allow"])

    def test_trade_still_has_every_subcommand_it_actually_needs(self):
        needed = ("status", "field", "symbols", "bars", "order", "close", "modify", "journal",
                  "goals", "fitness", "ledger", "attempts")
        allow = P.KINDS["trade"]["allow"]
        for verb in needed:
            self.assertTrue(any(a == f"Bash(python3 {P.PAPER} {verb}*)" for a in allow), verb)

    def test_review_and_only_review_may_hand_off_to_risk(self):
        spawners = [k for k, v in P.KINDS.items() if "risk" in v.get("may_spawn", [])]
        # 2026-10-19 (operator): the position manager asks risk for a widen -- the one decision risk owns per trade
        self.assertEqual(sorted(spawners), ["manage", "review"])


class LiveKindTest(unittest.TestCase):
    """The LIVE worker set (2026-10-11): its own kind table, its own tools. A TRAIN kind can never
    touch the real account; a LIVE kind can never read the replay (paper.py, the training board),
    which would mean deciding real orders from backtest data."""
    LIVE_TOOLS = ("live_broker.py", "live_market.py", "live_board.py")

    def test_no_training_kind_can_reach_any_live_tool(self):
        for name, kind in P.KINDS.items():
            for rule in kind.get("allow", []):
                self.assertFalse(any(t in rule for t in self.LIVE_TOOLS), f"{name}: {rule}")

    def test_no_live_kind_can_reach_paper_or_the_training_board(self):
        for name, kind in P.LIVE_KINDS.items():
            for rule in kind.get("allow", []):
                self.assertNotIn("paper.py", rule, f"{name}: {rule}")
                self.assertNotIn("/tools/board.py", rule, f"{name}: {rule}")

    def test_the_tables_are_disjoint_except_the_tool_less_helpers(self):
        shared = set(P.KINDS) & set(P.LIVE_KINDS)
        self.assertEqual(shared, {"reflect", "write"})
        for k in shared:  # neither can touch a market at all
            self.assertFalse(any(t in a for a in P.KINDS[k].get("allow", []) for t in ("paper.py", *self.LIVE_TOOLS)))

    def test_only_the_decider_and_the_live_symbol_traders_can_move_money(self):
        movers = sorted(k for k, v in P.LIVE_KINDS.items()
                        if any(f"live_broker.py {verb}" in a for a in v.get("allow", [])
                               for verb in ("order", "close", "modify")))
        # 2026-10-19: the position manager may close and modify (never order)
        self.assertEqual(movers, sorted(["live", "live_manage", *P.LIVE_SYMBOL_TRADER_KINDS]))
        orderers = sorted(k for k, v in P.LIVE_KINDS.items() if any("live_broker.py order" in a for a in v.get("allow", [])))
        self.assertEqual(orderers, sorted(["live", *P.LIVE_SYMBOL_TRADER_KINDS]))

    def test_only_the_risk_workers_may_widen_a_stop(self):
        self.assertEqual([k for k, v in P.LIVE_KINDS.items() if any("live_broker.py widen" in a for a in v.get("allow", []))],
                         ["live_risk"])
        self.assertEqual([k for k, v in P.KINDS.items() if any("paper.py widen" in a for a in v.get("allow", []))],
                         ["risk"])

    def test_consult_is_pinned_to_its_own_desk(self):
        for k, v in P.LIVE_KINDS.items():
            self.assertFalse(any("consult.py training" in a for a in v.get("allow", [])), k)
        for k, v in P.KINDS.items():
            self.assertFalse(any("consult.py live" in a for a in v.get("allow", [])), k)
        for k in ("chart", "timing"):   # a specialist never consults another (no consult loops)
            self.assertFalse(any("consult.py" in a for a in P.KINDS[k].get("allow", [])))
            self.assertFalse(any("consult.py" in a for a in P.LIVE_KINDS[f"live_{k}"].get("allow", [])))

    def test_each_live_trader_only_orders_closes_modifies_its_own_symbol(self):
        for sym in P.SYMBOL_TRADERS:
            kind = P.LIVE_KINDS[f"live_trade_{sym.lower().replace('.', '_')}"]
            moving = [a for a in kind["allow"] if any(f"live_broker.py {v} " in a for v in ("order", "close", "modify"))]
            self.assertEqual(len(moving), 3)
            for rule in moving:
                self.assertIn(f"--symbol {sym} ", rule + " ")
                for other in P.SYMBOL_TRADERS:
                    if other != sym:
                        self.assertNotIn(f"--symbol {other} ", rule + " ")

    def test_live_spawns_stay_inside_the_live_table_and_training_spawns_inside_training(self):
        for name, kind in P.LIVE_KINDS.items():
            for child in kind["may_spawn"]:
                self.assertIn(child, P.LIVE_KINDS, f"live kind {name} may spawn unknown/training kind {child}")
        for name, kind in P.KINDS.items():
            for child in kind["may_spawn"]:
                self.assertIn(child, P.KINDS, f"training kind {name} may spawn {child} outside its table")

    def test_no_code_changes_and_no_training_kinds_while_live(self):
        for k in ("evolve", "risk", "verify", "research", "trade", "review", "scout"):
            self.assertNotIn(k, P.LIVE_KINDS)

    def test_only_live_risk_can_set_the_live_lever_and_it_cannot_trade(self):
        owners = [k for k, v in P.LIVE_KINDS.items() if any("live_broker.py risk-factor" in a for a in v.get("allow", []))]
        self.assertEqual(owners, ["live_risk"])
        allow = P.LIVE_KINDS["live_risk"]["allow"]
        for verb in ("order", "close", "modify", "journal"):
            self.assertFalse(any(f"live_broker.py {verb}" in a for a in allow), verb)
        self.assertNotIn("paper.py", " ".join(allow))  # the paper lever is not its to touch

    def test_reviews_can_raise_a_risk_question_and_scouts_can_wake_the_decider(self):
        self.assertIn("live_risk", P.LIVE_KINDS["live_review"]["may_spawn"])
        self.assertIn("live", P.LIVE_KINDS["live_scout"]["may_spawn"])

    def test_live_review_cannot_open_a_trade(self):
        spawn = P.LIVE_KINDS["live_review"]["may_spawn"]
        self.assertNotIn("live", spawn)
        self.assertFalse(any(k.startswith("live_trade_") for k in spawn))

    def test_each_live_kind_matches_its_training_twin(self):
        """Same model, effort and budget as what was trained -- only the tools differ."""
        twins = {"live": "trade", "live_scout": "scout", "live_chart": "chart", "live_timing": "timing",
                 "live_review": "review", "live_risk": "risk"}
        twins.update({f"live_trade_{s.lower().replace('.', '_')}": f"trade_{s.lower().replace('.', '_')}"
                      for s in P.SYMBOL_TRADERS})
        for live, train in twins.items():
            for key in ("model", "effort", "budget_usd"):
                self.assertEqual(P.LIVE_KINDS[live][key], P.KINDS[train][key], f"{live}.{key}")

    def test_no_live_kind_is_opus_at_max(self):
        for name, kind in P.LIVE_KINDS.items():
            self.assertNotEqual((kind["model"], kind.get("effort")), ("opus", "max"), name)

    def test_live_limits_fit_the_live_chain_and_only_a_runaway_guard_binds(self):
        # live(0) -> live_scout(1) -> live_trade_<sym>(2) -> live_review(3)
        self.assertGreaterEqual(P.LIVE_LIMITS["max_depth"], 3)
        self.assertGreater(P.LIVE_DAILY_MAX_CALLS, 100)  # a normal day is ~25-60 calls
        self.assertFalse(hasattr(P, "LIVE_DAILY_BUDGET_USD"))  # spend is reported, never a stop (operator)
        self.assertGreaterEqual(P.LIVE_LIMITS["max_runs"], 10**6)  # the cumulative caps must never end live

    def test_live_notes_never_teach_paper_syntax(self):
        for name, kind in P.LIVE_KINDS.items():
            if name in ("reflect", "write", "live_dream"):   # no market tools at all
                continue
            self.assertNotIn("paper.py's subcommands", kind.get("notes", ""), name)
            self.assertIn("live_market.py's subcommands", kind.get("notes", ""), name)


class DreamPermissionTest(unittest.TestCase):
    """Live -> training only as dreams: live_dream may PROPOSE (through the checks) and nothing else;
    training owns them -- every knowledge-reading kind reads, review/write/evolve also edit; no kind
    anywhere has a raw Edit on knowledge/, and no training kind can read state/ (the provenance)."""

    def _dream_verbs(self, kind):
        return {a.split("dreams.py ")[1].split("*")[0] for a in kind.get("allow", []) if "dreams.py" in a}

    def test_live_dream_only_proposes_and_reads_and_cannot_trade(self):
        k = P.LIVE_KINDS["live_dream"]
        self.assertEqual(self._dream_verbs(k), {"propose", "read"})
        self.assertEqual(k["tools"], ["Bash"])
        self.assertEqual(k["may_spawn"], [])
        self.assertFalse(any(t in a for a in k["allow"] for t in ("live_broker", "live_market", "paper.py", "board")))

    def test_no_other_live_kind_touches_dreams(self):
        for name, k in P.LIVE_KINDS.items():
            if name != "live_dream":
                self.assertEqual(self._dream_verbs(k), set(), name)

    def test_only_live_dream_can_propose(self):
        for table in (P.KINDS, P.LIVE_KINDS):
            for name, k in table.items():
                if "propose" in self._dream_verbs(k):
                    self.assertEqual(name, "live_dream")

    def test_training_reads_them_and_its_reflective_kinds_shape_them(self):
        for name in ("trade", "scout", "chart", "timing", "risk", *P.SYMBOL_TRADER_KINDS):
            self.assertEqual(self._dream_verbs(P.KINDS[name]), {"read"}, name)
        for name in ("review", "write", "evolve"):
            self.assertEqual(self._dream_verbs(P.KINDS[name]), {"read", "note", "revise", "retire"}, name)

    def test_no_kind_has_a_raw_edit_on_knowledge_and_no_training_kind_reads_state(self):
        for table in (P.KINDS, P.LIVE_KINDS):
            for name, k in table.items():
                self.assertFalse(any(a.startswith("Edit(") and "/knowledge" in a for a in k.get("allow", [])), name)
        for name, k in P.KINDS.items():
            self.assertFalse(any(a.startswith("Read(") and "/state" in a for a in k.get("allow", [])), name)

if __name__ == "__main__":
    unittest.main()
