"""Tests for common.cost.CostTracker."""

from common.cost import DEFAULT_MODEL, CostTracker


def test_cost_math():
    t = CostTracker()
    call = t.record(model=DEFAULT_MODEL, input_tokens=1000, output_tokens=500, label="x")
    # 1000 * 3/1e6 + 500 * 15/1e6 = 0.003 + 0.0075 = 0.0105
    assert round(call.usd, 6) == 0.0105
    assert round(t.total_usd, 6) == 0.0105


def test_total_tokens_and_aggregations():
    t = CostTracker()
    t.record(model=DEFAULT_MODEL, input_tokens=100, output_tokens=10, label="a", bucket="alice")
    t.record(model=DEFAULT_MODEL, input_tokens=200, output_tokens=20, label="b", bucket="alice")
    t.record(model=DEFAULT_MODEL, input_tokens=300, output_tokens=30, label="a", bucket="bob")
    assert t.total_tokens() == (600, 60)
    by_bucket = t.by_bucket()
    assert set(by_bucket) == {"alice", "bob"}
    by_label = t.by_label()
    assert set(by_label) == {"a", "b"}


def test_unknown_model_zero_cost_no_raise():
    logged = []

    class _Log:
        def warning(self, *a, **k):
            logged.append(a)

    t = CostTracker(_logger=_Log())
    call = t.record(model="mystery-model", input_tokens=1000, output_tokens=1000, label="x")
    assert call.usd == 0.0
    assert t.total_usd == 0.0
    assert logged  # warned


def test_as_dict_shape():
    t = CostTracker()
    t.record(model=DEFAULT_MODEL, input_tokens=1000, output_tokens=500, label="tier", bucket="default")
    d = t.as_dict()
    assert d["call_count"] == 1
    assert d["input_tokens"] == 1000
    assert d["output_tokens"] == 500
    assert d["total_usd"] == 0.0105
    assert d["by_bucket"]["default"] == 0.0105
    assert d["by_label"]["tier"] == 0.0105
