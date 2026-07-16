"""Tests for xAI-based X fetcher."""

from unittest.mock import patch, Mock
import pytest

from fetch.fetch_x import (
    fetch_tweets,
    _extract_tweet_urls,
    _parse_tweet_from_url,
    _extract_text_from_summary,
    _search_topic,
)


class TestFetchXAI:
    """Test suite for xAI x_search-based tweet fetcher."""

    @patch("fetch.fetch_x.XAI_API_KEY", "")
    def test_guard_when_api_key_unset(self):
        """Should return empty list when XAI_API_KEY is not set."""
        result = fetch_tweets()
        assert result == []

    def test_extract_urls_from_citations(self):
        """Should extract only valid X/Twitter URLs from citations."""
        citations = [
            "https://x.com/karpathy/status/123456789",
            "https://twitter.com/ylecun/status/987654321",
            "https://example.com/not-a-tweet",
            "https://github.com/user/repo",
        ]
        urls = _extract_tweet_urls(citations)
        assert len(urls) == 2
        assert "karpathy" in urls[0]
        assert "ylecun" in urls[1]

    def test_parse_tweet_from_url_x_domain(self):
        """Should parse tweet info from x.com URL."""
        url = "https://x.com/karpathy/status/1234567890"
        result = _parse_tweet_from_url(url)
        assert result is not None
        assert result["username"] == "karpathy"
        assert result["id"] == "1234567890"

    def test_parse_tweet_from_url_twitter_domain(self):
        """Should parse tweet info from twitter.com URL."""
        url = "https://twitter.com/ylecun/status/9876543210"
        result = _parse_tweet_from_url(url)
        assert result is not None
        assert result["username"] == "ylecun"
        assert result["id"] == "9876543210"

    def test_parse_tweet_from_invalid_url(self):
        """Should return None for invalid URL."""
        url = "https://example.com/not-a-tweet"
        result = _parse_tweet_from_url(url)
        assert result is None

    def test_extract_text_from_quoted_content(self):
        """Should extract text from quoted content in summary."""
        content = 'The tweet states: "This is a breakthrough in AI alignment research."'
        tweet_info = {"username": "testuser"}
        text = _extract_text_from_summary(content, tweet_info)
        assert "breakthrough" in text
        assert len(text) > 20

    def test_extract_text_from_username_mention(self):
        """Should extract text following @username mention."""
        content = "@testuser: Just published our new paper on scaling laws"
        tweet_info = {"username": "testuser"}
        text = _extract_text_from_summary(content, tweet_info)
        assert "paper" in text or "published" in text

    def test_extract_text_fallback(self):
        """Should use fallback strategy when no quotes or mentions found."""
        content = "This paper discusses advances in transformer architectures and attention mechanisms."
        tweet_info = {"username": "testuser"}
        text = _extract_text_from_summary(content, tweet_info)
        assert len(text) > 0
        # Should extract a substantial sentence
        assert len(text) > 20

    @patch("fetch.fetch_x.XAI_API_KEY", "test-key")
    @patch("fetch.fetch_x.requests.post")
    @patch("fetch.fetch_x._load_topics")
    def test_search_topic_successful(self, mock_load_topics, mock_post):
        """Should successfully search a topic and return tweets."""
        # Mock API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "citations": [
                "https://x.com/karpathy/status/123",
                "https://x.com/ylecun/status/456",
            ],
            "choices": [{
                "message": {
                    "content": "The research discusses 'breakthrough in AI alignment' and 'new transformer architecture'."
                }
            }]
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        topic = {
            "name": "AI Research",
            "query": "AI alignment and transformers",
            "focus_areas": ["alignment", "architecture"]
        }

        tweets = _search_topic(topic)

        assert len(tweets) == 2
        assert all(tweet["_source"] == "xai" for tweet in tweets)
        assert all(tweet["_topic"] == "AI Research" for tweet in tweets)
        assert all("public_metrics" in tweet for tweet in tweets)
        assert all(tweet["public_metrics"]["like_count"] == 0 for tweet in tweets)

    @patch("fetch.fetch_x.XAI_API_KEY", "test-key")
    @patch("fetch.fetch_x.requests.post")
    def test_search_topic_handles_api_error(self, mock_post):
        """Should handle API errors gracefully and return empty list."""
        mock_post.side_effect = Exception("API Error")

        topic = {
            "name": "AI Research",
            "query": "AI alignment",
            "focus_areas": []
        }

        tweets = _search_topic(topic)
        assert tweets == []

    @patch("fetch.fetch_x.XAI_API_KEY", "test-key")
    @patch("fetch.fetch_x._load_topics")
    @patch("fetch.fetch_x._search_topic")
    def test_deduplication_across_topics(self, mock_search, mock_load_topics):
        """Should deduplicate tweets that appear in multiple topics."""
        mock_load_topics.return_value = [
            {"name": "Topic 1", "query": "query1"},
            {"name": "Topic 2", "query": "query2"},
        ]

        # Same tweet appears in both topics
        duplicate_tweet = {
            "id": "123",
            "text": "test tweet",
            "public_metrics": {"like_count": 0},
        }
        unique_tweet = {
            "id": "456",
            "text": "another tweet",
            "public_metrics": {"like_count": 0},
        }

        mock_search.side_effect = [
            [duplicate_tweet, unique_tweet],  # Topic 1 results
            [duplicate_tweet],                # Topic 2 results (duplicate)
        ]

        tweets = fetch_tweets()

        # Should only have 2 unique tweets, not 3
        assert len(tweets) == 2
        tweet_ids = [t["id"] for t in tweets]
        assert "123" in tweet_ids
        assert "456" in tweet_ids

    def test_search_topic_with_no_query(self):
        """Should skip topic if query is empty."""
        topic = {
            "name": "Empty Topic",
            "query": "",
            "focus_areas": []
        }

        tweets = _search_topic(topic)
        assert tweets == []

    @patch("fetch.fetch_x.XAI_API_KEY", "test-key")
    @patch("fetch.fetch_x.requests.post")
    def test_search_topic_with_no_citations(self, mock_post):
        """Should return empty list when no citations in response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "citations": [],  # No citations
            "choices": [{"message": {"content": "No tweets found."}}]
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        topic = {
            "name": "AI Research",
            "query": "AI alignment",
            "focus_areas": []
        }

        tweets = _search_topic(topic)
        assert tweets == []
