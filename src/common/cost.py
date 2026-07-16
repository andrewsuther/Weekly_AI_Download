"""Per-call Claude cost tracking.

Records token usage and estimated USD cost for every Claude call, aggregated
per run and per ``bucket`` label. ``bucket`` defaults to ``"default"`` today but
is the seam for future per-persona cost breakdowns.

Pricing verified 2026-07-16: claude-sonnet-4-5 is $3.00 / 1M input tokens and
$15.00 / 1M output tokens. Edit :data:`PRICING` to update.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["CostTracker", "CallCost", "PRICING", "DEFAULT_MODEL"]

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

PRICING = {
    "claude-sonnet-4-5-20250929": {
        "input_per_token": 3.00 / 1_000_000,
        "output_per_token": 15.00 / 1_000_000,
    },
}


@dataclass
class CallCost:
    model: str
    label: str
    bucket: str
    input_tokens: int
    output_tokens: int
    usd: float


@dataclass
class CostTracker:
    calls: list[CallCost] = field(default_factory=list)
    _logger: object = None

    def record(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        label: str = "",
        bucket: str = "default",
    ) -> CallCost:
        """Record one Claude call. Unknown models cost $0.00 (with a warning)."""
        pricing = PRICING.get(model)
        if pricing is None:
            usd = 0.0
            if self._logger is not None:
                self._logger.warning("no pricing for model %s; cost recorded as $0", model)
        else:
            usd = (
                input_tokens * pricing["input_per_token"]
                + output_tokens * pricing["output_per_token"]
            )
        call = CallCost(
            model=model,
            label=label,
            bucket=bucket,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            usd=usd,
        )
        self.calls.append(call)
        return call

    @property
    def total_usd(self) -> float:
        return sum(c.usd for c in self.calls)

    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(c.input_tokens for c in self.calls),
            sum(c.output_tokens for c in self.calls),
        )

    def by_bucket(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for c in self.calls:
            out[c.bucket] = out.get(c.bucket, 0.0) + c.usd
        return out

    def by_label(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for c in self.calls:
            out[c.label] = out.get(c.label, 0.0) + c.usd
        return out

    def as_dict(self) -> dict:
        in_tok, out_tok = self.total_tokens()
        return {
            "total_usd": round(self.total_usd, 6),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "call_count": len(self.calls),
            "by_bucket": {k: round(v, 6) for k, v in self.by_bucket().items()},
            "by_label": {k: round(v, 6) for k, v in self.by_label().items()},
        }
