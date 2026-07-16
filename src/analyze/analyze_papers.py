"""Claude API: tier assignment, deep analysis, cross-paper synthesis, failure annotation."""

from __future__ import annotations

import json
import os

import anthropic

CLIENT = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
MODEL = "claude-sonnet-4-5-20250929"


def _call_claude(prompt: str, system: str = "", max_tokens: int = 4096) -> str:
    """Single Claude API call. Returns text content."""
    kwargs = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    resp = CLIENT.messages.create(**kwargs)
    return resp.content[0].text


def _parse_json(raw: str) -> dict:
    """Strip accidental markdown fences and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        # Remove opening fence (possibly with 'json' language tag)
        raw = raw.split("```", 1)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        # Remove closing fence
        if "```" in raw:
            raw = raw.split("```", 1)[0]
    return json.loads(raw.strip())


def tier_assign(papers: list[dict]) -> dict[str, int | None]:
    """
    Call 1: Assign tiers to all papers in a single prompt.
    Returns {arxiv_id: tier} where tier is 1, 2, 3, or None.
    """
    paper_list = "\n".join(
        f"[{i+1}] **{p['title']}** (arxiv_id: {p['arxiv_id']})\n"
        f"    Authors: {', '.join(p['authors'][:3])}\n"
        f"    Abstract: {p['abstract'][:400]}"
        for i, p in enumerate(papers)
    )

    prompt = (
        "You are evaluating research papers for a weekly AI digest "
        "targeted at a Pricing PM.\n\n"
        "Assign each paper to exactly one tier based on its potential impact:\n"
        "- **Tier 1 — Paradigm Shifters:** Fundamentally new ideas that could "
        "change how people think.\n"
        "- **Tier 2 — High-Leverage Tools:** Practical frameworks or methods "
        "with clear adoption paths.\n"
        "- **Tier 3 — Tactical Enhancements:** Incremental improvements, "
        "useful but not game-changing.\n"
        "- **null:** Below threshold — not relevant enough to include.\n\n"
        f"Papers:\n{paper_list}\n\n"
        "Respond with ONLY valid JSON — no markdown fences, no explanation.\n"
        'Format: {{"assignments": [{{"arxiv_id": "<id>", "tier": <1|2|3|null>}}]}}\n\n'
        "Select at most 3 papers per tier. Prefer quality over quantity."
    )

    raw = _call_claude(prompt)
    data = _parse_json(raw)
    return {a["arxiv_id"]: a["tier"] for a in data["assignments"]}


def deep_analyze(paper: dict) -> str:
    """
    Calls 2-10: Deep analysis of one paper.
    Returns analysis text (bullet-point markdown) per the tier template.
    """
    tier = paper.get("assigned_tier")

    if tier == 1:
        fields = (
            "- **Core idea:** 2-3 sentence distillation of the main contribution\n"
            "- **Technique introduced:** The specific method, framework, or approach\n"
            "- **Why it changes the game:** What shifts in thinking or capability this enables\n"
            "- **Cross-domain take:** How a Pricing PM or business role might borrow this mindset or model\n"
            "- **Caveats:** Limitations, assumptions, or conditions where this breaks down"
        )
    elif tier == 2:
        fields = (
            "- **Problem + solution:** What problem is solved and how\n"
            "- **Results:** Key quantitative or qualitative results\n"
            "- **Tools released:** Any code, models, datasets, or frameworks released\n"
            "- **Cross-domain take:** How a Pricing PM or business role might apply this\n"
            "- **Adoption barriers:** What makes this hard to adopt in practice"
        )
    else:  # tier == 3
        fields = (
            "- **Key insight:** The most useful takeaway\n"
            "- **Real-world tradeoff:** The practical tradeoff this addresses\n"
            "- **How to use it:** Concrete tweak inspiration for forecasting, override flows, UI, etc.\n"
            "- **Performance gains:** What improvement this delivers (quantified if available)"
        )

    prompt = (
        f"Analyze this research paper for a weekly AI digest.\n\n"
        f"Title: {paper['title']}\n"
        f"Authors: {', '.join(paper['authors'])}\n"
        f"Abstract: {paper['abstract']}\n"
        f"Published: {paper['published']}\n\n"
        f"Provide the following fields:\n{fields}\n\n"
        "Be concise. Each bullet 1-3 sentences max. "
        "Write for a Pricing PM audience — translate technical concepts "
        "into business/product implications where possible."
    )

    return _call_claude(prompt)


def synthesize_trends(analyses: list[dict]) -> dict:
    """
    Call 11: Cross-paper synthesis + one-line takeaway.
    analyses: list of dicts with 'title', 'domain', 'analysis'.
    Returns {"trends": str, "takeaway": str}.
    """
    context = "\n\n---\n\n".join(
        f"**{a['title']}** ({a['domain']})\n{a['analysis']}"
        for a in analyses
    )

    prompt = (
        "You have the following weekly deep analyses of research papers "
        "across AI, ML, and related fields:\n\n"
        f"{context}\n\n"
        "Identify the emerging trends and themes. Specifically:\n\n"
        "1. **Spreading techniques:** What methods or approaches appear across "
        "multiple papers? What's the common thread?\n"
        "2. **Mental models:** What do these innovations collectively imply about "
        "how to think about AI/ML problems?\n"
        "3. **Convergence:** Where are multiple fields hitting the same wall — "
        "or arriving at the same solution from different directions?\n\n"
        "Be specific. Reference paper titles. Keep each section to 2-4 bullet points. "
        "Write for a Pricing PM.\n\n"
        "After the three sections above, add:\n\n"
        "4. **One-Line Takeaway:** The single most surprising or transferable "
        "insight for a Pricing PM this week. One sentence only."
    )

    text = _call_claude(prompt, max_tokens=2048)

    # Extract the one-line takeaway from the end of the response
    takeaway = ""
    for marker in ["**One-Line Takeaway:**", "One-Line Takeaway:"]:
        if marker in text:
            idx = text.index(marker)
            after = text[idx + len(marker):]
            # First non-empty line after the marker
            for line in after.split("\n"):
                line = line.strip().strip("*- ").strip()
                if line:
                    takeaway = line
                    break
            text = text[:idx].strip()
            break

    return {"trends": text, "takeaway": takeaway}


def annotate_failure_signals(failure_tweets: list[dict]) -> list[dict]:
    """
    Call 12: One-line 'why it matters' annotation for each failure signal.
    Only the annotation is Claude-generated; tweet text stays raw.
    """
    if not failure_tweets:
        return []

    tweet_list = "\n".join(
        f"[{i+1}] @{t.get('author', {}).get('username', 'unknown')}: {t['text'][:200]}"
        for i, t in enumerate(failure_tweets)
    )

    prompt = (
        "These are tweets flagged as 'failure signals' — dead ends, shutdowns, "
        "negative results, or pivots in AI/tech.\n\n"
        "For each, write ONE sentence explaining why this matters to a Pricing PM. "
        "What should they take away? What risk does it signal? "
        "What approach should they avoid?\n\n"
        f"Tweets:\n{tweet_list}\n\n"
        "Respond with ONLY valid JSON — no markdown fences.\n"
        'Format: {{"annotations": [{{"index": <1-based>, "why_it_matters": "<one sentence>"}}]}}'
    )

    raw = _call_claude(prompt, max_tokens=1024)
    data = _parse_json(raw)
    annotations = {a["index"]: a["why_it_matters"] for a in data["annotations"]}

    for i, tweet in enumerate(failure_tweets):
        tweet["why_it_matters"] = annotations.get(i + 1, "")

    return failure_tweets


def run_analysis(papers: list[dict], failure_tweets: list[dict]) -> dict:
    """
    Orchestrates all 12 Claude calls.

    Returns:
        {
            "tiered_papers": {1: [...], 2: [...], 3: [...]},
            "trends":        str,
            "takeaway":      str,
            "failure_signals": [annotated tweet dicts],
        }
    """
    # ── Call 1: Tier assignment ──
    print("  [Claude] Assigning tiers...")
    tier_map = tier_assign(papers)

    # Bucket papers by tier, cap at 3 per tier
    tiered: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    for paper in papers:
        tier = tier_map.get(paper["arxiv_id"])
        if tier in (1, 2, 3):
            paper["assigned_tier"] = tier
            if len(tiered[tier]) < 3:
                tiered[tier].append(paper)

    # ── Calls 2-10: Deep analysis (up to 9 papers) ──
    all_analyses: list[dict] = []
    for tier in [1, 2, 3]:
        for paper in tiered[tier]:
            title_short = paper["title"][:60]
            print(f"  [Claude] Deep-analyzing: {title_short}... (Tier {tier})")
            paper["analysis"] = deep_analyze(paper)
            all_analyses.append({
                "title": paper["title"],
                "domain": paper.get("domain", "Unknown"),
                "analysis": paper["analysis"],
            })

    # ── Call 11: Cross-paper synthesis + takeaway ──
    trends = ""
    takeaway = ""
    if all_analyses:
        print("  [Claude] Synthesizing trends...")
        synthesis = synthesize_trends(all_analyses)
        trends = synthesis["trends"]
        takeaway = synthesis.get("takeaway", "")

    # ── Call 12: Failure signal annotation ──
    print("  [Claude] Annotating failure signals...")
    annotated_failures = annotate_failure_signals(failure_tweets)

    return {
        "tiered_papers": tiered,
        "trends": trends,
        "takeaway": takeaway,
        "failure_signals": annotated_failures,
    }
