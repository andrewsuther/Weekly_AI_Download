"""arXiv export API: 8 domain queries, dedup by arxiv_id, 3s courtesy delay."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CATEGORIES_PATH = CONFIG_DIR / "arxiv_categories.yaml"

ARXIV_API_BASE = "http://export.arxiv.org/api/"
COURTESY_DELAY_SECS = 3
MAX_RESULTS_PER_QUERY = 50


def _load_domains() -> list[dict]:
    with open(CATEGORIES_PATH) as f:
        return yaml.safe_load(f)["domains"]


def _build_query(domain: dict) -> str:
    """
    Construct an arXiv search_query string for one domain config.
    Format: (cat OR ...) AND (keyword OR ...) AND submittedDate:[...]
    """
    cat_filter = " OR ".join(f"cat:{c}" for c in domain["categories"])
    kw_filter = " OR ".join(f'"{kw}"' for kw in domain["keywords"])

    now = datetime.now(timezone.utc)
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


def fetch_papers() -> list[dict]:
    """
    Query arXiv across all 8 configured domains.
    Deduplicates by arxiv_id across domains.
    Returns list of paper dicts.
    """
    domains = _load_domains()
    seen_ids: set[str] = set()
    all_papers: list[dict] = []

    for domain in domains:
        query = _build_query(domain)
        params = {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": MAX_RESULTS_PER_QUERY,
        }

        print(f"  Querying arXiv: {domain['name']}...")
        resp = requests.get(ARXIV_API_BASE, params=params)
        resp.raise_for_status()

        feed = feedparser.parse(resp.text)
        new_count = 0
        for entry in feed.entries:
            paper = _parse_entry(entry, domain["name"])
            if paper["arxiv_id"] in seen_ids:
                continue
            seen_ids.add(paper["arxiv_id"])
            all_papers.append(paper)
            new_count += 1

        print(f"    -> {new_count} new papers (total: {len(all_papers)})")
        time.sleep(COURTESY_DELAY_SECS)

    print(f"  Fetched {len(all_papers)} unique papers across {len(domains)} domains.")
    return all_papers
