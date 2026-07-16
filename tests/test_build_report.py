"""Tests for build_report."""

from datetime import datetime, timezone

from report.build_report import (
    build_report,
    _truncate,
    _x_signal_block,
    _failure_signal_block,
    _categorize_high_signals,
)


# ── _truncate ──

class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello world", 150) == "hello world"

    def test_long_text_truncated_with_ellipsis(self):
        text = "word " * 100  # 500 chars
        result = _truncate(text, 50)
        assert len(result) <= 51  # content + ellipsis
        assert result.endswith("\u2026")

    def test_truncation_at_word_boundary(self):
        text = "aaa bbb ccc ddd eee fff ggg"
        result = _truncate(text, 12)
        # Should not cut mid-word
        assert "\u2026" in result
        assert "aaa" in result
        # Verify no partial word before ellipsis
        before_ellipsis = result.rstrip("\u2026")
        assert before_ellipsis == before_ellipsis.rstrip()


# ── _x_signal_block ──

class TestXSignalBlock:
    def test_basic_format(self):
        tweet = {
            "id": "123456",
            "text": "Just shipped a new feature!",
            "author": {"username": "testuser"},
        }
        result = _x_signal_block(tweet)
        assert "https://x.com/testuser/status/123456" in result
        assert "@testuser" in result
        assert "Just shipped a new feature!" in result

    def test_missing_author_defaults_unknown(self):
        tweet = {"id": "789", "text": "orphan tweet", "author": {}}
        result = _x_signal_block(tweet)
        assert "@unknown" in result

    def test_long_text_truncated(self):
        tweet = {
            "id": "111",
            "text": "x " * 200,  # 400 chars
            "author": {"username": "u"},
        }
        result = _x_signal_block(tweet)
        assert "\u2026" in result


# ── _failure_signal_block ──

class TestFailureSignalBlock:
    def test_includes_annotation(self):
        tweet = {
            "id": "222",
            "text": "Product shutting down",
            "author": {"username": "insider"},
            "why_it_matters": "Signals market consolidation.",
        }
        result = _failure_signal_block(tweet)
        assert "Why it matters: Signals market consolidation." in result
        assert "\u21b3" in result

    def test_missing_annotation_no_arrow(self):
        tweet = {
            "id": "333",
            "text": "Something failed",
            "author": {"username": "dev"},
        }
        result = _failure_signal_block(tweet)
        assert "Why it matters" not in result


# ── _categorize_high_signals ──

class TestCategorizeHighSignals:
    def test_oss_keyword_buckets_to_oss(self):
        tweets = [{"text": "New open source model on huggingface!", "id": "1", "author": {"username": "a"}}]
        result = _categorize_high_signals(tweets)
        assert len(result["oss_tools"]) == 1
        assert len(result["industry"]) == 0

    def test_industry_keyword_buckets_to_industry(self):
        tweets = [{"text": "Company just launched product in production", "id": "2", "author": {"username": "b"}}]
        result = _categorize_high_signals(tweets)
        assert len(result["industry"]) == 1
        assert len(result["oss_tools"]) == 0

    def test_tie_defaults_to_oss(self):
        # No keywords at all -> oss_score == ind_score == 0 -> oss (>=)
        tweets = [{"text": "something neutral here", "id": "3", "author": {"username": "c"}}]
        result = _categorize_high_signals(tweets)
        assert len(result["oss_tools"]) == 1


# ── build_report (integration) ──

def _sample_analysis() -> dict:
    return {
        "tiered_papers": {
            1: [{
                "title": "Paradigm Paper",
                "authors": ["Author A", "Author B"],
                "published": "2026-02-01T00:00:00Z",
                "url": "https://arxiv.org/abs/2602.00001",
                "domain": "AI Foundations",
                "analysis": "- **Core idea:** Revolutionary approach\n- **Technique:** New method",
            }],
            2: [{
                "title": "Tool Paper",
                "authors": ["Author C"],
                "published": "2026-01-31T00:00:00Z",
                "url": "https://arxiv.org/abs/2601.99999",
                "domain": "ML",
                "analysis": "- **Problem + solution:** Solves X elegantly",
            }],
            3: [],
        },
        "trends": "**Spreading techniques:** Transformers appearing everywhere.",
        "takeaway": "Small efficient models beat brute-force scaling.",
        "failure_signals": [{
            "id": "999",
            "text": "That approach failed completely",
            "author": {"username": "skeptic"},
            "why_it_matters": "Avoid this dead-end pattern.",
        }],
    }


def _sample_high_signal() -> list[dict]:
    return [
        {"id": "501", "text": "New open source LLM released!", "author": {"username": "ml_researcher"}, "score": 0.85},
        {"id": "502", "text": "Company launched enterprise product", "author": {"username": "biz_lead"}, "score": 0.72},
    ]


class TestBuildReport:
    def test_contains_yaml_frontmatter(self):
        md = build_report(_sample_analysis(), _sample_high_signal())
        assert md.startswith("---\n")
        assert "generated_by: Weekly AI Download" in md
        assert "date:" in md
        assert "papers_analyzed:" in md

    def test_contains_all_tier_sections(self):
        md = build_report(_sample_analysis(), _sample_high_signal())
        assert "## Tier 1: Paradigm Shifters" in md
        assert "## Tier 2: High-Leverage Tools" in md
        assert "## Tier 3: Tactical Enhancements" in md

    def test_empty_tier_shows_placeholder(self):
        md = build_report(_sample_analysis(), _sample_high_signal())
        # Tier 3 is empty in sample
        assert "*No Tier 3 papers this week.*" in md

    def test_contains_trends_section(self):
        md = build_report(_sample_analysis(), _sample_high_signal())
        assert "## Emerging Trends & Themes" in md
        assert "Transformers appearing everywhere" in md

    def test_contains_x_signal_links(self):
        md = build_report(_sample_analysis(), _sample_high_signal())
        assert "https://x.com/ml_researcher/status/501" in md
        assert "https://x.com/biz_lead/status/502" in md

    def test_contains_arxiv_links(self):
        md = build_report(_sample_analysis(), _sample_high_signal())
        assert "https://arxiv.org/abs/2602.00001" in md
        assert "[Read on arXiv" in md

    def test_contains_failure_signal_annotation(self):
        md = build_report(_sample_analysis(), _sample_high_signal())
        assert "Avoid this dead-end pattern." in md
        assert "\u21b3 Why it matters:" in md

    def test_contains_takeaway(self):
        md = build_report(_sample_analysis(), _sample_high_signal())
        assert "Small efficient models beat brute-force scaling." in md

    def test_custom_date_in_header(self):
        dt = datetime(2026, 3, 15, tzinfo=timezone.utc)
        md = build_report(_sample_analysis(), _sample_high_signal(), date=dt)
        assert "March 15, 2026" in md
        assert "date: 2026-03-15" in md

    def test_all_empty_inputs(self):
        empty = {
            "tiered_papers": {1: [], 2: [], 3: []},
            "trends": "",
            "takeaway": "",
            "failure_signals": [],
        }
        md = build_report(empty, [])
        assert "*No Tier 1 papers this week.*" in md
        assert "*No OSS signals this week.*" in md
        assert "*No failure signals this week.*" in md
