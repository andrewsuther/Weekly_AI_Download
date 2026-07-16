"""Tests for arXiv fetcher resilience: partial digest on domain failures."""

from unittest.mock import patch

import pytest
import requests

from common.run_summary import RunSummary
from fetch.fetch_arxiv import REQUEST_TIMEOUT_SECS, fetch_papers


SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2601.00001v1</id>
    <title>Scaling Laws for Widgets</title>
    <summary>An abstract about widgets and scaling.</summary>
    <published>2026-07-10T00:00:00Z</published>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <category term="cs.LG" />
    <category term="cs.AI" />
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2601.00002v2</id>
    <title>Attention Revisited</title>
    <summary>Another abstract on attention.</summary>
    <published>2026-07-11T00:00:00Z</published>
    <author><name>Grace Hopper</name></author>
    <category term="cs.CL" />
  </entry>
</feed>
"""

EMPTY_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>
"""


class _MockResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


def _mock_resp(text, status=200):
    return _MockResp(text, status=status)


def _domains(names):
    return [
        {"name": n, "categories": ["cs.LG"], "keywords": ["ai"]} for n in names
    ]


@pytest.fixture(autouse=True)
def _no_sleep():
    """Disable both the courtesy delay and the retry sleep for all tests."""
    with patch("fetch.fetch_arxiv.time.sleep"), patch(
        "fetch.fetch_arxiv._SLEEP"
    ):
        yield


def test_happy_path_parse_and_dedup():
    with patch("fetch.fetch_arxiv._load_domains", return_value=_domains(["A", "B"])), patch(
        "fetch.fetch_arxiv.requests.get", return_value=_mock_resp(SAMPLE_ATOM)
    ):
        papers = fetch_papers()

    # Same feed for both domains -> dedup to 2 unique papers.
    assert len(papers) == 2
    ids = {p["arxiv_id"] for p in papers}
    assert ids == {"2601.00001", "2601.00002"}  # version suffix stripped

    p1 = next(p for p in papers if p["arxiv_id"] == "2601.00001")
    assert p1["url"] == "https://arxiv.org/abs/2601.00001"
    assert p1["domain"] == "A"
    assert p1["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert p1["categories"] == ["cs.LG", "cs.AI"]
    assert p1["title"] == "Scaling Laws for Widgets"


def test_one_domain_500_skipped_others_returned():
    summary = RunSummary(started_at="now")

    with patch("fetch.fetch_arxiv._load_domains", return_value=_domains(["A", "B", "C"])):
        # Map by call order: A repeated (3 retries exhaust), then B, then C.
        seq = [
            _mock_resp("boom", status=500),
            _mock_resp("boom", status=500),
            _mock_resp("boom", status=500),
            _mock_resp(SAMPLE_ATOM),
            _mock_resp(SAMPLE_ATOM),
        ]
        with patch("fetch.fetch_arxiv.requests.get", side_effect=seq):
            papers = fetch_papers(summary=summary)

    assert len(papers) == 2  # B + C dedup to 2 unique
    scopes = [d.scope for d in summary.degradations]
    assert "domain:A" in scopes
    assert len(summary.degradations) == 1
    assert summary.degradations[0].fatal_to_stage is False


def test_all_domains_fail_returns_empty_no_raise():
    summary = RunSummary(started_at="now")
    with patch("fetch.fetch_arxiv._load_domains", return_value=_domains(["A", "B"])), patch(
        "fetch.fetch_arxiv.requests.get", return_value=_mock_resp("boom", status=503)
    ):
        papers = fetch_papers(summary=summary)

    assert papers == []
    assert len(summary.degradations) == 2
    assert {d.scope for d in summary.degradations} == {"domain:A", "domain:B"}


def test_transient_timeout_then_success():
    good = _mock_resp(SAMPLE_ATOM)
    seq = [requests.exceptions.Timeout("slow"), good]
    with patch("fetch.fetch_arxiv._load_domains", return_value=_domains(["A"])), patch(
        "fetch.fetch_arxiv.requests.get", side_effect=seq
    ) as mock_get:
        papers = fetch_papers()

    assert len(papers) == 2
    assert mock_get.call_count == 2  # retried once after the timeout


def test_timeout_passed_to_requests_get():
    with patch("fetch.fetch_arxiv._load_domains", return_value=_domains(["A"])), patch(
        "fetch.fetch_arxiv.requests.get", return_value=_mock_resp(SAMPLE_ATOM)
    ) as mock_get:
        fetch_papers()

    _, kwargs = mock_get.call_args
    assert kwargs["timeout"] == REQUEST_TIMEOUT_SECS


def test_empty_feed_zero_papers_no_error():
    summary = RunSummary(started_at="now")
    with patch("fetch.fetch_arxiv._load_domains", return_value=_domains(["A"])), patch(
        "fetch.fetch_arxiv.requests.get", return_value=_mock_resp(EMPTY_ATOM)
    ):
        papers = fetch_papers(summary=summary)

    assert papers == []
    assert summary.degradations == []


def test_400_not_retried_domain_degraded():
    summary = RunSummary(started_at="now")
    with patch("fetch.fetch_arxiv._load_domains", return_value=_domains(["A"])), patch(
        "fetch.fetch_arxiv.requests.get", return_value=_mock_resp("bad", status=400)
    ) as mock_get:
        papers = fetch_papers(summary=summary)

    assert papers == []
    assert mock_get.call_count == 1  # 4xx (non-429) is not retried
    assert len(summary.degradations) == 1
    assert summary.degradations[0].scope == "domain:A"
    assert summary.degradations[0].error_type == "HTTPError"
