"""No network, model, email or credentials required."""
import copy
import pytest
from common.signal_lab import FIXTURES, LabRequest, PERSONAS, TOPICS, compare


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("topic", TOPICS)
def test_contract_and_rankings(persona, topic):
    result = compare(LabRequest(persona, topic))
    assert result["mode"] == "synthetic"
    assert result["trusted"][0]["trusted"] is True
    assert result["discovery"][0]["trusted"] is False
    assert {r["id"] for r in result["trusted"]} == {r["id"] for r in result["discovery"]}
    assert all(r["synthetic"] and r["topic"] == topic for r in result["trusted"])


@pytest.mark.parametrize("persona,topic", [("invalid", "privacy"), ("builder", "invalid"), ("", "")])
def test_rejects_unknown_values(persona, topic):
    with pytest.raises(ValueError):
        LabRequest(persona, topic)


def test_deterministic_and_does_not_mutate_fixtures():
    before = copy.deepcopy(FIXTURES)
    request = LabRequest("builder", "reliability")
    assert compare(request) == compare(request)
    assert FIXTURES == before


def test_browser_snapshots_match_contract():
    import json
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "docs" / "signal-lab.html").read_text()
    block = re.search(r'<script id="lab-data" type="application/json">(.*?)</script>', html, re.DOTALL)
    assert block
    snapshots = json.loads(block.group(1))
    assert snapshots == {p + ":" + t: compare(LabRequest(p, t)) for p in PERSONAS for t in TOPICS}
