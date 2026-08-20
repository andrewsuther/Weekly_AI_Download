"""arXiv export API: 8 domain queries, dedup by arxiv_id, 3s courtesy delay."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

from common.logging_setup import get_logger, log_event
from common.resilience import http_retryable, retry, retry_after_seconds
from common.run_summary import record_degradation

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CATEGORIES_PATH = CONFIG_DIR / "arxiv_categories.yaml"

ARXIV_API_BASE = "https://export.arxiv.org/api/query"
COURTESY_DELAY_SECS = 3
MAX_RESULTS_PER_QUERY = 50
REQUEST_TIMEOUT_SECS = 30
USER_AGENT = "WeeklyAI-Download/1.0 (+https://github.com/andrewsuther/Weekly_AI_Download)"

# Retry sleep hook only. Tests monkeypatch ``fetch.fetch_arxiv._SLEEP`` to a
# no-op; the courtesy delay between domains still uses ``time.sleep`` directly.
_SLEEP = time.sleep

logger = get_logger(__name__)


def _load_domains() -> list[dict]:
    with open(CATEGORIES_PATH) as f:
        return yaml.safe_load(f)["domains"]


def _build_query(domain: dict, end_date: datetime | None = None) -> str:
    """
    Construct an arXiv search_query string for one domain config.
    Format: (cat OR ...) AND (keyword OR ...) AND submittedDate:[...]
    """
    cat_filter = " OR ".join(f"cat:{c}" for c in domain["categories"])
    kw_filter = " OR ".join(f'"{kw}"' for kw in domain["keywords"])

    now = end_date or datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    # arXiv date format: YYYYMMDD0000 to YYYYMMDD2359
    date_filter = (
        f"submittedDate:[{start.strftime('%Y%m%d')}0000 "
        f"TO {now.strftime('%Y%m%d')}2359]"
    )

    return f"all:({cat_filter}) AND all:({kw_filter}) AND {date_filter}"


def _parse_entry(entry, domain_name: str) -> dict:
    """Parse one feedparser entry into a normalized paper dict."""
    # Extract arxiv_id from the entry URL (strip version suffix)
    raw_id = entry.id
    if "/abs/" in raw_id:
        arxiv_id = raw_id.split("/abs/")[-1]
    else:
        arxiv_id = raw_id.split("/")[-1]
    # Remove version suffix (e.g. v1, v2)
    if "v" in arxiv_id and arxiv_id.split("v")[-1].isdigit():
        arxiv_id = arxiv_id.rsplit("v", 1)[0]

    authors = [a.get("name", "Unknown") for a in entry.get("authors", [])]
    categories = [t.get("term", "") for t in entry.get("tags", [])]

    return {
        "arxiv_id": arxiv_id,
        "title": entry.title.replace("\n", " ").strip(),
        "authors": authors,
        "abstract": entry.summary.replace("\n", " ").strip(),
        "published": entry.get("published", ""),
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "categories": categories,
        "domain": domain_name,
    }


def _fetch_domain(
    domain: dict,
    seen_ids: set[str],
    end_date: datetime | None = None,
) -> tuple[list[dict], int]:
    """Fetch + parse one domain, returning (new_papers, new_count).

    The HTTP request is wrapped in retry-with-backoff for transient errors.
    Raises on exhaustion (RetryError) or non-retryable errors; the caller is
    responsible for skipping the domain and recording a degradation.
    """
    query = _build_query(domain, end_date=end_date)
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": MAX_RESULTS_PER_QUERY,
    }

    @retry(
        retry_on=http_retryable,
        retry_after=retry_after_seconds,
        logger=logger,
        sleep=lambda s: _SLEEP(s),
    )
    def _do_request():
        resp = requests.get(
            ARXIV_API_BASE,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECS,
        )
        resp.raise_for_status()
        return resp

    resp = _do_request()

    feed = feedparser.parse(resp.text)
    new_papers: list[dict] = []
    new_count = 0
    for entry in feed.entries:
        paper = _parse_entry(entry, domain["name"])
        if paper["arxiv_id"] in seen_ids:
            continue
        seen_ids.add(paper["arxiv_id"])
        new_papers.append(paper)
        new_count += 1

    return new_papers, new_count


def fetch_papers(*, summary=None, end_date: datetime | None = None) -> list[dict]:
    """
    Query arXiv across all configured domains.
    Deduplicates by arxiv_id across domains.

    A single domain failing skips only that domain (recording a degradation on
    ``summary`` if provided); all domains failing returns []. This never raises
    so the pipeline always ships a partial digest.
    """
    domains = _load_domains()
    seen_ids: set[str] = set()
    all_papers: list[dict] = []

    for domain in domains:
        try:
            new_papers, new_count = _fetch_domain(
                domain, seen_ids, end_date=end_date
            )
        except Exception as exc:  # noqa: BLE001 - degrade the domain, keep shipping
            # Notably RetryError / requests.RequestException, but any failure of
            # a single domain must skip only that domain, never abort the run.
            record_degradation(
                summary,
                logger,
                "fetch_arxiv",
                f"domain:{domain['name']}",
                exc,
                event="domain",
                domain=domain["name"],
            )
        else:
            all_papers.extend(new_papers)
            log_event(
                logger,
                "fetch_arxiv",
                "domain",
                outcome="ok",
                domain=domain["name"],
                new=new_count,
                total=len(all_papers),
            )
        finally:
            time.sleep(COURTESY_DELAY_SECS)

    log_event(
        logger,
        "fetch_arxiv",
        "complete",
        outcome="ok",
        total=len(all_papers),
        domains=len(domains),
    )
    return all_papers
