"""Assemble the weekly digest .md report."""

from __future__ import annotations

from datetime import datetime, timezone


def _truncate(text: str, max_len: int = 150) -> str:
    """Truncate at word boundary, append ellipsis if cut."""
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "\u2026"


def _paper_block(paper: dict) -> str:
    """Render one paper's header + analysis block."""
    title = paper["title"]
    authors = ", ".join(paper["authors"][:3])
    if len(paper["authors"]) > 3:
        authors += " et al."

    # Format published date
    date_str = paper.get("published", "")
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            date_str = dt.strftime("%b %d, %Y")
        except (ValueError, AttributeError):
            pass

    url = paper["url"]
    analysis = paper.get("analysis", "")

    header = (
        f"### {title}\n"
        f"**Authors:** {authors}  |  **Date:** {date_str}  |  "
        f"**[Read on arXiv \u2192]({url})**\n"
    )
    return header + "\n" + analysis


def _x_signal_block(tweet: dict) -> str:
    """Render one X signal entry: truncated text as link, handle."""
    author = tweet.get("author", {})
    handle = author.get("username", "unknown")
    tweet_id = tweet.get("id", "")
    url = f"https://x.com/{handle}/status/{tweet_id}"
    text = _truncate(tweet.get("text", ""), 150)
    return f"- **[{text}]({url})** \u2014 @{handle}"


def _failure_signal_block(tweet: dict) -> str:
    """Render one failure signal: signal block + 'why it matters' annotation."""
    block = _x_signal_block(tweet)
    why = tweet.get("why_it_matters", "")
    if why:
        block += f"\n  \u21b3 Why it matters: {why}"
    return block


def _categorize_high_signals(tweets: list[dict]) -> dict[str, list[dict]]:
    """Heuristic bucket: OSS & Tools vs Industry."""
    oss_keywords = [
        "github", "open source", "open-source", "repo", "huggingface",
        "model", "weights", "sdk", "api", "tool", "library", "dataset",
    ]
    industry_keywords = [
        "shipped", "launched", "production", "company", "startup",
        "funding", "product", "deploy", "enterprise", "partnership",
    ]
    oss: list[dict] = []
    industry: list[dict] = []

    for tweet in tweets:
        text = tweet.get("text", "").lower()
        oss_score = sum(1 for kw in oss_keywords if kw in text)
        ind_score = sum(1 for kw in industry_keywords if kw in text)
        if oss_score >= ind_score:
            oss.append(tweet)
        else:
            industry.append(tweet)

    return {"oss_tools": oss, "industry": industry}


def build_report(
    analysis_results: dict,
    high_signal_tweets: list[dict],
    date: datetime | None = None,
) -> str:
    """
    Build the full weekly digest .md.

    Args:
        analysis_results: output from analyze_papers.run_analysis()
        high_signal_tweets: classified + scored high_signal tweets, sorted by score desc
        date: report date (defaults to now UTC)

    Returns:
        Complete .md string with YAML front-matter.
    """
    if date is None:
        date = datetime.now(timezone.utc)

    tiered_papers = analysis_results["tiered_papers"]
    trends = analysis_results.get("trends", "")
    failure_signals = analysis_results.get("failure_signals", [])
    takeaway = analysis_results.get("takeaway", "")

    papers_analyzed = sum(len(v) for v in tiered_papers.values())
    x_signals_count = len(high_signal_tweets) + len(failure_signals)

    # Collect domains from analyzed papers (fallback to default set)
    all_domains = sorted(set(
        p.get("domain", "Unknown")
        for tier_papers in tiered_papers.values()
        for p in tier_papers
    ))
    if not all_domains:
        all_domains = [
            "AI", "Data Science", "Economics", "Engineering",
            "ML", "Operations Research", "Statistics", "Systems Design",
        ]

    domains_str = ", ".join(all_domains)
    week_str = date.strftime("%B %d, %Y")

    # ── YAML front-matter ──
    md = (
        "---\n"
        f"date: {date.strftime('%Y-%m-%d')}\n"
        f"domains: [{domains_str}]\n"
        f"papers_analyzed: {papers_analyzed}\n"
        f"x_signals: {x_signals_count}\n"
        "generated_by: Weekly AI Download\n"
        "---\n"
        "\n"
        "# Weekly AI Download\n"
        f"## Week of {week_str}\n"
        "\n"
        "---\n"
    )

    # ── Tier 1 ──
    md += "\n## Tier 1: Paradigm Shifters\n"
    if tiered_papers.get(1):
        for paper in tiered_papers[1]:
            md += "\n" + _paper_block(paper) + "\n"
    else:
        md += "\n*No Tier 1 papers this week.*\n"
    md += "\n---\n"

    # ── Tier 2 ──
    md += "\n## Tier 2: High-Leverage Tools\n"
    if tiered_papers.get(2):
        for paper in tiered_papers[2]:
            md += "\n" + _paper_block(paper) + "\n"
    else:
        md += "\n*No Tier 2 papers this week.*\n"
    md += "\n---\n"

    # ── Tier 3 ──
    md += "\n## Tier 3: Tactical Enhancements\n"
    if tiered_papers.get(3):
        for paper in tiered_papers[3]:
            md += "\n" + _paper_block(paper) + "\n"
    else:
        md += "\n*No Tier 3 papers this week.*\n"
    md += "\n---\n"

    # ── Emerging Trends ──
    md += "\n## Emerging Trends & Themes\n\n"
    md += trends if trends else "*No trends synthesized this week.*"
    md += "\n\n---\n"

    # ── X Signals ──
    md += "\n## Signal from X \u2014 This Week\n\n"
    md += "*What's moving in the real world. Links to read deeper.*\n"

    categorized = _categorize_high_signals(high_signal_tweets)

    md += "\n### OSS & Tools\n"
    if categorized["oss_tools"]:
        md += "\n".join(_x_signal_block(t) for t in categorized["oss_tools"])
        md += "\n"
    else:
        md += "\n*No OSS signals this week.*\n"

    md += "\n### Industry\n"
    if categorized["industry"]:
        md += "\n".join(_x_signal_block(t) for t in categorized["industry"])
        md += "\n"
    else:
        md += "\n*No industry signals this week.*\n"

    md += "\n### Failure Signals & Dead Ends\n"
    md += "*Progress accelerates faster by avoiding dead ends than chasing every new idea.*\n\n"
    if failure_signals:
        md += "\n".join(_failure_signal_block(t) for t in failure_signals)
        md += "\n"
    else:
        md += "\n*No failure signals this week.*\n"

    md += "\n---\n"

    # ── One-Line Takeaway ──
    md += "\n## One-Line Takeaway\n\n"
    if takeaway:
        md += f"> {takeaway}\n"
    else:
        md += "> *See Emerging Trends & Themes above for this week's key insight.*\n"

    return md
