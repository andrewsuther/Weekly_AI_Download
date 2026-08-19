# Project Context

context_schema: 1
revision: 3
updated_at: 2026-08-19
updated_by: codex

phase: Ship
active_slice: Reliable weekly generation, delivery, and historical replay
proof_target: A historical week generates non-empty artifacts, sends once, and records a delivery receipt.
owner: Andrew
status: blocked

## Stack and conventions
- GitHub Actions runs the scheduled Python pipeline.
- API credentials stay in GitHub Actions secrets or local `.env`; never in Git.
- Empty digests are artifacts for diagnosis, not emails.

## Definition of done
- Required delivery configuration fails before the pipeline starts.
- A caller can generate any week with `--week-ending` and preview with `--no-send`.
- arXiv uses the documented `/api/query` endpoint with HTTPS and a user agent.
- Resend retries use a stable per-week idempotency key and successful sends write a receipt.
- The five failed weeks are backfilled exactly once with delivery evidence.

## Decisions
- 2026-08-19: Keep X and Anthropic optional; require only the three Resend values for a send.
- 2026-08-19: Block empty-email delivery instead of treating an empty artifact as a useful digest.
- 2026-08-19: Scheduled reruns are generate-only; an intentional backfill requires a new manual run with send enabled.
- 2026-08-19: Missing optional Anthropic configuration takes one explicit abstract-only fallback path and makes no provider calls.

## Open risks
- Resend confirms API acceptance, not final inbox delivery; verify the recipient inbox after the backfill.
- X history depends on an optional xAI key and may remain absent from historical replays.
- Publishing the four local credentials to encrypted Actions secrets requires Andrew's explicit approval.

## Next action
- Andrew: explicitly approve copying the existing Anthropic and Resend values into this repository's Actions secrets.

## Last verified
- 2026-08-19: Five regenerated reports contained 5-9 analyzed papers each; Resend accepted all five and receipts were saved.
- 2026-08-19: The repaired branch completed a hosted generate-only workflow in 45 seconds, including tests, report generation, and artifact upload.
- 2026-08-19: 194 tests passed and pip-audit found no known vulnerabilities.
