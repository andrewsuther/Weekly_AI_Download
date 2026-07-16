"""Tests for common.run_summary and common.logging_setup structured records."""

import json
import logging

from common.logging_setup import _JsonLinesFormatter, log_event
from common.run_summary import Degradation, RunSummary, StageRecord, stage


def _summary():
    return RunSummary(started_at="2026-07-16T00:00:00Z")


def _quiet_logger():
    log = logging.getLogger("test.run_summary")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def test_stage_ok_records_stage():
    s = _summary()
    log = _quiet_logger()
    with stage(s, "fetch_x", log) as body:
        body["counts"] = {"tweets": 5}
    assert len(s.stages) == 1
    rec = s.stages[0]
    assert rec.name == "fetch_x"
    assert rec.outcome == "ok"
    assert rec.counts == {"tweets": 5}
    assert s.degradations == []


def test_stage_suppresses_exception_and_records_degradation():
    s = _summary()
    log = _quiet_logger()
    # The exception must NOT propagate out of the context manager.
    with stage(s, "analyze", log) as body:
        body["counts"] = {"papers": 3}
        raise RuntimeError("boom")
    assert len(s.stages) == 1
    assert s.stages[0].outcome == "failed"
    assert len(s.degradations) == 1
    d = s.degradations[0]
    assert d.stage == "analyze"
    assert d.error_type == "RuntimeError"
    assert d.fatal_to_stage is True
    assert "boom" in d.message


def test_compute_completeness():
    s = _summary()
    assert s.compute_completeness(papers=0, tweets=0, analyzed=False, delivered=False) == "empty"
    assert s.compute_completeness(papers=3, tweets=5, analyzed=True, delivered=True) == "full"
    # a degradation forces partial even when everything nominally ran
    s.add_degradation(Degradation("fetch_arxiv", "domain:cs.AI", "Timeout", "x"))
    assert s.compute_completeness(papers=3, tweets=5, analyzed=True, delivered=True) == "partial"
    # not delivered => partial
    s2 = _summary()
    assert s2.compute_completeness(papers=3, tweets=0, analyzed=True, delivered=False) == "partial"


def test_to_json_round_trips(tmp_path):
    s = _summary()
    s.add_stage(StageRecord(name="fetch_x", duration_s=1.2, outcome="ok", counts={"tweets": 4}))
    s.add_degradation(Degradation("deliver", "resend", "DeliveryError", "no id", fatal_to_stage=True))
    s.cost = {"total_usd": 0.0105}
    s.digest_completeness = "partial"
    s.finished_at = "2026-07-16T00:01:00Z"
    out = tmp_path / "run_summary.json"
    s.write(out)
    data = json.loads(out.read_text())
    assert data["digest_completeness"] == "partial"
    assert data["stages"][0]["name"] == "fetch_x"
    assert data["degradations"][0]["error_type"] == "DeliveryError"
    assert data["cost"]["total_usd"] == 0.0105


def test_log_event_structured_fields_via_caplog(caplog):
    log = logging.getLogger("test.structured")
    log.propagate = True
    with caplog.at_level(logging.INFO):
        log_event(log, "fetch_arxiv", "domain", outcome="ok", duration_s=0.5, domain="cs.AI", new=3)
    rec = caplog.records[-1]
    assert rec.stage == "fetch_arxiv"
    assert rec.event == "domain"
    assert rec.outcome == "ok"
    assert rec.duration_s == 0.5
    assert rec.domain == "cs.AI"


def test_log_event_reserved_field_is_renamed(caplog):
    log = logging.getLogger("test.reserved")
    log.propagate = True
    with caplog.at_level(logging.INFO):
        # "name" and "module" collide with LogRecord attributes; must not raise.
        log_event(log, "s", "e", name="collides", module="also")
    rec = caplog.records[-1]
    assert rec.x_name == "collides"
    assert rec.x_module == "also"


def test_json_formatter_serializes_structured_fields():
    fmt = _JsonLinesFormatter()
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "hello", None, None)
    rec.stage = "deliver"
    rec.event = "send"
    rec.outcome = "ok"
    rec.email_id = "abc123"
    line = fmt.format(rec)
    payload = json.loads(line)
    assert payload["stage"] == "deliver"
    assert payload["event"] == "send"
    assert payload["email_id"] == "abc123"
    assert payload["msg"] == "hello"
