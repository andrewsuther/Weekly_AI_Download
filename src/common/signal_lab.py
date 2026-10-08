"""Offline comparison contract. Synthetic examples, not a live search or benchmark.

This module does not fetch sources, call a model, send email, or modify the
production scoring pipeline. The lab ranks the same fixtures in two views.
"""
from dataclasses import dataclass
from typing import Literal

Persona = Literal["builder", "operator", "learner"]
Topic = Literal["reliability", "privacy", "learning"]
PERSONAS = ("builder", "operator", "learner")
TOPICS = ("reliability", "privacy", "learning")


@dataclass(frozen=True)
class LabRequest:
    persona: Persona
    topic: Topic

    def __post_init__(self):
        if self.persona not in PERSONAS:
            raise ValueError("Unknown persona")
        if self.topic not in TOPICS:
            raise ValueError("Unknown topic")


# Public tutorial URLs are references for the concept, not evidence of the
# fictional reports below. Every example must retain its synthetic label.
FIXTURES = (
    {"id": "retry", "title": "One failed source should not erase the digest",
     "topic": "reliability", "trusted": True, "novelty": 2,
     "fit": {"builder": 3, "operator": 3, "learner": 2},
     "action": "Simulate a source timeout and check the partial-report warning.",
     "reference": "https://docs.python.org/3/library/unittest.mock.html"},
    {"id": "replay", "title": "Repeat a failed case before accepting a fix",
     "topic": "reliability", "trusted": False, "novelty": 3,
     "fit": {"builder": 3, "operator": 2, "learner": 3},
     "action": "Save a failing fixture, fix the bug, then rerun the same fixture.",
     "reference": "https://docs.python.org/3/library/unittest.html"},
    {"id": "boundary", "title": "Check the data boundary before sending",
     "topic": "privacy", "trusted": True, "novelty": 2,
     "fit": {"builder": 2, "operator": 3, "learner": 2},
     "action": "Try a synthetic sensitive input and confirm it stays out of remote calls.",
     "reference": "https://owasp.org/www-project-top-ten/"},
    {"id": "checkpoint", "title": "Resume from state instead of repeating work",
     "topic": "privacy", "trusted": False, "novelty": 3,
     "fit": {"builder": 3, "operator": 2, "learner": 2},
     "action": "Stop a synthetic job midway and confirm its resume repeats no side effects.",
     "reference": "https://docs.python.org/3/library/sqlite3.html"},
    {"id": "diff", "title": "A correction needs a reason, not just a thumbs-up",
     "topic": "learning", "trusted": True, "novelty": 2,
     "fit": {"builder": 2, "operator": 3, "learner": 3},
     "action": "Compare a draft with an edited version and record the changed field.",
     "reference": "https://docs.python.org/3/library/difflib.html"},
    {"id": "holdout", "title": "Test the rule on an example it has not seen",
     "topic": "learning", "trusted": False, "novelty": 3,
     "fit": {"builder": 3, "operator": 2, "learner": 3},
     "action": "Hold one example aside before checking whether a proposed rule helps.",
     "reference": "https://scikit-learn.org/stable/modules/cross_validation.html"},
)


def compare(request: LabRequest) -> dict:
    """Return deterministic rankings and reasons. Do not imply measured quality."""
    rows = []
    for item in FIXTURES:
        if item["topic"] != request.topic:
            continue
        fit = item["fit"][request.persona]
        rows.append({**item, "fit": fit, "synthetic": True,
                     "reason": f"{request.persona} fit {fit}/3; illustrative scores only"})
    trusted = sorted(rows, key=lambda r: (-int(r["trusted"]), -r["fit"], r["id"]))
    discovery = sorted(rows, key=lambda r: (-r["novelty"], -r["fit"], r["id"]))
    return {"mode": "synthetic", "request": {"persona": request.persona, "topic": request.topic},
            "trusted": trusted, "discovery": discovery,
            "limits": "Same fixtures, different ranking. No live retrieval or model evaluation."}
