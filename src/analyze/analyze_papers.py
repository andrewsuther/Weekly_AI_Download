"""Claude API: tier assignment, deep analysis, cross-paper synthesis, failure annotation.

Guiding principle: ALWAYS SHIP A PARTIAL DIGEST. Every Claude call is wrapped in
retry + timeout; if a call still fails after retries or returns unparseable JSON,
the orchestrator falls back gracefully (all-Tier-3 stubs, empty trends, etc.) so
the weekly digest still ships. ``run_analysis`` never raises.
"""

from __future__ import annotations

import json
import os
import time

import anthropic

from common.cost import CostTracker, DEFAULT_MODEL
from common.logging_setup import get_logger, log_event
from common.resilience import retry
from common.run_summary import record_degradation

# Single source of truth: the analyzed model must match the priced model so
# tracker.record(model=MODEL) always hits a PRICING entry.
MODEL = DEFAULT_MODEL
CLAUDE_TIMEOUT_S = 60
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

logger = get_logger(__name__)

# Injected sleep hook so retry backoff can be monkeypatched to a no-op in tests.
_SLEEP = time.sleep

# max_retries=0 so our own retry decorator is the only retry layer.
CLIENT = anthropic.Anthropic(
    api_key=ANTHROPIC_API_KEY,
    max_retries=0,
)

# Transient Claude errors worth retrying. Anything else (auth, bad request,
# 4xx) raises immediately and is caught by per-call degradation handling.
CLAUDE_TRANSIENT = (
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
)


class JSONParseError(ValueError):
    """Raised when a Claude response cannot be parsed into a JSON object."""


class NoApiKey(RuntimeError):
    """Raised when optional Anthropic analysis is not configured."""


def _is_retryable_claude(exc: BaseException) -> bool:
    """True for transient Claude failures (rate limit, timeout, 5xx)."""
    if isinstance(exc, CLAUDE_TRANSIENT):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return getattr(exc, "status_code", 0) >= 500
    return False


def _extract_json_object(raw: str) -> str | None:
    """Return the first balanced ``{...}`` object in ``raw``, or None.

    Brace-depth scan that is string-literal aware: braces inside double-quoted
    strings (including escaped quotes) do not affect depth, so JSON embedded in
    prose or containing braces inside string values is extracted correctly.
    """
    start = None
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    return raw[start : i + 1]
    return None


def _parse_json(raw: str) -> dict:
    """Parse a JSON object from a Claude response, tolerating fences and prose.

    Fast path strips markdown fences and tries ``json.loads`` directly. If that
    fails, a string-literal-aware brace scan extracts the first balanced object
    from surrounding preamble/trailing prose. Raises :class:`JSONParseError` if
    no valid JSON object can be recovered.
    """
    text = raw.strip()

    # Fast path: strip an accidental markdown fence.
    fenced = text
    if fenced.startswith("```"):
        fenced = fenced.split("```", 1)[1]
        if fenced.startswith("json"):
            fenced = fenced[4:]
        if "```" in fenced:
            fenced = fenced.split("```", 1)[0]
    fenced = fenced.strip()
    if fenced:
        try:
            return json.loads(fenced)
        except (json.JSONDecodeError, ValueError):
            pass

    # Recovery path: extract the first balanced {...} from surrounding prose.
    candidate = _extract_json_object(text)
    if candidate is not None:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass

    raise JSONParseError(f"could not parse JSON object from Claude response: {raw[:200]}")


def _fallback_analysis_stub(paper: dict) -> str:
    """Short markdown stub used when deep analysis of a paper fails.

    Renders the abstract (truncated) as a bullet plus a note that the full
    analysis is unavailable. Shape matches what build_report tolerates — it just
    reads ``paper["analysis"]`` as free markdown text.
    """
    abstract = (paper.get("abstract", "") or "").strip()[:400]
    lines = []
    if abstract:
        lines.append(f"- **Abstract:** {abstract}")
    lines.append("- *Full analysis unavailable this week.*")
    return "\n".join(lines)


def _call_claude(
    prompt: str,
    system: str = "",
    max_tokens: int = 4096,
    *,
    call_name: str = "claude",
    tracker: CostTracker | None = None,
) -> tuple[str, object]:
    """Single Claude API call with retry, timeout, and cost tracking.

    Returns ``(text, usage)`` where ``usage`` is the response usage object (or
    None). Transient failures are retried; on exhaustion a
    :class:`~common.resilience.RetryError` propagates to the caller.
    """
    kwargs = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    @retry(
        retry_on=_is_retryable_claude,
        logger=logger,
        sleep=lambda s: _SLEEP(s),
    )
    def _create():
        return CLIENT.messages.create(timeout=CLAUDE_TIMEOUT_S, **kwargs)

    resp = _create()
    usage = getattr(resp, "usage", None)
    if tracker is not None and usage is not None:
        tracker.record(
            model=MODEL,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            label=call_name,
            bucket="default",
        )
    return resp.content[0].text, usage


def tier_assign(papers: list[dict], *, tracker: CostTracker | None = None) -> dict[str, int | None]:
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

    raw, _ = _call_claude(prompt, call_name="tier_assign", tracker=tracker)
    data = _parse_json(raw)
    return {a["arxiv_id"]: a["tier"] for a in data["assignments"]}


def deep_analyze(paper: dict, *, tracker: CostTracker | None = None) -> str:
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

    text, _ = _call_claude(prompt, call_name="deep_analyze", tracker=tracker)
    return text


def synthesize_trends(analyses: list[dict], *, tracker: CostTracker | None = None) -> dict:
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

    text, _ = _call_claude(prompt, max_tokens=2048, call_name="synthesize_trends", tracker=tracker)

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


def annotate_failure_signals(
    failure_tweets: list[dict], *, tracker: CostTracker | None = None
) -> list[dict]:
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

    raw, _ = _call_claude(prompt, max_tokens=1024, call_name="annotate_failures", tracker=tracker)
    data = _parse_json(raw)
    annotations = {a["index"]: a["why_it_matters"] for a in data["annotations"]}

    for i, tweet in enumerate(failure_tweets):
        tweet["why_it_matters"] = annotations.get(i + 1, "")

    return failure_tweets


def _degrade(summary, scope: str, event: str, exc: BaseException) -> None:
    """Log + record a non-fatal degradation for a failed Claude stage."""
    record_degradation(summary, logger, "analyze", scope, exc, event=event)


def run_analysis(
    papers: list[dict],
    failure_tweets: list[dict],
    *,
    tracker: CostTracker | None = None,
    summary=None,
) -> dict:
    """
    Orchestrates all 12 Claude calls with per-call graceful degradation.

    Every stage is wrapped so a failure (retries exhausted, unparseable JSON,
    auth error, etc.) is recorded as a degradation and a fallback is applied —
    the digest always ships. This function never raises.

    Returns:
        {
            "tiered_papers": {1: [...], 2: [...], 3: [...]},
            "trends":        str,
            "takeaway":      str,
            "failure_signals": [annotated tweet dicts],
        }
    """
    if not ANTHROPIC_API_KEY:
        _degrade(
            summary,
            "config",
            "analysis_skipped",
            NoApiKey("ANTHROPIC_API_KEY not set; using abstract-only fallback"),
        )
        tiered = {1: [], 2: [], 3: []}
        for paper in papers[:3]:
            paper["assigned_tier"] = 3
            paper["analysis"] = _fallback_analysis_stub(paper)
            tiered[3].append(paper)
        for tweet in failure_tweets:
            tweet["why_it_matters"] = ""
        return {
            "tiered_papers": tiered,
            "trends": "",
            "takeaway": "",
            "failure_signals": failure_tweets,
        }

    # ── Call 1: Tier assignment ──
    log_event(logger, "analyze", "tier_assign_start", counts={"papers": len(papers)})
    tiered: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    try:
        tier_map = tier_assign(papers, tracker=tracker)
        # Bucket papers by tier, cap at 3 per tier
        for paper in papers:
            tier = tier_map.get(paper["arxiv_id"])
            if tier in (1, 2, 3):
                paper["assigned_tier"] = tier
                if len(tiered[tier]) < 3:
                    tiered[tier].append(paper)
    except Exception as exc:  # noqa: BLE001 - any failure (JSONParseError/RetryError/APIError) → fallback
        # FALLBACK: assign all papers to Tier 3, capped at 3. Still ships.
        _degrade(summary, "tier_assign", "tier_assign", exc)
        tiered = {1: [], 2: [], 3: []}
        for paper in papers[:3]:
            paper["assigned_tier"] = 3
            tiered[3].append(paper)

    # ── Calls 2-10: Deep analysis (up to 9 papers) ──
    all_analyses: list[dict] = []
    for tier in [1, 2, 3]:
        for paper in tiered[tier]:
            title_short = paper["title"][:60]
            log_event(
                logger, "analyze", "deep_analyze_start",
                msg=title_short, x_tier=tier,
            )
            try:
                paper["analysis"] = deep_analyze(paper, tracker=tracker)
            except Exception as exc:  # noqa: BLE001 - single-paper failure → stub, keep going
                # Single-paper failure: stub it, keep the others going.
                _degrade(summary, f"deep_analyze:{paper['arxiv_id']}", "deep_analyze", exc)
                paper["analysis"] = _fallback_analysis_stub(paper)
            all_analyses.append({
                "title": paper["title"],
                "domain": paper.get("domain", "Unknown"),
                "analysis": paper["analysis"],
            })

    # ── Call 11: Cross-paper synthesis + takeaway ──
    trends = ""
    takeaway = ""
    if all_analyses:
        log_event(logger, "analyze", "synthesize_start", counts={"analyses": len(all_analyses)})
        try:
            synthesis = synthesize_trends(all_analyses, tracker=tracker)
            trends = synthesis["trends"]
            takeaway = synthesis.get("takeaway", "")
        except Exception as exc:  # noqa: BLE001 - synthesis failure → empty trends/takeaway
            _degrade(summary, "synthesize_trends", "synthesize_trends", exc)
            trends = ""
            takeaway = ""

    # ── Call 12: Failure signal annotation ──
    log_event(logger, "analyze", "annotate_start", counts={"failures": len(failure_tweets)})
    try:
        annotated_failures = annotate_failure_signals(failure_tweets, tracker=tracker)
    except Exception as exc:  # noqa: BLE001 - annotation failure → empty why_it_matters
        # Leave failure tweets with empty why_it_matters (build_report tolerates).
        _degrade(summary, "annotate_failures", "annotate_failures", exc)
        for tweet in failure_tweets:
            tweet["why_it_matters"] = ""
        annotated_failures = failure_tweets

    return {
        "tiered_papers": tiered,
        "trends": trends,
        "takeaway": takeaway,
        "failure_signals": annotated_failures,
    }
