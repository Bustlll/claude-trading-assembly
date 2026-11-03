"""Routing: an event that happened becomes a demand of some kind.

default_route(event, payload) -> (kind, prompt). An Engine takes its own router
(Engine(router=...)) when a home dir needs richer routing, e.g. the FTMO agent.
The kind decides tools and budget (see engine.KINDS), so an event can never grant
itself more power than its kind has.
"""
import json

EVENT_ROUTES = {
    "research_request": ("research", "{payload}"),
    "work_request": ("work", "{payload}"),
    "trade_closed": ("work",
                     "A trade closed. Event payload:\n{payload}\n"
                     "Judge the decision process, not the outcome alone. Say what should be recorded."),
    "day_closed": ("reflect",
                   "A trading day closed. Event payload:\n{payload}\n"
                   "Write a short day record: result, limits touched, anything to note for tomorrow."),
    "goal_passed": ("reflect",
                    "A goal was verified as passed. Event payload:\n{payload}\n"
                    "Record the evidence and state the next goal to attempt, if any."),
}


def default_route(event: str, payload: dict):
    if event not in EVENT_ROUTES:
        raise ValueError(f"unknown event {event!r}; known: {sorted(EVENT_ROUTES)}")
    kind, template = EVENT_ROUTES[event]
    return kind, template.format(payload=json.dumps(payload, indent=2, sort_keys=True))
