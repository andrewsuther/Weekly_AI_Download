"""Orchestrator — wires all 5 layers of the Weekly AI Download pipeline."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Make src/ the import root so layers are importable as packages
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()  # picks up .env for local dev; no-op in CI where env vars are set

from fetch.fetch_arxiv import fetch_papers       # noqa: E402
from fetch.fetch_x import fetch_tweets           # noqa: E402
from score.classify_x import classify            # noqa: E402
from score.score_x import score_tweet            # noqa: E402
from analyze.analyze_papers import run_analysis  # noqa: E402
from report.build_report import build_report     # noqa: E402
from deliver.send_email import send_digest       # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    print(f"[Weekly AI Download] Starting — {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    # ─── Layer 1: FETCH ──────────────────────────────────────────────────────
    print("[1/5] FETCH")
    raw_tweets = fetch_tweets()
    papers = fetch_papers()
    print()

    # ─── Layer 2: CLASSIFY + SCORE ───────────────────────────────────────────
    print("[2/5] CLASSIFY + SCORE")

    # Score every tweet first
    for tweet in raw_tweets:
        tweet["score"] = score_tweet(tweet)

    # Classify using the computed score
    high_signal: list[dict] = []
    failure_signal: list[dict] = []
    noise_count = 0

    for tweet in raw_tweets:
        label = classify(tweet, score=tweet["score"])
        tweet["classification"] = label
        if label == "high_signal":
            high_signal.append(tweet)
        elif label == "failure_signal":
            failure_signal.append(tweet)
        else:
            noise_count += 1

    # Surface highest-scored signals first
    high_signal.sort(key=lambda t: t["score"], reverse=True)

    print(
        f"  {len(raw_tweets)} raw tweets  ->  "
        f"{len(high_signal)} high_signal | "
        f"{len(failure_signal)} failure_signal | "
        f"{noise_count} noise (dropped)"
    )
    print(f"  {len(papers)} papers from arXiv\n")

    # ─── Layer 3: ANALYZE (Claude API) ──────────────────────────────────────
    print("[3/5] ANALYZE")
    if not papers:
        print("  WARNING: No papers fetched. Skipping Claude analysis.")
        analysis_results: dict = {
            "tiered_papers": {1: [], 2: [], 3: []},
            "trends": "",
            "takeaway": "",
            "failure_signals": [],
        }
    else:
        analysis_results = run_analysis(papers, failure_signal)
    print()

    # ─── Layer 4: BUILD REPORT ───────────────────────────────────────────────
    print("[4/5] BUILD REPORT")
    report_md = build_report(analysis_results, high_signal, date=now)

    report_path = OUTPUT_DIR / "digest_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"  Report saved to {report_path}\n")

    # ─── Layer 5: DELIVER ────────────────────────────────────────────────────
    print("[5/5] DELIVER")
    send_digest(report_md)
    print()

    print("[Weekly AI Download] Pipeline complete.")


if __name__ == "__main__":
    main()
