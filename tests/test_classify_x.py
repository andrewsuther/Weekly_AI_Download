"""Tests for classify_x."""

from unittest.mock import patch

from score.classify_x import classify, _count_group_matches

MOCK_PATTERNS = {
    "high_signal_groups": [
        {"name": "open_source_release", "patterns": ["open source", "github.com", "released"]},
        {"name": "benchmark_results", "patterns": ["SOTA", "benchmark", "outperforms"]},
    ],
    "failure_signal_groups": [
        {"name": "shutdown_deprecation", "patterns": ["shutting down", "deprecated", "sunset"]},
        {"name": "failed_attempt", "patterns": ["failed", "didn't work", "scrapped"]},
        {"name": "negative_results", "patterns": ["dead end", "overhyped", "doesn't scale"]},
    ],
}


def _tweet(text: str) -> dict:
    return {"text": text}


# ── _count_group_matches ──

class TestCountGroupMatches:
    def test_zero_matches(self):
        assert _count_group_matches("hello world", MOCK_PATTERNS["high_signal_groups"]) == 0

    def test_single_group_match(self):
        assert _count_group_matches("just released on github.com", MOCK_PATTERNS["high_signal_groups"]) == 1

    def test_two_group_matches(self):
        # "open source" -> group 1, "benchmark" -> group 2
        assert _count_group_matches("open source benchmark", MOCK_PATTERNS["high_signal_groups"]) == 2

    def test_case_insensitive(self):
        assert _count_group_matches("OPEN SOURCE RELEASE", MOCK_PATTERNS["high_signal_groups"]) == 1

    def test_multiple_patterns_same_group_count_as_one(self):
        # "open source" and "released" are both in open_source_release group
        assert _count_group_matches("open source released", MOCK_PATTERNS["high_signal_groups"]) == 1


# ── classify ──

class TestClassify:
    @patch("score.classify_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_failure_signal_needs_two_groups(self, _mock):
        # Only "failed" matches one failure group -> not failure_signal
        result = classify(_tweet("the feature failed"), score=0.9)
        assert result != "failure_signal"

    @patch("score.classify_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_failure_signal_two_groups(self, _mock):
        # "shutting down" (group 1) + "failed" (group 2)
        result = classify(_tweet("product shutting down — it failed"), score=0.9)
        assert result == "failure_signal"

    @patch("score.classify_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_failure_signal_overrides_high_signal(self, _mock):
        # Both failure AND high signal keywords present; failure wins
        result = classify(_tweet("shutting down, failed, open source benchmark"), score=0.9)
        assert result == "failure_signal"

    @patch("score.classify_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_high_signal_above_threshold(self, _mock):
        result = classify(_tweet("just released open source on github.com"), score=0.5)
        assert result == "high_signal"

    @patch("score.classify_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_high_signal_below_threshold_is_noise(self, _mock):
        # Score below threshold -> noise even with keyword match
        result = classify(_tweet("just released open source"), score=0.1)
        assert result == "noise"

    @patch("score.classify_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_no_keyword_match_is_noise(self, _mock):
        result = classify(_tweet("having lunch today"), score=0.9)
        assert result == "noise"

    @patch("score.classify_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_exact_threshold_is_high_signal(self, _mock):
        # score == threshold (0.3) should still qualify
        result = classify(_tweet("released on github.com"), score=0.3)
        assert result == "high_signal"
