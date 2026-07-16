"""Retry-with-exponential-backoff primitive shared across the pipeline.

Hand-rolled (no external dependency) so the exact knobs and the sleep hook are
under our control and retry tests run instantly.

Usage::

    from common.resilience import retry, RetryError, retry_after_seconds

    @retry(max_attempts=3, retry_on=_is_retryable, retry_after=retry_after_seconds,
           logger=logger, sleep=lambda s: _SLEEP(s))
    def _do_request():
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp

Only transient errors should be retried. Pass ``retry_on`` either as a tuple of
exception types or as a predicate ``fn(exc) -> bool``. Anything the predicate
rejects re-raises immediately without sleeping. When all attempts are exhausted
the last exception is wrapped in :class:`RetryError`.
"""

from __future__ import annotations

import functools
import random
import time
from typing import Callable, Iterable

__all__ = ["retry", "RetryError", "retry_after_seconds"]


class RetryError(Exception):
    """Raised when every retry attempt has been exhausted."""

    def __init__(self, attempts: int, last_exc: BaseException):
        self.attempts = attempts
        self.last_exc = last_exc
        super().__init__(f"gave up after {attempts} attempt(s): {last_exc!r}")


def _matches(retry_on, exc: BaseException) -> bool:
    """True if ``exc`` is retryable per ``retry_on`` (tuple of types or predicate)."""
    if callable(retry_on) and not isinstance(retry_on, type):
        # A predicate function fn(exc) -> bool.
        try:
            return bool(retry_on(exc))
        except Exception:
            return False
    # A single type or a tuple/iterable of types.
    if isinstance(retry_on, type):
        types: tuple = (retry_on,)
    elif isinstance(retry_on, Iterable):
        types = tuple(retry_on)
    else:  # pragma: no cover - defensive
        types = (Exception,)
    return isinstance(exc, types)


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.25,
    retry_on=(Exception,),
    logger=None,
    on_giveup: Callable[[BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retry_after: Callable[[BaseException], float | None] | None = None,
) -> Callable:
    """Decorator applying exponential backoff with jitter around a callable.

    Args:
        max_attempts: total tries (>=1) before giving up.
        base_delay: seconds for the first backoff; doubles each attempt.
        max_delay: cap for the computed backoff (before jitter).
        jitter: +/- fraction of the delay added as uniform random noise.
        retry_on: tuple of exception types OR predicate ``fn(exc) -> bool``.
            A non-matching exception re-raises immediately (no sleep).
        logger: optional logger for a per-attempt WARNING.
        on_giveup: optional callback invoked with the last exception before
            :class:`RetryError` is raised.
        sleep: sleep function; injected as a lambda in production so tests can
            monkeypatch the module-level clock and run instantly.
        retry_after: optional ``fn(exc) -> float | None``; when it returns a
            value that delay is used instead of the computed backoff (honours
            HTTP ``Retry-After``).
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - classified below
                    if not _matches(retry_on, exc):
                        raise
                    if attempt >= max_attempts:
                        if on_giveup is not None:
                            on_giveup(exc)
                        raise RetryError(attempt, exc) from exc

                    delay = None
                    if retry_after is not None:
                        delay = retry_after(exc)
                        if delay is not None:
                            # Cap a server-provided Retry-After so a huge value
                            # (e.g. 3600s) can't stall the whole run — always
                            # ship a partial digest rather than block for an hour.
                            delay = min(delay, max_delay)
                    if delay is None:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        if jitter:
                            delay += random.uniform(-jitter * delay, jitter * delay)
                        delay = max(0.0, delay)

                    if logger is not None:
                        logger.warning(
                            "retry %s/%s after %.2fs due to %s: %s",
                            attempt,
                            max_attempts,
                            delay,
                            exc.__class__.__name__,
                            exc,
                        )
                    sleep(delay)

        return wrapper

    return decorator


def retry_after_seconds(exc: BaseException) -> float | None:
    """Extract a ``Retry-After`` delay (in seconds) from an HTTP-style exception.

    Reads ``exc.response.headers['Retry-After']``. Only the delta-seconds form
    (e.g. ``"120"``) is supported; the HTTP-date form returns ``None`` so the
    caller falls back to computed backoff.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None  # HTTP-date form not supported; use computed backoff.
    return value if value >= 0 else None
