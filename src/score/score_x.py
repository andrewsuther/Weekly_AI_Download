"""4-component algo-inspired composite scorer for X posts."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
WEIGHTS_PATH = CONFIG_DIR / "scoring_weights.yaml"
KEYWORDS_PATH = CONFIG_DIR / "x_keywords.yaml"


def _load_weights() -> dict:
    """Load scoring weights from scoring_weights.yaml."""
    with open(WEIGHTS_PATH) as f:
        return yaml.safe_load(f)


def _load_patterns() -> dict:
    """Load keyword patterns from x_keywords.yaml."""
    with open(KEYWORDS_PATH) as f:
        return yaml.safe_load(f)


# ── Component scorers ────────────────────────────────────────────────────────


def _engagement_score(metrics: dict, weights: dict, tweet_age_hours: float) -> float:
    """
    Weighted engagement sum using X's revealed algo weights,
    multiplied by an engagement-velocity factor, then log-normalized to [0, 1].
    """
    ew = weights["engagement_weights"]
    raw = (
        metrics.get("reply_count", 0) * ew["reply_count"]
        + metrics.get("like_count", 0) * ew["like_count"]
        + metrics.get("retweet_count", 0) * ew["retweet_count"]
        + metrics.get("bookmark_count", 0) * ew.get("bookmark_count", 0)
        + metrics.get("quote_tweet_count", 0) * ew.get("quote_tweet_count", 0)
    )

    # Velocity multiplier: rewards fast-accumulating engagement
    if tweet_age_hours > 0:
        velocity = raw / tweet_age_hours
        # Cap the velocity boost at 10x to avoid domination by sub-hour tweets
        velocity_boost = min(velocity * weights["engagement_velocity_multiplier"], 10.0)
        raw *= 1.0 + velocity_boost

    # Log-normalize to [0, 1]
    return min(math.log1p(raw) / 10.0, 1.0)


def _time_decay_score(tweet_age_hours: float, half_life: float) -> float:
    """Exponential decay. 1.0 = just posted; 0.5 at half_life hours."""
    return 0.5 ** (tweet_age_hours / half_life)


def _social_graph_score(author: dict, weights: dict) -> float:
    """
    Follower count ^ exponent, log-dampened to flatten mega-account dominance.
    Verified accounts get a multiplicative boost.
    """
    public_metrics = author.get("public_metrics", {})
    followers = public_metrics.get("followers_count", 0)

    # Power law compression
    raw = followers ** weights["social_graph_exponent"]

    # Log dampening
    dampened = math.log(raw + 1, weights["social_graph_log_base"])

    # Verified boost
    if author.get("verified", False):
        dampened *= weights["verified_boost"]

    # Normalize to [0, 1]
    return min(dampened / 5.0, 1.0)


def _snr_score(metrics: dict, weights: dict) -> float:
    """
    Signal-to-noise ratio filter.
    - Hard floor: total engagements < threshold -> 0.0 (tweet is noise)
    - Low reply/like ratio -> heavy penalty (viral but shallow engagement)
    """
    total = (
        metrics.get("reply_count", 0)
        + metrics.get("like_count", 0)
        + metrics.get("retweet_count", 0)
    )

    if total < weights["snr_min_total_engagements"]:
        return 0.0  # hard floor

    likes = metrics.get("like_count", 0)
    replies = metrics.get("reply_count", 0)
    if likes > 0:
        ratio = replies / likes
        if ratio < weights["snr_reply_like_ratio_threshold"]:
            return weights["snr_low_ratio_penalty"]

    return 1.0


# ── Content-based scoring (for tweets without engagement metrics) ──────────


def _score_tweet_content_based(tweet: dict) -> float:
    """
    Score tweets by content quality signals (no metrics required).

    Components:
    - keyword_relevance (weight 0.5): # of high_signal groups matched
    - content_length (weight 0.2): substantive vs one-liner
    - url_presence (bonus 0.2): links to papers/repos/articles
    - time_decay (weight 0.1): recency (if created_at available)

    Returns 0.0-1.0.
    """
    weights = _load_weights()
    cw = weights.get("content_weights", {
        "keyword_relevance": 0.5,
        "length": 0.2,
        "url_bonus": 0.2,
        "time_decay": 0.1,
    })

    text = tweet.get("text", "").lower()

    # Keyword relevance
    patterns = _load_patterns()
    high_groups = patterns.get("high_signal_groups", [])
    matches = sum(1 for g in high_groups if any(p.lower() in text for p in g["patterns"]))
    kw_score = min(matches / 3.0, 1.0)  # cap at 3 groups

    # Content length
    min_len = weights.get("content_min_length", 50)
    opt_len = weights.get("content_optimal_length", 140)
    length = len(tweet.get("text", ""))
    if length < min_len:
        len_score = 0.0
    elif length < opt_len:
        len_score = 0.5
    else:
        len_score = 1.0

    # URL presence
    url_score = 0.2 if any(kw in text for kw in ["http", "github", "arxiv"]) else 0.0

    # Time decay (if created_at present)
    created_at_str = tweet.get("created_at", "")
    if created_at_str:
        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
        td_score = 0.5 ** (age_hours / weights.get("time_decay_half_life_hours", 6))
    else:
        td_score = 0.5  # default midpoint

    composite = (
        kw_score * cw["keyword_relevance"] +
        len_score * cw["length"] +
        url_score +
        td_score * cw["time_decay"]
    )

    return round(min(composite, 1.0), 4)


# ── Composite scorer ─────────────────────────────────────────────────────────


def _score_tweet_engagement_based(tweet: dict) -> float:
    """
    Original engagement-based scorer (unchanged).

    Compute the composite score for a tweet using engagement metrics. Returns 0.0 – 1.0.

    Formula (weights from scoring_weights.yaml):
        score = eng*0.45 + td*0.15 + sg*0.25 + snr*0.15

    If SNR hard-floors to 0 (< 5 total engagements), entire score is 0.
    """
    weights = _load_weights()
    cw = weights["composite_weights"]

    metrics = tweet.get("public_metrics", {})
    author = tweet.get("author", {})

    # Compute tweet age in hours
    created_at_str = tweet.get("created_at", "")
    if created_at_str:
        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
    else:
        age_hours = 24.0  # conservative default if timestamp missing

    # Component scores
    eng = _engagement_score(metrics, weights, age_hours)
    td = _time_decay_score(age_hours, weights["time_decay_half_life_hours"])
    sg = _social_graph_score(author, weights)
    snr = _snr_score(metrics, weights)

    # SNR = 0 means hard floor; entire tweet is noise
    if snr == 0.0:
        return 0.0

    composite = (
        eng * cw["engagement"]
        + td * cw["time_decay"]
        + sg * cw["social_graph"]
        + snr * cw["snr"]
    )

    return round(composite, 4)


def score_tweet(tweet: dict) -> float:
    """
    Composite scorer with metric-availability detection.

    - Metrics present & non-zero: engagement-based scoring (existing)
    - Metrics absent or all zero: content-based scoring (new)
    """
    metrics = tweet.get("public_metrics", {})
    total = metrics.get("reply_count", 0) + metrics.get("like_count", 0) + metrics.get("retweet_count", 0)

    if total > 0:
        # Existing path: engagement-based
        return _score_tweet_engagement_based(tweet)
    else:
        # New path: content-based
        return _score_tweet_content_based(tweet)
