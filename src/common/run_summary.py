"""Run summary + the ``stage()`` guard that enforces always-ship-partial.

``stage()`` times each pipeline stage, records a :class:`Degradation` on failure,
and SUPPRESSES the exception so ``main()`` keeps going. The end-of-run
:class:`RunSummary` is written to ``output/run_summary.json`` and logged.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from .logging_setup import log_event

__all__ = ["Degradation", "StageRecord", "RunSummary", "stage", "record_degradation"]


@dataclass
class Degradation:
    stage: str
    scope: str
    error_type: str
    message: str
    fatal_to_stage: bool = False


@dataclass
class StageRecord:
    name: str
    duration_s: float
    outcome: str
    counts: dict = field(default_factory=dict)


@dataclass
class RunSummary:
    started_at: str
    stages: list = field(default_factory=list)
    degradations: list = field(default_factory=list)
    cost: dict = field(default_factory=dict)
    digest_completeness: str = ""
    finished_at: str = ""

    def add_stage(self, rec: StageRecord) -> None:
        self.stages.append(rec)

    def add_degradation(self, d: Degradation) -> None:
        self.degradations.append(d)

    def compute_completeness(self, *, papers: int, tweets: int, analyzed: bool, delivered: bool) -> str:
        """Classify the run: empty / full / partial."""
        if papers == 0 and tweets == 0:
            return "empty"
        if analyzed and delivered and not self.degradations:
            return "full"
        return "partial"

    def to_json(self) -> str:
        return json.dumps(
            {
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "digest_completeness": self.digest_completeness,
                "stages": [asdict(s) for s in self.stages],
                "degradations": [asdict(d) for d in self.degradations],
                "cost": self.cost,
            },
            indent=2,
            default=str,
        )

    def write(self, path: Path, logger=None) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        if logger is not None:
            log_event(
                logger,
                "run",
                "run_summary",
                outcome=self.digest_completeness or "ok",
                degradations=len(self.degradations),
                total_usd=self.cost.get("total_usd") if self.cost else None,
                path=str(path),
            )


def record_degradation(
    summary,
    logger,
    stage_name: str,
    scope: str,
    exc: BaseException,
    *,
    event: str,
    fatal_to_stage: bool = False,
    **log_fields,
) -> None:
    """Log a degraded event and append a :class:`Degradation` to ``summary``.

    The paired "log + record" pattern is identical across every pipeline module,
    so it lives here once. ``summary`` may be ``None`` (log-only); ``logger`` may
    be ``None`` to skip the log. Extra ``log_fields`` are forwarded to the event.
    """
    message = str(exc)[:200]
    if logger is not None:
        log_event(
            logger,
            stage_name,
            event,
            outcome="degraded",
            error=exc.__class__.__name__,
            msg=message,
            **log_fields,
        )
    if summary is not None:
        summary.add_degradation(
            Degradation(stage_name, scope, exc.__class__.__name__, message, fatal_to_stage)
        )


@contextmanager
def stage(summary: RunSummary, name: str, logger) -> Iterator[dict]:
    """Time a pipeline stage; on failure record a degradation and suppress.

    Yields a mutable ``body`` dict the caller fills with ``counts``. The stage's
    :class:`StageRecord` is appended to ``summary`` on both success and failure.
    Exceptions are logged, recorded as a ``fatal_to_stage`` degradation, and
    SUPPRESSED so the pipeline continues (always ship a partial digest).

    A caller that catches its own exception (so it can run extra cleanup) can
    still mark the stage failed by setting ``body["outcome"] = "failed"``; the
    recorded outcome is then ``failed`` even though no exception escaped.
    """
    body: dict = {"counts": {}}
    start = time.monotonic()
    log_event(logger, name, "start")
    try:
        yield body
    except Exception as exc:  # noqa: BLE001 - deliberate suppression for partial digest
        duration = time.monotonic() - start
        summary.add_degradation(
            Degradation(
                stage=name,
                scope=body.get("scope", name),
                error_type=exc.__class__.__name__,
                message=str(exc)[:200],
                fatal_to_stage=True,
            )
        )
        summary.add_stage(StageRecord(name=name, duration_s=duration, outcome="failed", counts=body.get("counts", {})))
        log_event(logger, name, "end", outcome="failed", duration_s=duration, error=exc.__class__.__name__)
    else:
        duration = time.monotonic() - start
        # Honor a caller-set outcome (e.g. a handled delivery failure) so the
        # stage record can't claim "ok" when the caller knows it failed.
        outcome = body.get("outcome", "ok")
        summary.add_stage(StageRecord(name=name, duration_s=duration, outcome=outcome, counts=body.get("counts", {})))
        # Counts are passed as a single nested field (not spread) so arbitrary
        # count keys populated by callers can't collide with stage/event/outcome.
        log_event(logger, name, "end", outcome=outcome, duration_s=duration, counts=body.get("counts", {}))
