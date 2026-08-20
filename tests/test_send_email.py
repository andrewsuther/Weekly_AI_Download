"""Tests for Resend delivery resilience + the pure markdown renderer.

Delivery is the last mile: transient failures retry with backoff, and on
ultimate/permanent failure a typed :class:`DeliveryError` is raised for the
caller (main.py) to record. These tests stay fully offline — HTTP is patched
and the retry sleep hook is neutralised.
"""

from unittest.mock import Mock, patch
from datetime import datetime, timezone

import pytest
import requests

from deliver.send_email import (
    RESEND_TIMEOUT_S,
    DeliveryError,
    _inline_md,
    _is_retryable_resend,
    _md_to_html,
    _notify_delivery_failure,
    send_digest,
)

import resend.exceptions as resend_exceptions


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Neutralise retry backoff so tests run instantly."""
    monkeypatch.setattr("deliver.send_email._SLEEP", lambda _s: None)


@pytest.fixture
def _creds(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("RESEND_TO_EMAIL", "to@example.com")


def _ok_resp(payload=None):
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json.return_value = payload if payload is not None else {"id": "abc"}
    return resp


def _http_error(status):
    resp = Mock()
    resp.status_code = status
    err = requests.exceptions.HTTPError(f"HTTP {status}")
    err.response = resp
    return err


def _fake_resend_error(code):
    return resend_exceptions.ResendError(
        code=code,
        error_type="test_error",
        message="boom",
        suggested_action="retry",
    )


# ── Delivery ──────────────────────────────────────────────────────────────


def test_happy_path_returns_dict_and_logs(_creds):
    with patch("deliver.send_email.requests.post", return_value=_ok_resp()) as post, \
            patch("deliver.send_email.log_event") as log_event:
        result = send_digest("# Digest\n\nHello")

    assert result == {"id": "abc"}
    post.assert_called_once()
    _, post_kwargs = post.call_args
    assert post_kwargs["headers"]["Idempotency-Key"].startswith(
        "weekly-ai-download/"
    )
    # id logged on success.
    _, kwargs = log_event.call_args
    assert kwargs.get("email_id") == "abc"
    assert kwargs.get("outcome") == "ok"


@pytest.mark.parametrize("missing", ["RESEND_API_KEY", "RESEND_FROM_EMAIL", "RESEND_TO_EMAIL"])
def test_missing_creds_raises_before_post(_creds, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with patch("deliver.send_email.requests.post") as post:
        with pytest.raises(ValueError):
            send_digest("# Digest")
    post.assert_not_called()


def test_retry_then_success(_creds):
    side_effect = [requests.exceptions.ConnectionError(), _ok_resp()]
    with patch("deliver.send_email.requests.post", side_effect=side_effect) as post:
        result = send_digest("# Digest")
    assert result == {"id": "abc"}
    assert post.call_count == 2


def test_ultimate_failure_raises_delivery_error(_creds):
    with patch(
        "deliver.send_email.requests.post",
        side_effect=requests.exceptions.ConnectionError(),
    ) as post:
        with pytest.raises(DeliveryError):
            send_digest("# Digest")
    assert post.call_count == 3  # max_attempts default


def test_response_missing_id_raises_delivery_error(_creds):
    with patch("deliver.send_email.requests.post", return_value=_ok_resp({})):
        with pytest.raises(DeliveryError):
            send_digest("# Digest")


def test_timeout_kwarg_passed(_creds):
    with patch("deliver.send_email.requests.post", return_value=_ok_resp()) as post:
        send_digest("# Digest")
    _, kwargs = post.call_args
    assert kwargs["timeout"] == RESEND_TIMEOUT_S


def test_digest_date_controls_subject_attachment_and_idempotency(_creds):
    digest_date = datetime(2026, 7, 19, 23, 59, tzinfo=timezone.utc)
    with patch("deliver.send_email.requests.post", return_value=_ok_resp()) as post:
        send_digest("# Digest", digest_date=digest_date)

    _, kwargs = post.call_args
    assert kwargs["json"]["subject"] == "Weekly AI Download \u2014 Jul 19, 2026"
    assert kwargs["json"]["attachments"][0]["filename"] == "weekly_digest_20260719.md"
    assert kwargs["headers"]["Idempotency-Key"].startswith(
        "weekly-ai-download/2026-07-19/"
    )


def test_permanent_http_error_not_retried(_creds):
    with patch(
        "deliver.send_email.requests.post",
        side_effect=_http_error(400),
    ) as post:
        # Non-retryable errors re-raise immediately (not wrapped); the caller
        # never retries a permanent 4xx.
        with pytest.raises(requests.exceptions.HTTPError):
            send_digest("# Digest")
    assert post.call_count == 1  # 4xx is permanent — raised immediately


def test_notify_delivery_failure_is_noop():
    assert _notify_delivery_failure(DeliveryError("x")) is None


# ── Retry predicate ─────────────────────────────────────────────────────────


def test_is_retryable_true_cases():
    assert _is_retryable_resend(requests.exceptions.Timeout()) is True
    assert _is_retryable_resend(requests.exceptions.ConnectionError()) is True
    assert _is_retryable_resend(_http_error(500)) is True
    assert _is_retryable_resend(_http_error(429)) is True
    assert _is_retryable_resend(_fake_resend_error(503)) is True
    assert _is_retryable_resend(_fake_resend_error(429)) is True


def test_is_retryable_false_cases():
    assert _is_retryable_resend(_http_error(400)) is False
    assert _is_retryable_resend(_http_error(404)) is False
    assert _is_retryable_resend(_fake_resend_error(422)) is False
    assert _is_retryable_resend(ValueError("nope")) is False


def test_is_retryable_http_error_no_response():
    err = requests.exceptions.HTTPError("no response")
    err.response = None
    assert _is_retryable_resend(err) is False


# ── Pure renderer ────────────────────────────────────────────────────────────


def test_md_to_html_skips_frontmatter():
    md = "---\ntitle: x\ndate: 2026\n---\n# Real Heading"
    html = _md_to_html(md)
    assert "title: x" not in html
    assert "date: 2026" not in html
    assert "Real Heading" in html


def test_md_to_html_headings():
    html = _md_to_html("# H1\n## H2\n### H3")
    assert "<h1" in html and ">H1</h1>" in html
    assert "<h2" in html and ">H2</h2>" in html
    assert "<h3" in html and ">H3</h3>" in html


def test_md_to_html_bullet_and_link_and_bold():
    html = _md_to_html("- bullet item\n[t](http://e.com) **b** *i*")
    assert "\u2022" in html  # bullet glyph
    assert "bullet item" in html
    assert '<a href="http://e.com"' in html
    assert "<strong>b</strong>" in html
    assert "<em>i</em>" in html


def test_md_to_html_blockquote_and_hr():
    html = _md_to_html("> quoted\n\n---")
    assert "<blockquote" in html and "quoted" in html
    assert "<hr" in html


def test_inline_md_link_bold_italic():
    out = _inline_md("[t](u) **b** *i*")
    assert '<a href="u"' in out and ">t</a>" in out
    assert "<strong>b</strong>" in out
    assert "<em>i</em>" in out
