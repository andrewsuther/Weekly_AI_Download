"""Keyword + pattern classifier -> high_signal / failure_signal / noise."""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
KEYWORDS_PATH = CONFIG_DIR / "x_keywords.yaml"


def _load_patterns() -> dict:
    """Load pattern config from x_keywords.yaml."""
    with open(KEYWORDS_PATH) as f:
        return yaml.safe_load(f)


def _count_group_matches(text: str, groups: list[dict]) -> int:
    """
    Count how many pattern GROUPS have at least one match in text.
    Multiple patterns hitting the same group still count as one.
    """
    text_lower = text.lower()
    return sum(
        1 for group in groups
        if any(p.lower() in text_lower for p in group["patterns"])
    )


def classify(tweet: dict, score: float = 0.0, score_threshold: float = 0.3) -> str:
    """
    Classify a single tweet.

    Rules (evaluated in priority order):
        1. failure_signal — 2+ distinct failure_signal group matches
        2. high_signal   — 1+ high_signal group match AND score >= threshold
        3. noise         — everything else (will be dropped from report)

    The 2-group requirement for failure_signal prevents single-word
    false positives (e.g. "exploit" in a neutral context).
    """
    patterns = _load_patterns()
    text = tweet.get("text", "")

    # Priority 1: failure detection (requires 2 distinct groups)
    failure_matches = _count_group_matches(text, patterns.get("failure_signal_groups", []))
    if failure_matches >= 2:
        return "failure_signal"

    # Priority 2: high signal (1 group + score gate)
    high_matches = _count_group_matches(text, patterns.get("high_signal_groups", []))
    if high_matches >= 1 and score >= score_threshold:
        return "high_signal"

    return "noise"
