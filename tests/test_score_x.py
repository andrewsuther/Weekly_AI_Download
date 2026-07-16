"""Tests for score_x."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from score.score_x import (
    score_tweet,
    _engagement_score,
    _time_decay_score,
    _social_graph_score,
    _snr_score,
)

MOCK_WEIGHTS = {
    "composite_weights": {
        "engagement": 0.45,
        "time_decay": 0.15,
        "social_graph": 0.25,
        "snr": 0.15,
    },
    "engagement_weights": {
        "reply_count": 13.5,
        "like_count": 0.5,
        "retweet_count": 1.0,
        "bookmark_count": 3.0,
        "quote_tweet_count": 2.0,
    },
    "engagement_velocity_multiplier": 1.5,
    "time_decay_half_life_hours": 6,
    "social_graph_exponent": 0.3,
    "social_graph_log_base": 10,
    "verified_boost": 1.4,
    "snr_min_total_engagements": 5,
    "snr_reply_like_ratio_threshold": 0.05,
    "snr_low_ratio_penalty": 0.3,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# ── Engagement ──

class TestEngagementScore:
    def test_zero_engagements(self):
        metrics = {"reply_count": 0, "like_count": 0, "retweet_count": 0}
        assert _engagement_score(metrics, MOCK_WEIGHTS, 1.0) == 0.0

    def test_replies_weighted_higher_than_likes(self):
        reply_score = _engagement_score(
            {"reply_count": 10, "like_count": 0, "retweet_count": 0}, MOCK_WEIGHTS, 1.0
        )
        like_score = _engagement_score(
            {"reply_count": 0, "like_count": 10, "retweet_count": 0}, MOCK_WEIGHTS, 1.0
        )
        assert reply_score > like_score

    def test_missing_fields_default_zero(self):
        # Should not raise
        score = _engagement_score({}, MOCK_WEIGHTS, 1.0)
        assert score == 0.0

    def test_score_capped_at_one(self):
        # Absurdly high engagement
        metrics = {"reply_count": 1000000, "like_count": 1000000, "retweet_count": 1000000}
        assert _engagement_score(metrics, MOCK_WEIGHTS, 0.01) <= 1.0


# ── Time Decay ──

class TestTimeDecay:
    def test_fresh_tweet(self):
        assert _time_decay_score(0.1, 6) > 0.9

    def test_old_tweet(self):
        assert _time_decay_score(24, 6) < 0.1

    def test_at_half_life(self):
        assert abs(_time_decay_score(6, 6) - 0.5) < 0.001


# ── Social Graph ──

class TestSocialGraph:
    def test_zero_followers(self):
        author = {"public_metrics": {"followers_count": 0}, "verified": False}
        assert _social_graph_score(author, MOCK_WEIGHTS) >= 0.0

    def test_verified_boost(self):
        base = {"public_metrics": {"followers_count": 10000}, "verified": False}
        verified = {"public_metrics": {"followers_count": 10000}, "verified": True}
        assert _social_graph_score(verified, MOCK_WEIGHTS) > _social_graph_score(base, MOCK_WEIGHTS)

    def test_mega_account_dampened(self):
        # 100 vs 1M followers: ratio should be far less than 10000x due to log dampening
        small = _social_graph_score(
            {"public_metrics": {"followers_count": 100}, "verified": False}, MOCK_WEIGHTS
        )
        huge = _social_graph_score(
            {"public_metrics": {"followers_count": 1_000_000}, "verified": False}, MOCK_WEIGHTS
        )
        assert huge / max(small, 0.001) < 100

    def test_missing_public_metrics(self):
        # Should not raise
        score = _social_graph_score({}, MOCK_WEIGHTS)
        assert score >= 0.0


# ── SNR ──

class TestSNR:
    def test_below_min_total_is_zero(self):
        # total = 3 < 5
        metrics = {"reply_count": 1, "like_count": 2, "retweet_count": 0}
        assert _snr_score(metrics, MOCK_WEIGHTS) == 0.0

    def test_low_reply_ratio_penalized(self):
        # 100 likes, 1 reply -> ratio 0.01 < 0.05
        metrics = {"reply_count": 1, "like_count": 100, "retweet_count": 5}
        assert _snr_score(metrics, MOCK_WEIGHTS) == MOCK_WEIGHTS["snr_low_ratio_penalty"]

    def test_healthy_ratio_is_one(self):
        # 50 likes, 10 replies -> ratio 0.2 > 0.05
        metrics = {"reply_count": 10, "like_count": 50, "retweet_count": 5}
        assert _snr_score(metrics, MOCK_WEIGHTS) == 1.0


# ── Composite score_tweet ──

class TestScoreTweet:
    @patch("score.score_x._load_weights", return_value=MOCK_WEIGHTS)
    def test_low_engagement_tweet_scores_zero(self, _mock):
        # total engagements = 2 < SNR floor of 5
        tweet = {
            "public_metrics": {"reply_count": 1, "like_count": 1, "retweet_count": 0},
            "author": {"public_metrics": {"followers_count": 100}, "verified": False},
            "created_at": _now_iso(),
        }
        assert score_tweet(tweet) == 0.0

    @patch("score.score_x._load_weights", return_value=MOCK_WEIGHTS)
    def test_high_engagement_tweet_positive_score(self, _mock):
        tweet = {
            "public_metrics": {"reply_count": 50, "like_count": 1000, "retweet_count": 200},
            "author": {"public_metrics": {"followers_count": 500000}, "verified": True},
            "created_at": _now_iso(),
        }
        assert score_tweet(tweet) > 0.3

    @patch("score.score_x._load_weights", return_value=MOCK_WEIGHTS)
    def test_missing_created_at_uses_default(self, _mock):
        tweet = {
            "public_metrics": {"reply_count": 10, "like_count": 50, "retweet_count": 5},
            "author": {"public_metrics": {"followers_count": 1000}, "verified": False},
            # no created_at
        }
        score = score_tweet(tweet)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    @patch("score.score_x._load_weights", return_value=MOCK_WEIGHTS)
    def test_score_always_in_unit_range(self, _mock):
        tweet = {
            "public_metrics": {"reply_count": 500, "like_count": 5000, "retweet_count": 1000},
            "author": {"public_metrics": {"followers_count": 2_000_000}, "verified": True},
            "created_at": _now_iso(),
        }
        score = score_tweet(tweet)
        assert 0.0 <= score <= 1.0


# ── Content-Based Scoring ──

MOCK_CONTENT_WEIGHTS = {
    **MOCK_WEIGHTS,
    "content_weights": {
        "keyword_relevance": 0.5,
        "length": 0.2,
        "url_bonus": 0.2,
        "time_decay": 0.1,
    },
    "content_min_length": 50,
    "content_optimal_length": 140,
}

MOCK_PATTERNS = {
    "high_signal_groups": [
        {"patterns": ["open source", "github", "code release"]},
        {"patterns": ["benchmark", "SOTA", "state of the art"]},
        {"patterns": ["research paper", "arxiv", "publication"]},
    ]
}


class TestContentBasedScoring:
    @patch("score.score_x._load_weights", return_value=MOCK_CONTENT_WEIGHTS)
    @patch("score.score_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_content_scoring_when_metrics_zero(self, mock_patterns, mock_weights):
        """Should use content-based path when all metrics are zero."""
        tweet = {
            "text": "Just released our new open source model on github! Benchmark results show SOTA performance.",
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        score = score_tweet(tweet)

        # Should use content-based path and score > 0 due to keywords
        assert score > 0.3  # Has 2 keyword groups + length + URL

    @patch("score.score_x._load_weights", return_value=MOCK_CONTENT_WEIGHTS)
    @patch("score.score_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_content_scoring_with_missing_metrics(self, mock_patterns, mock_weights):
        """Should use content-based path when metrics dict is absent."""
        tweet = {
            "text": "New research paper on arxiv about state of the art transformer models https://arxiv.org/abs/123",
            # No public_metrics at all
            "created_at": _now_iso(),
        }

        score = score_tweet(tweet)

        # Should use content-based path
        assert score > 0.4  # Has 3 keyword groups + URL + length

    @patch("score.score_x._load_weights", return_value=MOCK_WEIGHTS)
    def test_engagement_scoring_when_metrics_present(self, mock_weights):
        """Should use engagement-based path when metrics are present."""
        tweet = {
            "text": "test",
            "public_metrics": {"reply_count": 10, "like_count": 50, "retweet_count": 5},
            "author": {"public_metrics": {"followers_count": 1000}, "verified": False},
            "created_at": _now_iso(),
        }

        score = score_tweet(tweet)

        # Should use engagement-based path (existing logic)
        assert score > 0  # metrics present → composite formula applies

    @patch("score.score_x._load_weights", return_value=MOCK_CONTENT_WEIGHTS)
    @patch("score.score_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_content_scoring_keyword_relevance(self, mock_patterns, mock_weights):
        """Should score higher with more keyword matches."""
        # Tweet with no keywords
        tweet_no_kw = {
            "text": "I had a nice coffee today, nothing technical to report.",
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        # Tweet with multiple keyword groups
        tweet_with_kw = {
            "text": "New open source benchmark for research paper evaluation on github with SOTA results.",
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        score_no_kw = score_tweet(tweet_no_kw)
        score_with_kw = score_tweet(tweet_with_kw)

        assert score_with_kw > score_no_kw

    @patch("score.score_x._load_weights", return_value=MOCK_CONTENT_WEIGHTS)
    @patch("score.score_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_content_scoring_length_component(self, mock_patterns, mock_weights):
        """Should score higher for longer, more substantive tweets."""
        # Short tweet (below min_length)
        tweet_short = {
            "text": "AI is cool",  # 10 chars < 50
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        # Medium tweet
        tweet_medium = {
            "text": "x" * 100,  # 100 chars, between min and optimal
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        # Long tweet
        tweet_long = {
            "text": "x" * 200,  # 200 chars, >= optimal
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        score_short = score_tweet(tweet_short)
        score_medium = score_tweet(tweet_medium)
        score_long = score_tweet(tweet_long)

        # Longer should score higher (all else equal)
        assert score_long >= score_medium >= score_short

    @patch("score.score_x._load_weights", return_value=MOCK_CONTENT_WEIGHTS)
    @patch("score.score_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_content_scoring_url_bonus(self, mock_patterns, mock_weights):
        """Should give bonus for URLs."""
        # Tweet without URL
        tweet_no_url = {
            "text": "x" * 150,  # Substantial length, no URL
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        # Tweet with URL
        tweet_with_url = {
            "text": "x" * 100 + " https://github.com/user/repo",
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        score_no_url = score_tweet(tweet_no_url)
        score_with_url = score_tweet(tweet_with_url)

        assert score_with_url > score_no_url

    @patch("score.score_x._load_weights", return_value=MOCK_CONTENT_WEIGHTS)
    @patch("score.score_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_content_scoring_time_decay(self, mock_patterns, mock_weights):
        """Should decay score for older tweets."""
        # Fresh tweet
        tweet_fresh = {
            "text": "open source benchmark research",
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        # Old tweet
        tweet_old = {
            "text": "open source benchmark research",
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _hours_ago_iso(48),  # 2 days old
        }

        score_fresh = score_tweet(tweet_fresh)
        score_old = score_tweet(tweet_old)

        # Fresh should score higher than old (time decay component)
        assert score_fresh > score_old

    @patch("score.score_x._load_weights", return_value=MOCK_CONTENT_WEIGHTS)
    @patch("score.score_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_content_scoring_no_created_at(self, mock_patterns, mock_weights):
        """Should handle missing created_at with default."""
        tweet = {
            "text": "open source research paper benchmark",
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            # No created_at
        }

        score = score_tweet(tweet)

        # Should not crash, should return valid score
        assert 0.0 <= score <= 1.0
        assert score > 0  # Has keywords

    @patch("score.score_x._load_weights", return_value=MOCK_CONTENT_WEIGHTS)
    @patch("score.score_x._load_patterns", return_value=MOCK_PATTERNS)
    def test_content_score_capped_at_one(self, mock_patterns, mock_weights):
        """Should cap content-based score at 1.0."""
        # Tweet with everything: many keywords, long, URL, fresh
        tweet = {
            "text": "open source benchmark SOTA research paper on github with arxiv publication " + "x" * 200,
            "public_metrics": {"reply_count": 0, "like_count": 0, "retweet_count": 0},
            "created_at": _now_iso(),
        }

        score = score_tweet(tweet)

        assert score <= 1.0
