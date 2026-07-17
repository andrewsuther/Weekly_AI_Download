"""Tests for analyze.analyze_papers — Claude resilience & always-ship-partial.

All tests run offline: CLIENT.messages.create is patched, and the backoff sleep
hook (_SLEEP) is monkeypatched to a no-op so retries run instantly.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import anthropic

import analyze.analyze_papers as ap
from analyze.analyze_papers import (
    JSONParseError,
    _call_claude,
    _extract_json_object,
    _fallback_analysis_stub,
    _is_retryable_claude,
    _parse_json,
    annotate_failure_signals,
    deep_analyze,
    run_analysis,
    synthesize_trends,
    tier_assign,
)
from common.cost import CostTracker
from common.run_summary import RunSummary


# ── helpers ──

def _fake_resp(text, in_tok=10, out_tok=5):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


def _req():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status):
    return cls("boom", response=httpx.Response(status, request=_req()), body=None)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make retry backoff instant for every test in this module."""
    monkeypatch.setattr(ap, "_SLEEP", lambda s: None)


@pytest.fixture
def summary():
    return RunSummary(started_at="2026-07-16T00:00:00Z")


# ── _parse_json / _extract_json_object ──

class TestParseJson:
    def test_clean(self):
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        raw = '```json\n{"a": 1}\n```'
        assert _parse_json(raw) == {"a": 1}

    def test_fenced_no_lang(self):
        raw = '```\n{"a": 1}\n```'
        assert _parse_json(raw) == {"a": 1}

    def test_with_preamble_prose(self):
        raw = 'Sure, here is the JSON you asked for:\n{"a": 1, "b": 2}'
        assert _parse_json(raw) == {"a": 1, "b": 2}

    def test_with_trailing_prose(self):
        raw = '{"a": 1}\nHope that helps!'
        assert _parse_json(raw) == {"a": 1}

    def test_nested_braces(self):
        raw = 'result: {"outer": {"inner": [1, 2]}, "x": 3} done'
        assert _parse_json(raw) == {"outer": {"inner": [1, 2]}, "x": 3}

    def test_braces_inside_string_literal(self):
        raw = '{"note": "use {curly} braces here", "n": 1}'
        assert _parse_json(raw) == {"note": "use {curly} braces here", "n": 1}

    def test_escaped_quote_in_string(self):
        raw = '{"q": "she said \\"hi\\" {ok}", "n": 2}'
        assert _parse_json(raw) == {"q": 'she said "hi" {ok}', "n": 2}

    def test_malformed_raises(self):
        with pytest.raises(JSONParseError):
            _parse_json("no json at all here")

    def test_unbalanced_raises(self):
        with pytest.raises(JSONParseError):
            _parse_json('{"a": 1')


class TestExtractJsonObject:
    def test_ignores_braces_in_strings(self):
        raw = 'x {"s": "a } b { c", "n": 1} y'
        assert _extract_json_object(raw) == '{"s": "a } b { c", "n": 1}'

    def test_first_balanced_object(self):
        raw = '{"a": 1} {"b": 2}'
        assert _extract_json_object(raw) == '{"a": 1}'

    def test_none_when_absent(self):
        assert _extract_json_object("no braces here") is None


# ── _is_retryable_claude ──

class TestIsRetryable:
    def test_rate_limit_retryable(self):
        assert _is_retryable_claude(_status_error(anthropic.RateLimitError, 429))

    def test_5xx_apistatus_retryable(self):
        assert _is_retryable_claude(_status_error(anthropic.APIStatusError, 503))

    def test_internal_server_retryable(self):
        assert _is_retryable_claude(_status_error(anthropic.InternalServerError, 500))

    def test_timeout_retryable(self):
        assert _is_retryable_claude(anthropic.APITimeoutError(_req()))

    def test_bad_request_not_retryable(self):
        assert not _is_retryable_claude(_status_error(anthropic.BadRequestError, 400))

    def test_auth_not_retryable(self):
        assert not _is_retryable_claude(_status_error(anthropic.AuthenticationError, 401))

    def test_4xx_apistatus_not_retryable(self):
        assert not _is_retryable_claude(_status_error(anthropic.APIStatusError, 404))


# ── _fallback_analysis_stub ──

class TestFallbackStub:
    def test_includes_abstract_and_note(self):
        stub = _fallback_analysis_stub({"abstract": "A great paper about pricing."})
        assert "A great paper about pricing." in stub
        assert "Full analysis unavailable this week." in stub

    def test_no_abstract(self):
        stub = _fallback_analysis_stub({})
        assert "Full analysis unavailable this week." in stub


# ── _call_claude ──

class TestCallClaude:
    def test_returns_text_and_usage_and_records(self, monkeypatch):
        monkeypatch.setattr(ap.CLIENT.messages, "create", lambda **kw: _fake_resp("hello", 100, 50))
        tracker = CostTracker()
        text, usage = _call_claude("prompt", call_name="deep_analyze", tracker=tracker)
        assert text == "hello"
        assert usage.input_tokens == 100
        assert tracker.total_usd > 0
        assert tracker.calls[0].label == "deep_analyze"

    def test_no_tracker_ok(self, monkeypatch):
        monkeypatch.setattr(ap.CLIENT.messages, "create", lambda **kw: _fake_resp("hi"))
        text, usage = _call_claude("prompt")
        assert text == "hi"

    def test_passes_timeout(self, monkeypatch):
        captured = {}

        def _create(**kw):
            captured.update(kw)
            return _fake_resp("ok")

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        _call_claude("prompt")
        assert captured["timeout"] == ap.CLAUDE_TIMEOUT_S

    def test_transient_then_success_retries(self, monkeypatch):
        calls = {"n": 0}

        def _create(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise anthropic.APITimeoutError(_req())
            return _fake_resp("recovered")

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        text, _ = _call_claude("prompt")
        assert text == "recovered"
        assert calls["n"] > 1

    def test_non_retryable_raises_immediately(self, monkeypatch):
        calls = {"n": 0}

        def _create(**kw):
            calls["n"] += 1
            raise _status_error(anthropic.BadRequestError, 400)

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        with pytest.raises(anthropic.BadRequestError):
            _call_claude("prompt")
        assert calls["n"] == 1


# ── tier_assign ──

def _papers(n=4):
    return [
        {
            "arxiv_id": f"id{i}",
            "title": f"Paper {i}",
            "authors": ["A", "B", "C", "D"],
            "abstract": f"Abstract {i} " * 50,
            "published": "2026-07-01",
            "domain": "ML",
        }
        for i in range(n)
    ]


class TestTierAssign:
    def test_happy(self, monkeypatch):
        resp = _fake_resp(
            '{"assignments": [{"arxiv_id": "id0", "tier": 1}, {"arxiv_id": "id1", "tier": 2}]}'
        )
        monkeypatch.setattr(ap.CLIENT.messages, "create", lambda **kw: resp)
        result = tier_assign(_papers(2))
        assert result == {"id0": 1, "id1": 2}

    def test_records_tracker(self, monkeypatch):
        resp = _fake_resp('{"assignments": [{"arxiv_id": "id0", "tier": 1}]}')
        monkeypatch.setattr(ap.CLIENT.messages, "create", lambda **kw: resp)
        tracker = CostTracker()
        tier_assign(_papers(1), tracker=tracker)
        assert tracker.calls[0].label == "tier_assign"


# ── deep_analyze template selection ──

class TestDeepAnalyzeTemplates:
    def _capture_prompt(self, monkeypatch):
        captured = {}

        def _create(**kw):
            captured["prompt"] = kw["messages"][0]["content"]
            return _fake_resp("analysis text")

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        return captured

    def test_tier1_fields(self, monkeypatch):
        captured = self._capture_prompt(monkeypatch)
        p = _papers(1)[0]
        p["assigned_tier"] = 1
        deep_analyze(p)
        assert "Why it changes the game" in captured["prompt"]

    def test_tier2_fields(self, monkeypatch):
        captured = self._capture_prompt(monkeypatch)
        p = _papers(1)[0]
        p["assigned_tier"] = 2
        deep_analyze(p)
        assert "Adoption barriers" in captured["prompt"]

    def test_tier3_fields(self, monkeypatch):
        captured = self._capture_prompt(monkeypatch)
        p = _papers(1)[0]
        p["assigned_tier"] = 3
        deep_analyze(p)
        assert "Performance gains" in captured["prompt"]


# ── synthesize_trends ──

class TestSynthesizeTrends:
    def test_extracts_takeaway(self, monkeypatch):
        text = (
            "Trends:\n- something\n\n"
            "**One-Line Takeaway:** Pricing PMs should watch retrieval costs."
        )
        monkeypatch.setattr(ap.CLIENT.messages, "create", lambda **kw: _fake_resp(text))
        out = synthesize_trends([{"title": "T", "domain": "ML", "analysis": "a"}])
        assert out["takeaway"] == "Pricing PMs should watch retrieval costs."
        assert "One-Line Takeaway" not in out["trends"]


# ── annotate_failure_signals ──

class TestAnnotateFailureSignals:
    def test_empty_returns_empty(self):
        assert annotate_failure_signals([]) == []

    def test_happy(self, monkeypatch):
        resp = _fake_resp('{"annotations": [{"index": 1, "why_it_matters": "It signals risk."}]}')
        monkeypatch.setattr(ap.CLIENT.messages, "create", lambda **kw: resp)
        tweets = [{"text": "shutdown", "author": {"username": "u"}}]
        out = annotate_failure_signals(tweets)
        assert out[0]["why_it_matters"] == "It signals risk."

    def test_malformed_raises_jsonparse(self, monkeypatch):
        monkeypatch.setattr(ap.CLIENT.messages, "create", lambda **kw: _fake_resp("not json"))
        tweets = [{"text": "x", "author": {"username": "u"}}]
        with pytest.raises(JSONParseError):
            annotate_failure_signals(tweets)


# ── run_analysis: orchestration & graceful degradation ──

class TestRunAnalysisHappy:
    def test_full_success(self, monkeypatch, summary):
        def _create(**kw):
            prompt = kw["messages"][0]["content"]
            if "Assign each paper" in prompt:
                return _fake_resp(
                    '{"assignments": [{"arxiv_id": "id0", "tier": 1}, {"arxiv_id": "id1", "tier": 3}]}'
                )
            if "emerging trends" in prompt:
                return _fake_resp("Trends\n**One-Line Takeaway:** Big insight.")
            if "failure signals" in prompt:
                return _fake_resp('{"annotations": [{"index": 1, "why_it_matters": "risk"}]}')
            return _fake_resp("deep analysis body")

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        tracker = CostTracker()
        out = run_analysis(
            _papers(2),
            [{"text": "dead end", "author": {"username": "u"}}],
            tracker=tracker,
            summary=summary,
        )
        assert out["tiered_papers"][1][0]["arxiv_id"] == "id0"
        assert out["tiered_papers"][3][0]["arxiv_id"] == "id1"
        assert out["takeaway"] == "Big insight."
        assert out["failure_signals"][0]["why_it_matters"] == "risk"
        assert summary.degradations == []
        assert tracker.total_usd > 0


class TestRunAnalysisDegradation:
    def test_tier_assign_fallback_all_tier3(self, monkeypatch, summary):
        def _create(**kw):
            prompt = kw["messages"][0]["content"]
            if "Assign each paper" in prompt:
                return _fake_resp("total garbage, no json")
            return _fake_resp("deep body")

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        out = run_analysis(_papers(5), [], summary=summary)
        # capped at 3, all in tier 3
        assert len(out["tiered_papers"][3]) == 3
        assert out["tiered_papers"][1] == []
        assert all(p["assigned_tier"] == 3 for p in out["tiered_papers"][3])
        scopes = [d.scope for d in summary.degradations]
        assert "tier_assign" in scopes

    def test_mid_sequence_deep_analyze_failure(self, monkeypatch, summary):
        call_state = {"deep": 0}

        def _create(**kw):
            prompt = kw["messages"][0]["content"]
            if "Assign each paper" in prompt:
                return _fake_resp(
                    '{"assignments": [{"arxiv_id": "id0", "tier": 3}, '
                    '{"arxiv_id": "id1", "tier": 3}, {"arxiv_id": "id2", "tier": 3}]}'
                )
            if "emerging trends" in prompt:
                return _fake_resp("trends")
            # deep_analyze calls: fail the second paper only
            call_state["deep"] += 1
            if call_state["deep"] == 2:
                raise _status_error(anthropic.BadRequestError, 400)
            return _fake_resp("real analysis body")

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        out = run_analysis(_papers(3), [], summary=summary)
        tier3 = out["tiered_papers"][3]
        assert len(tier3) == 3
        # exactly one paper got the stub
        stubbed = [p for p in tier3 if "Full analysis unavailable" in p["analysis"]]
        assert len(stubbed) == 1
        good = [p for p in tier3 if p["analysis"] == "real analysis body"]
        assert len(good) == 2
        scopes = [d.scope for d in summary.degradations]
        assert any(s.startswith("deep_analyze:") for s in scopes)

    def test_synthesize_failure_empty_trends(self, monkeypatch, summary):
        def _create(**kw):
            prompt = kw["messages"][0]["content"]
            if "Assign each paper" in prompt:
                return _fake_resp('{"assignments": [{"arxiv_id": "id0", "tier": 1}]}')
            if "emerging trends" in prompt:
                raise _status_error(anthropic.InternalServerError, 500)
            return _fake_resp("deep body")

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        out = run_analysis(_papers(1), [], summary=summary)
        assert out["trends"] == ""
        assert out["takeaway"] == ""
        assert "synthesize_trends" in [d.scope for d in summary.degradations]

    def test_annotate_failure_empty_why(self, monkeypatch, summary):
        def _create(**kw):
            prompt = kw["messages"][0]["content"]
            if "Assign each paper" in prompt:
                return _fake_resp('{"assignments": [{"arxiv_id": "id0", "tier": 1}]}')
            if "emerging trends" in prompt:
                return _fake_resp("trends")
            if "failure signals" in prompt:
                return _fake_resp("not valid json")
            return _fake_resp("deep body")

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        tweets = [{"text": "dead", "author": {"username": "u"}}]
        out = run_analysis(_papers(1), tweets, summary=summary)
        assert out["failure_signals"][0]["why_it_matters"] == ""
        assert "annotate_failures" in [d.scope for d in summary.degradations]

    def test_total_auth_failure_still_ships(self, monkeypatch, summary):
        """Invalid API key: AuthenticationError on every call → heavily degraded digest."""
        def _create(**kw):
            raise _status_error(anthropic.AuthenticationError, 401)

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        out = run_analysis(
            _papers(5),
            [{"text": "dead", "author": {"username": "u"}}],
            summary=summary,
        )
        # all Tier-3 stubs, empty trends, empty annotations — but it shipped
        assert len(out["tiered_papers"][3]) == 3
        assert all("Full analysis unavailable" in p["analysis"] for p in out["tiered_papers"][3])
        assert out["trends"] == ""
        assert out["takeaway"] == ""
        assert out["failure_signals"][0]["why_it_matters"] == ""
        assert len(summary.degradations) >= 1

    def test_never_raises_without_summary(self, monkeypatch):
        def _create(**kw):
            raise _status_error(anthropic.AuthenticationError, 401)

        monkeypatch.setattr(ap.CLIENT.messages, "create", _create)
        # summary=None must not blow up
        out = run_analysis(_papers(2), [], summary=None)
        assert len(out["tiered_papers"][3]) == 2


# ── degraded-shape render test (build_report tolerates total-failure output) ──

class TestDegradedShapeRenders:
    def test_build_report_renders_stub_output(self):
        from report.build_report import build_report

        papers = _papers(3)
        for p in papers:
            p["assigned_tier"] = 3
            p["url"] = f"https://arxiv.org/abs/{p['arxiv_id']}"
            p["analysis"] = _fallback_analysis_stub(p)

        analysis_results = {
            "tiered_papers": {1: [], 2: [], 3: papers},
            "trends": "",
            "takeaway": "",
            "failure_signals": [
                {"id": "1", "text": "dead end", "author": {"username": "u"}, "why_it_matters": ""}
            ],
        }
        md = build_report(analysis_results, high_signal_tweets=[])
        assert isinstance(md, str)
        assert md
        assert "Full analysis unavailable this week." in md
