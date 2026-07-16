"""Orchestrator — wires all layers of the Weekly AI Download pipeline.

Every stage runs under a ``stage()`` guard so a single failure is logged,
recorded as a degradation, and skipped — the pipeline always ships whatever it
could build (partial digest). The one deliberate non-zero exit is a last-mile
delivery failure, raised only after every artifact has been written to disk.
"""

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
from deliver.send_email import send_digest, _notify_delivery_failure  # noqa: E402
from common.cost import CostTracker              # noqa: E402
from common.logging_setup import configure_logging, get_logger, log_event  # noqa: E402
from common.run_summary import Degradation, RunSummary, stage  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"

def _empty_analysis() -> dict:
    """Fresh empty analysis result (no shared-mutable nested lists)."""
    return {
        "tiered_papers": {1: [], 2: [], 3: []},
        "trends": "",
        "takeaway": "",
        "failure_signals": [],
    }


def _minimal_report(now: datetime, summary: RunSummary) -> str:
    """Fallback digest when build_report itself fails — always ship something."""
    stamp = now.strftime("%b %d, %Y")
    lines = [
        f"# Weekly AI Download — {stamp}",
        "",
        "_This week's digest was generated in a degraded state; some sections "
        "could not be produced._",
        "",
        f"Degradations recorded: {len(summary.degradations)}.",
    ]
    return "\n".join(lines) + "\n"


def run_pipeline(now: datetime, log) -> tuple[RunSummary, str, bool]:
    """Execute all stages, always ship a partial digest.

    Returns (summary, report_path_written, delivery_failed).
    """
    summary = RunSummary(started_at=now.isoformat())
    tracker = CostTracker(_logger=log)

    # Default-initialize every stage output so a suppressed failure can never
    # leave a variable unbound and block shipping.
    raw_tweets: list[dict] = []
    papers: list[dict] = []
    high_signal: list[dict] = []
    failure_signal: list[dict] = []
    noise_count = 0
    analysis_results: dict = _empty_analysis()
    report_md = ""
    delivery_failed = False
    analyze_ok = False

    # ─── Stage: FETCH X ──────────────────────────────────────────────────────
    with stage(summary, "fetch_x", log) as st:
        raw_tweets = fetch_tweets(summary=summary)
        st["counts"] = {"tweets": len(raw_tweets)}

    # ─── Stage: FETCH ARXIV ──────────────────────────────────────────────────
    with stage(summary, "fetch_arxiv", log) as st:
        papers = fetch_papers(summary=summary)
        st["counts"] = {"papers": len(papers)}

    # ─── Stage: CLASSIFY + SCORE ─────────────────────────────────────────────
    with stage(summary, "classify_score", log) as st:
        for tweet in raw_tweets:
            try:
                tweet["score"] = score_tweet(tweet)
                label = classify(tweet, score=tweet["score"])
                tweet["classification"] = label
                if label == "high_signal":
                    high_signal.append(tweet)
                elif label == "failure_signal":
                    failure_signal.append(tweet)
                else:
                    noise_count += 1
            except Exception as exc:  # noqa: BLE001 - one bad tweet must not abort
                log_event(
                    log, "classify_score", "tweet", outcome="degraded",
                    error=exc.__class__.__name__,
                )
                summary.add_degradation(
                    Degradation(
                        "classify_score", "tweet", exc.__class__.__name__,
                        str(exc)[:200], fatal_to_stage=False,
                    )
                )
        high_signal.sort(key=lambda t: t.get("score", 0), reverse=True)
        st["counts"] = {
            "high_signal": len(high_signal),
            "failure_signal": len(failure_signal),
            "noise": noise_count,
        }

    # ─── Stage: ANALYZE (Claude API) ─────────────────────────────────────────
    with stage(summary, "analyze", log) as st:
        if not papers:
            log_event(log, "analyze", "skip", outcome="skipped", reason="no_papers")
            summary.add_degradation(
                Degradation("analyze", "papers", "NoPapers",
                            "no papers fetched; skipping paper analysis", fatal_to_stage=False)
            )
            # No papers to analyze, but any failure_signal tweets that DID succeed
            # must still ship — carry them through un-annotated (why_it_matters="")
            # instead of dropping the source.
            analysis_results = _empty_analysis()
            for tweet in failure_signal:
                tweet.setdefault("why_it_matters", "")
            analysis_results["failure_signals"] = failure_signal
        else:
            analysis_results = run_analysis(
                papers, failure_signal, tracker=tracker, summary=summary
            )
            analyze_ok = True
        st["counts"] = {
            "tiered": sum(len(v) for v in analysis_results["tiered_papers"].values()),
        }

    # ─── Stage: BUILD REPORT ─────────────────────────────────────────────────
    report_path = OUTPUT_DIR / "digest_report.md"
    with stage(summary, "build", log) as st:
        try:
            report_md = build_report(analysis_results, high_signal, date=now)
        except Exception as exc:  # noqa: BLE001 - fall back so we still ship
            log_event(log, "build", "fallback", outcome="degraded",
                      error=exc.__class__.__name__)
            summary.add_degradation(
                Degradation("build", "report", exc.__class__.__name__,
                            str(exc)[:200], fatal_to_stage=False)
            )
            report_md = _minimal_report(now, summary)
        report_path.write_text(report_md, encoding="utf-8")
        st["counts"] = {"bytes": len(report_md)}
        # Only claim "saved" after a successful write inside the guard, so a
        # suppressed write failure can't make the log/summary misreport.
        log_event(log, "build", "saved", path=str(report_path))

    # ─── Stage: DELIVER ──────────────────────────────────────────────────────
    with stage(summary, "deliver", log) as st:
        try:
            response = send_digest(report_md)
            st["counts"] = {"email_id": bool(response.get("id"))}
        except Exception as exc:  # noqa: BLE001 - broad so exit code is always correct
            delivery_failed = True
            st["outcome"] = "failed"  # so the stage record matches the degradation
            log_event(log, "deliver", "failed", outcome="failed",
                      error=exc.__class__.__name__)
            summary.add_degradation(
                Degradation("deliver", "resend", exc.__class__.__name__,
                            str(exc)[:200], fatal_to_stage=True)
            )
            try:
                _notify_delivery_failure(exc)
            except Exception:  # noqa: BLE001 - alert failure must not mask delivery failure
                log_event(log, "deliver", "alert_failed", outcome="failed")

    # ─── Finalize ────────────────────────────────────────────────────────────
    summary.cost = tracker.as_dict()
    summary.finished_at = datetime.now(timezone.utc).isoformat()
    summary.digest_completeness = summary.compute_completeness(
        papers=len(papers),
        tweets=len(raw_tweets),
        analyzed=analyze_ok,
        delivered=not delivery_failed,
    )
    summary.write(OUTPUT_DIR / "run_summary.json", logger=log)

    return summary, str(report_path), delivery_failed


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    configure_logging(json_path=OUTPUT_DIR / "run.log.jsonl")
    log = get_logger("main")
    now = datetime.now(timezone.utc)
    log_event(log, "run", "start", started_at=now.isoformat())

    summary, _report_path, delivery_failed = run_pipeline(now, log)

    log_event(
        log, "run", "complete",
        outcome=summary.digest_completeness,
        degradations=len(summary.degradations),
        total_usd=summary.cost.get("total_usd"),
    )

    # Artifacts are already on disk (report, run_summary.json, run.log.jsonl).
    # Only now signal a red run so a missed send is noticed and fixable.
    if delivery_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
