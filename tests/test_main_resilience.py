"""Integration tests for main.run_pipeline — always-ship-partial guarantees."""

import json
import logging
from datetime import datetime, timezone

import pytest

import main as main_mod
from deliver.send_email import DeliveryError


def _now():
    return datetime(2026, 7, 16, tzinfo=timezone.utc)


def _quiet_logger():
    log = logging.getLogger("test.main")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "OUTPUT_DIR", tmp_path)
    return tmp_path


def _good_analysis():
    return {
        "tiered_papers": {
            1: [],
            2: [],
            3: [{"arxiv_id": "1", "title": "T", "authors": ["A"],
                 "url": "u", "analysis": "stub", "domain": "cs.AI"}],
        },
        "trends": "",
        "takeaway": "",
        "failure_signals": [],
    }


def test_happy_path_full(output_dir, monkeypatch):
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda *, summary=None, end_date=None: [
        {"id": "1", "text": "hi", "author": {"username": "x"}},
    ])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda *, summary=None, end_date=None: [
        {"arxiv_id": "1", "title": "T", "authors": ["A"], "abstract": "abs",
         "published": "", "url": "u", "domain": "cs.AI"},
    ])
    monkeypatch.setattr(main_mod, "score_tweet", lambda t: 0.9)
    monkeypatch.setattr(main_mod, "classify", lambda t, score=0.0: "high_signal")
    monkeypatch.setattr(main_mod, "run_analysis",
                        lambda p, f, *, tracker=None, summary=None: _good_analysis())
    monkeypatch.setattr(main_mod, "build_report", lambda a, h, date=None: "# report\n")
    monkeypatch.setattr(main_mod, "send_digest", lambda md, **kwargs: {"id": "abc"})

    summary, report_path, delivery_failed = main_mod.run_pipeline(_now(), _quiet_logger())

    assert delivery_failed is False
    assert summary.digest_completeness == "full"
    assert summary.degradations == []
    # artifacts on disk
    assert (output_dir / "digest_report.md").exists()
    assert (output_dir / "run_summary.json").exists()
    receipt = json.loads((output_dir / "delivery_receipt.json").read_text())
    assert receipt["email_id"] == "abc"
    assert receipt["digest_date"] == "2026-07-16"
    data = json.loads((output_dir / "run_summary.json").read_text())
    assert data["digest_completeness"] == "full"


def test_all_leaves_fail_still_ships(output_dir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(main_mod, "fetch_tweets", boom)
    monkeypatch.setattr(main_mod, "fetch_papers", boom)
    monkeypatch.setattr(main_mod, "run_analysis", boom)
    monkeypatch.setattr(main_mod, "build_report", boom)
    monkeypatch.setattr(main_mod, "send_digest", boom)

    summary, report_path, delivery_failed = main_mod.run_pipeline(_now(), _quiet_logger())

    # pipeline completed without raising; report + summary written
    assert (output_dir / "digest_report.md").exists()
    assert (output_dir / "run_summary.json").exists()
    assert summary.digest_completeness in ("empty", "partial")
    assert summary.degradations  # multiple recorded
    # a minimal report was still produced
    assert (output_dir / "digest_report.md").read_text().strip() != ""


def test_classify_failure_still_builds(output_dir, monkeypatch):
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda *, summary=None, end_date=None: [
        {"id": "1", "text": "hi"},
    ])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda *, summary=None, end_date=None: [])
    # score_tweet raises for every tweet -> per-tweet degradation, empty signals
    monkeypatch.setattr(main_mod, "score_tweet", lambda t: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setattr(main_mod, "classify", lambda t, score=0.0: "high_signal")
    monkeypatch.setattr(main_mod, "build_report", lambda a, h, date=None: "# report\n")
    monkeypatch.setattr(main_mod, "send_digest", lambda md, **kwargs: {"id": "abc"})

    summary, report_path, delivery_failed = main_mod.run_pipeline(_now(), _quiet_logger())

    assert delivery_failed is False
    assert (output_dir / "digest_report.md").exists()
    # classify_score degradation recorded, run still shipped
    assert any(d.stage == "classify_score" for d in summary.degradations)


def test_delivery_failure_writes_artifacts_and_flags(output_dir, monkeypatch):
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda *, summary=None, end_date=None: [])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda *, summary=None, end_date=None: [
        {"arxiv_id": "1", "title": "T", "authors": ["A"], "abstract": "abs",
         "published": "", "url": "u", "domain": "cs.AI"},
    ])
    monkeypatch.setattr(main_mod, "run_analysis",
                        lambda p, f, *, tracker=None, summary=None: _good_analysis())
    monkeypatch.setattr(main_mod, "build_report", lambda a, h, date=None: "# report\n")

    def fail_send(md, **kwargs):
        raise DeliveryError("delivery failed after retries")

    monkeypatch.setattr(main_mod, "send_digest", fail_send)

    summary, report_path, delivery_failed = main_mod.run_pipeline(_now(), _quiet_logger())

    assert delivery_failed is True
    # report + summary still on disk (written before exit)
    assert (output_dir / "digest_report.md").exists()
    assert (output_dir / "run_summary.json").exists()
    assert any(d.stage == "deliver" for d in summary.degradations)
    assert summary.digest_completeness == "partial"


def test_main_exits_nonzero_on_delivery_failure(output_dir, monkeypatch):
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda *, summary=None, end_date=None: [])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda *, summary=None, end_date=None: [])
    monkeypatch.setattr(main_mod, "run_analysis",
                        lambda p, f, *, tracker=None, summary=None: main_mod._empty_analysis())
    monkeypatch.setattr(main_mod, "build_report", lambda a, h, date=None: "# r\n")

    def fail_send(md, **kwargs):
        raise DeliveryError("nope")

    monkeypatch.setattr(main_mod, "send_digest", fail_send)

    with pytest.raises(SystemExit) as ei:
        main_mod.main([])
    assert ei.value.code == 1


def test_empty_digest_is_blocked_before_send(output_dir, monkeypatch):
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda **kwargs: [])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda **kwargs: [])
    monkeypatch.setattr(main_mod, "build_report", lambda a, h, date=None: "# empty\n")
    send_calls = []
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: send_calls.append((a, k)))

    summary, _path, delivery_failed = main_mod.run_pipeline(_now(), _quiet_logger())

    assert delivery_failed is True
    assert send_calls == []
    assert any(d.error_type == "EmptyDigestBlocked" for d in summary.degradations)


def test_generate_only_skips_delivery(output_dir, monkeypatch):
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda **kwargs: [])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda **kwargs: [])
    monkeypatch.setattr(main_mod, "build_report", lambda a, h, date=None: "# preview\n")
    monkeypatch.setattr(
        main_mod, "send_digest", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
    )

    summary, _path, delivery_failed = main_mod.run_pipeline(
        _now(), _quiet_logger(), deliver=False
    )

    assert delivery_failed is False
    assert next(s for s in summary.stages if s.name == "deliver").outcome == "skipped"


def test_historical_week_ending_uses_end_of_day_utc():
    run_time = main_mod._run_time("2026-07-19")
    assert run_time.isoformat() == "2026-07-19T23:59:59+00:00"


def test_failure_signals_ship_when_no_papers(output_dir, monkeypatch):
    """arXiv empty but X failure-signals succeeded → they must still render."""
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda *, summary=None, end_date=None: [
        {"id": "9", "text": "startup shut down", "author": {"username": "acme"}},
    ])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda *, summary=None, end_date=None: [])
    monkeypatch.setattr(main_mod, "score_tweet", lambda t: 0.9)
    monkeypatch.setattr(main_mod, "classify", lambda t, score=0.0: "failure_signal")
    # run_analysis must NOT be called on the no-papers path; guard against it.
    monkeypatch.setattr(main_mod, "run_analysis", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("run_analysis should be skipped when no papers")))
    monkeypatch.setattr(main_mod, "build_report",
                        lambda a, h, date=None: "# r\nfailures: %d\n" % len(a["failure_signals"]))
    monkeypatch.setattr(main_mod, "send_digest", lambda md, **kwargs: {"id": "abc"})

    summary, report_path, delivery_failed = main_mod.run_pipeline(_now(), _quiet_logger())

    report = (output_dir / "digest_report.md").read_text()
    # The one succeeded failure-signal tweet made it into build_report's input.
    assert "failures: 1" in report
    assert delivery_failed is False


def test_deliver_stage_marked_failed_on_delivery_failure(output_dir, monkeypatch):
    """The deliver StageRecord must not claim outcome='ok' when delivery failed."""
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda *, summary=None, end_date=None: [])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda *, summary=None, end_date=None: [
        {"arxiv_id": "1", "title": "T", "authors": ["A"], "abstract": "abs",
         "published": "", "url": "u", "domain": "cs.AI"},
    ])
    monkeypatch.setattr(main_mod, "run_analysis",
                        lambda p, f, *, tracker=None, summary=None: _good_analysis())
    monkeypatch.setattr(main_mod, "build_report", lambda a, h, date=None: "# report\n")
    monkeypatch.setattr(main_mod, "send_digest",
                        lambda md, **kwargs: (_ for _ in ()).throw(DeliveryError("boom")))

    summary, report_path, delivery_failed = main_mod.run_pipeline(_now(), _quiet_logger())

    assert delivery_failed is True
    deliver_stages = [s for s in summary.stages if s.name == "deliver"]
    assert deliver_stages and deliver_stages[0].outcome == "failed"


def test_alert_hook_failure_does_not_mask_delivery(output_dir, monkeypatch):
    monkeypatch.setattr(main_mod, "fetch_tweets", lambda *, summary=None, end_date=None: [])
    monkeypatch.setattr(main_mod, "fetch_papers", lambda *, summary=None, end_date=None: [])
    monkeypatch.setattr(main_mod, "run_analysis",
                        lambda p, f, *, tracker=None, summary=None: main_mod._empty_analysis())
    monkeypatch.setattr(main_mod, "build_report", lambda a, h, date=None: "# r\n")
    monkeypatch.setattr(main_mod, "send_digest",
                        lambda md, **kwargs: (_ for _ in ()).throw(DeliveryError("x")))
    monkeypatch.setattr(main_mod, "_notify_delivery_failure",
                        lambda e: (_ for _ in ()).throw(RuntimeError("alert down")))

    summary, report_path, delivery_failed = main_mod.run_pipeline(_now(), _quiet_logger())
    # delivery failure flag survives an alert-hook exception
    assert delivery_failed is True
