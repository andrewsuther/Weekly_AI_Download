"""X (Twitter) fetcher via xAI Grok API x_search tool."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
TOPICS_PATH = CONFIG_DIR / "x_topics.yaml"

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"


def _load_topics() -> list[dict]:
    """Load search topics from x_topics.yaml."""
    with open(TOPICS_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("topics", [])


def _extract_tweet_urls(citations: list[str]) -> list[str]:
    """
    Extract valid X/Twitter URLs from citations list.
    Returns only URLs matching x.com or twitter.com tweet status pattern.
    """
    tweet_pattern = re.compile(r"https?://(?:x\.com|twitter\.com)/\w+/status/\d+")
    return [url for url in citations if tweet_pattern.match(url)]


def _parse_tweet_from_url(url: str) -> dict[str, str] | None:
    """
    Parse tweet URL to extract username and tweet ID.
    Format: https://x.com/{username}/status/{tweet_id}
    Returns dict with 'username' and 'id' keys, or None if invalid.
    """
    match = re.match(r"https?://(?:x\.com|twitter\.com)/(\w+)/status/(\d+)", url)
    if not match:
        return None
    return {"username": match.group(1), "id": match.group(2)}


def _extract_text_from_summary(content: str, tweet_info: dict) -> str:
    """
    Extract tweet text from LLM summary content via heuristics.

    Strategies:
    1. Look for quoted text in the summary
    2. Look for @username mentions and extract surrounding context
    3. Use first substantial sentence as fallback
    4. Return placeholder if all else fails

    Returns extracted text or placeholder.
    """
    # Strategy 1: Look for text in quotes
    quote_pattern = re.compile(r'["""](.*?)["""]', re.DOTALL)
    quotes = quote_pattern.findall(content)
    if quotes:
        # Use longest quote as it's likely the tweet text
        longest_quote = max(quotes, key=len)
        if len(longest_quote) > 20:  # Substantial enough
            return longest_quote.strip()

    # Strategy 2: Look for @username mentions
    username = tweet_info.get("username", "")
    if username:
        mention_pattern = re.compile(rf"@{username}[:\s]+(.*?)(?:\.|$)", re.IGNORECASE | re.DOTALL)
        mention_match = mention_pattern.search(content)
        if mention_match:
            text = mention_match.group(1).strip()
            if len(text) > 20:
                return text

    # Strategy 3: Use first substantial sentence
    sentences = re.split(r'[.!?]\s+', content)
    for sentence in sentences:
        if len(sentence) > 30 and not sentence.startswith(("The tweet", "This tweet", "A tweet")):
            return sentence.strip()

    # Strategy 4: Fallback - use first 200 chars of content
    # Keywords in the summary might still help classification
    return content[:200].strip() if content else f"[Tweet from @{username}]"


def _search_topic(topic: dict) -> list[dict]:
    """
    Search a single topic using xAI x_search tool.
    Returns list of tweet dicts parsed from citations.
    """
    topic_name = topic.get("name", "Unknown")
    query = topic.get("query", "")
    focus_areas = topic.get("focus_areas", [])

    if not query:
        print(f"  WARNING: Topic '{topic_name}' has no query. Skipping.")
        return []

    # Build prompt for xAI with x_search tool
    focus_text = "\n".join(f"- {area}" for area in focus_areas) if focus_areas else ""
    prompt = f"""Search X (Twitter) for recent tweets about: {query}

Focus areas:
{focus_text}

Please find tweets from the last 7 days that discuss these topics. Focus on tweets from researchers, engineers, or credible sources discussing technical developments, research findings, or significant insights."""

    # Make API call to xAI
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "grok-3-5-fast-20250129",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "tools": [{"type": "x_search"}],
    }

    try:
        print(f"  Searching X: {topic_name}...")
        resp = requests.post(
            f"{XAI_BASE_URL}/responses",
            headers=headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract citations (tweet URLs)
        citations = data.get("citations", [])
        tweet_urls = _extract_tweet_urls(citations)

        if not tweet_urls:
            print(f"    No tweets found for {topic_name}")
            return []

        # Get LLM response content for text extraction
        content = ""
        if data.get("choices"):
            message = data["choices"][0].get("message", {})
            content = message.get("content", "")

        # Parse tweets from URLs
        tweets = []
        for url in tweet_urls:
            tweet_info = _parse_tweet_from_url(url)
            if not tweet_info:
                continue

            # Extract text from summary
            text = _extract_text_from_summary(content, tweet_info)

            # Construct tweet dict with stub metrics
            # Format matches X API v2 structure for compatibility with scoring layer
            tweet = {
                "id": tweet_info["id"],
                "text": text,
                "created_at": (datetime.now(timezone.utc) - timedelta(days=3.5)).isoformat(),  # Approximate midpoint of search window
                "author_id": tweet_info["id"],  # Use tweet ID as proxy (not accurate but unused)
                "author": {
                    "id": tweet_info["id"],
                    "username": tweet_info["username"],
                    "name": tweet_info["username"],  # Unavailable from xAI
                    "verified": False,  # Unavailable from xAI
                    "public_metrics": {
                        "followers_count": 0,  # Unavailable from xAI
                        "following_count": 0,
                        "tweet_count": 0,
                    },
                },
                "public_metrics": {
                    "reply_count": 0,  # Unavailable from xAI
                    "like_count": 0,
                    "retweet_count": 0,
                    "quote_count": 0,
                },
                "_source": "xai",  # Mark for debugging
                "_topic": topic_name,
                "_url": url,  # Preserve original URL
            }

            tweets.append(tweet)

        print(f"    Found {len(tweets)} tweets")
        return tweets

    except requests.exceptions.RequestException as e:
        print(f"  ERROR: Failed to search topic '{topic_name}': {e}")
        return []
    except Exception as e:
        print(f"  ERROR: Unexpected error processing topic '{topic_name}': {e}")
        return []


def fetch_tweets() -> list[dict]:
    """
    Entry point: search all topics via xAI x_search, return flat list of tweet dicts.
    Deduplicates by tweet ID across topics.
    """
    if not XAI_API_KEY:
        print("  WARNING: XAI_API_KEY not set. Skipping X fetch.")
        return []

    topics = _load_topics()
    if not topics:
        print("  WARNING: No topics found in x_topics.yaml. Skipping X fetch.")
        return []

    all_tweets: list[dict] = []
    seen_ids: set[str] = set()

    for topic in topics:
        tweets = _search_topic(topic)

        # Deduplicate across topics
        for tweet in tweets:
            tweet_id = tweet.get("id", "")
            if tweet_id and tweet_id not in seen_ids:
                all_tweets.append(tweet)
                seen_ids.add(tweet_id)

    print(f"  Fetched {len(all_tweets)} unique tweets from {len(topics)} topics.")
    return all_tweets
