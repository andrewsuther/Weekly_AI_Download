"""Tests for common.resilience.retry."""

import random

import pytest

import requests

from common.resilience import RetryError, http_retryable, retry, retry_after_seconds


def _http_error(status: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(response=resp)


def test_http_retryable_transient():
    assert http_retryable(requests.exceptions.Timeout()) is True
    assert http_retryable(requests.exceptions.ConnectionError()) is True
    assert http_retryable(_http_error(429)) is True
    assert http_retryable(_http_error(500)) is True
    assert http_retryable(_http_error(503)) is True


def test_http_retryable_permanent():
    assert http_retryable(_http_error(400)) is False
    assert http_retryable(_http_error(404)) is False
    assert http_retryable(ValueError("nope")) is False
    # HTTPError with no attached response is not classified as retryable.
    assert http_retryable(requests.exceptions.HTTPError()) is False


class _Recorder:
    """Records sleep durations instead of actually sleeping."""

    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def test_succeeds_first_try_no_sleep():
    sleeper = _Recorder()
    calls = []

    @retry(sleep=sleeper)
    def fn():
        calls.append(1)
        return "ok"

    assert fn() == "ok"
    assert len(calls) == 1
    assert sleeper.delays == []


def test_fails_twice_then_succeeds():
    sleeper = _Recorder()
    attempts = {"n": 0}

    @retry(max_attempts=3, base_delay=1.0, jitter=0.0, sleep=sleeper)
    def fn():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ValueError("transient")
        return "done"

    assert fn() == "done"
    assert attempts["n"] == 3
    # base_delay * 2**(attempt-1): 1.0 then 2.0 (no jitter)
    assert sleeper.delays == [1.0, 2.0]


def test_exhaustion_raises_retry_error():
    sleeper = _Recorder()

    @retry(max_attempts=2, base_delay=0.0, jitter=0.0, sleep=sleeper)
    def fn():
        raise ValueError("always")

    with pytest.raises(RetryError) as ei:
        fn()
    assert ei.value.attempts == 2
    assert isinstance(ei.value.last_exc, ValueError)


def test_non_matching_exception_reraises_immediately():
    sleeper = _Recorder()

    @retry(max_attempts=3, retry_on=(KeyError,), sleep=sleeper)
    def fn():
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        fn()
    assert sleeper.delays == []  # never slept


def test_predicate_retry_on():
    sleeper = _Recorder()
    attempts = {"n": 0}

    def only_value_errors(exc):
        return isinstance(exc, ValueError)

    @retry(max_attempts=3, base_delay=0.0, jitter=0.0, retry_on=only_value_errors, sleep=sleeper)
    def fn():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ValueError("x")
        return "ok"

    assert fn() == "ok"
    assert attempts["n"] == 2


def test_max_delay_cap():
    sleeper = _Recorder()

    @retry(max_attempts=5, base_delay=10.0, max_delay=15.0, jitter=0.0, sleep=sleeper)
    def fn():
        raise ValueError("x")

    with pytest.raises(RetryError):
        fn()
    # 10, then min(20,15)=15, then 15, then 15 (4 sleeps for 5 attempts)
    assert sleeper.delays == [10.0, 15.0, 15.0, 15.0]


def test_jitter_within_bounds():
    random.seed(1234)
    sleeper = _Recorder()

    @retry(max_attempts=4, base_delay=10.0, jitter=0.25, sleep=sleeper)
    def fn():
        raise ValueError("x")

    with pytest.raises(RetryError):
        fn()
    expected_bases = [10.0, 20.0, 30.0]
    for delay, base in zip(sleeper.delays, expected_bases):
        assert base * 0.75 <= delay <= base * 1.25


def test_on_giveup_called_once():
    sleeper = _Recorder()
    seen = []

    @retry(max_attempts=2, base_delay=0.0, jitter=0.0, sleep=sleeper, on_giveup=lambda e: seen.append(e))
    def fn():
        raise ValueError("x")

    with pytest.raises(RetryError):
        fn()
    assert len(seen) == 1
    assert isinstance(seen[0], ValueError)


def test_retry_after_overrides_backoff():
    sleeper = _Recorder()
    attempts = {"n": 0}

    def after(exc):
        return 7.5

    @retry(max_attempts=2, base_delay=1.0, jitter=0.0, sleep=sleeper, retry_after=after)
    def fn():
        attempts["n"] += 1
        raise ValueError("x")

    with pytest.raises(RetryError):
        fn()
    assert sleeper.delays == [7.5]


def test_retry_after_capped_at_max_delay():
    sleeper = _Recorder()

    @retry(max_attempts=2, base_delay=1.0, max_delay=10.0, jitter=0.0, sleep=sleeper, retry_after=lambda e: 3600.0)
    def fn():
        raise ValueError("x")

    with pytest.raises(RetryError):
        fn()
    assert sleeper.delays == [10.0]  # capped, not 3600


def test_retry_after_none_falls_back_to_backoff():
    sleeper = _Recorder()

    @retry(max_attempts=2, base_delay=3.0, jitter=0.0, sleep=sleeper, retry_after=lambda e: None)
    def fn():
        raise ValueError("x")

    with pytest.raises(RetryError):
        fn()
    assert sleeper.delays == [3.0]


class _FakeResp:
    def __init__(self, headers):
        self.headers = headers


class _FakeHTTPError(Exception):
    def __init__(self, headers):
        self.response = _FakeResp(headers)


def test_retry_after_seconds_delta():
    exc = _FakeHTTPError({"Retry-After": "120"})
    assert retry_after_seconds(exc) == 120.0


def test_retry_after_seconds_case_insensitive():
    exc = _FakeHTTPError({"retry-after": "5"})
    assert retry_after_seconds(exc) == 5.0


def test_retry_after_seconds_http_date_returns_none():
    exc = _FakeHTTPError({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert retry_after_seconds(exc) is None


def test_retry_after_seconds_no_header():
    assert retry_after_seconds(ValueError("x")) is None
    assert retry_after_seconds(_FakeHTTPError({})) is None
