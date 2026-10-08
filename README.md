# Weekly AI Download

Automated weekly digest pipeline. Fetches AI/ML signals from X and arXiv, scores and classifies them, runs tiered analysis via Claude, assembles a report, and delivers it as an email via Resend.

---

## Offline Signal Lab (v0.1.0)

Open `docs/signal-lab.html` in a browser. It compares trusted-first and discovery-first rankings for a selected persona and topic using explicitly synthetic examples. It is not a live feed, benchmark or production pipeline change. Feedback is session-only. No keys, email or model calls are used. The same validated contract generates both views; see `src/common/signal_lab.py` and `tests/test_signal_lab.py`.

## Quick Start

```bash
python3 -m pip install --require-hashes -r requirements.txt
python3 src/main.py --no-send # generate and inspect without sending
python3 src/main.py           # generate and send the current week
```

`requirements.in` is the human-edited dependency source. Regenerate the
hash-locked `requirements.txt` with `pip-compile --generate-hashes` whenever a
dependency is intentionally updated.

Dev test — sends a stub report to Resend without hitting X, arXiv, or Claude:

```bash
python3 dev_test_send.py
```

---

## Secrets

| Secret | Purpose |
|---|---|
| `XAI_API_KEY` | Optional xAI key for X search |
| `ANTHROPIC_API_KEY` | Optional Anthropic key for deeper paper analysis |
| `RESEND_API_KEY` | Required Resend API key for delivery |
| `RESEND_FROM_EMAIL` | Required verified sender address |
| `RESEND_TO_EMAIL` | Required recipient address |

For CI, add the three Resend values as GitHub Actions secrets. Add the
optional source/analysis keys when those sections are desired. The schedule
runs every Sunday at 18:00 UTC. Manual runs default to generate-only so an
operator must explicitly enable **Send email**.
Reruns of a scheduled job generate artifacts without sending again; use a new
manual run with **Send email** only when a backfill is intentional.

### Historical replay and recovery

Generate a specific week without sending:

```bash
python3 src/main.py --week-ending 2026-08-16 --no-send \
  --output-dir output/2026-08-16
```

Remove `--no-send` only after inspecting the report. Delivery is blocked when
both paper and X collection are empty. Successful sends create
`delivery_receipt.json`; retries within Resend's idempotency window reuse a
stable key for that week and recipient.

---

## Architecture

| Diagram | What it shows |
|---|---|
| [`diagram-architecture.md`](diagram-architecture.md) | System-level view — entry points, pipeline stages, external services, config, artifacts |
| [`diagram-agent-tools.md`](diagram-agent-tools.md) | Orchestrator + tool interactions — invocation sequence, data contracts, Claude sub-orchestration |

### Inventory

| Category | Items |
|---|---|
| **Entry points** | `src/main.py` (CLI) · `.github/workflows/weekly_digest.yml` (GH Actions cron + manual) |
| **Orchestrators** | `main.py → main()` — top-level pipeline · `analyze_papers.py → run_analysis()` — 12-call Claude sub-orchestrator |
| **Tools** | `fetch_x` · `fetch_arxiv` · `score_x + classify_x` · `analyze_papers` · `build_report` · `send_email` |
| **Agents** | None. Claude is a stateless LLM tool — no agent loops, no tool registries, no memory. |

### Current pipeline (default branch)

1. Fetch: xAI `x_search` across configured topics (`fetch_x`) and the arXiv Atom feed (`fetch_arxiv`). Missing optional keys skip the corresponding service; topic/domain failures are recorded and can yield partial data.
2. Score and classify: content-based fallback when real engagement metrics are unavailable; keyword groups and configured score weights determine inclusion.
3. Analyze: optional Claude tier assignment and paper analysis, with bounded retries, timeouts, JSON recovery and visible fallback summaries.
4. Build: write a Markdown digest and a structured run summary. If report assembly fails, write a clearly degraded report.
5. Deliver: Resend email only when requested and at least one input source produced data. Generate-only and historical replay paths remain available. Delivery failures have a nonzero exit and preserve generated artifacts.

The offline Signal Lab is separate from these stages. Selecting a persona in it does **not** alter the scheduled digest or call live retrieval.

---

## Configuration

The active config-only settings are topics, keyword groups, scoring weights and arXiv categories. Legacy persona and trusted-account files are listed separately and need production code wiring.

| File | Controls |
|---|---|
| `config/x_topics.yaml` | xAI topic queries and focus areas |
| `config/trusted_accounts.yaml` | Legacy trusted-account configuration; current topic fetcher does not read it |
| `config/x_keywords.yaml` | 5 high-signal + 5 failure-signal keyword groups |
| `config/scoring_weights.yaml` | All composite weights, engagement weights, half-lives, SNR thresholds |
| `config/arxiv_categories.yaml` | 8 research domains → arXiv category codes + search keywords |
| `config/domains.yaml` | Intended domain/role settings; current analysis prompts still contain a hard-coded Pricing PM persona |

---

## Extension Points

**Config-only (no code):** topics, keywords, scoring weights and arXiv categories.

**Requires code changes:**

| What | Location |
|---|---|
| Production persona wiring | `src/analyze/analyze_papers.py` (hard-coded prompts) |
| Trusted-account integration | `src/fetch/fetch_x.py` (topic-only fetch path) |
| Classification thresholds (2-group floor, score gate) | `src/score/classify_x.py:31` |
| Scoring formula structure (4 components, SNR hard-floor) | `src/score/score_x.py:104` |
| Claude prompt templates | `src/analyze/analyze_papers.py:46–116` |
| Report section layout and OSS/Industry bucketing | `src/report/build_report.py:86` |
| HTML renderer — adding tables, code blocks, nested lists | `src/deliver/send_email.py:28` |

---

## Current limits and verification

- README and the two older architecture diagrams previously described handle-based X fetching. Current code uses topic search. The diagrams remain historical until separately reconciled.
- Production analysis still contains a hard-coded Pricing PM persona. The new offline persona contract does not claim to fix or connect that production behavior.
- All three personas currently produce the same ordering within each topic; only fit values/reasons change. This pilot does not demonstrate personalized ranking yet.
- Synthetic rankings demonstrate interactions, not improved signal quality. Live trusted-source/discovery comparison and measured outcomes remain future work.
- Model output and source links require review. Retry/fallback code is not a guarantee of factual correctness.
- The advertised hash-locked install failed on Python 3.10 in this test environment: a conditional `exceptiongroup` dependency was unpinned. The lockfile was not regenerated in this change. The regression suite was run in a disposable environment from `requirements.in`, with `httpx` installed explicitly because a test imports it. Do not treat this as verification of the lockfile installation path.
- Current CI runs, API credentials, scheduled delivery and live email receipt have not been verified for this release.

## Tests

```bash
python3 -m pytest tests/ -q
```

On the tested baseline: 194 existing tests passed. With the Signal Lab addition: 208 tests passed, including 14 new offline cases for all nine persona/topic combinations, invalid inputs, determinism and fixture preservation. The browser test exercised all combinations, choice feedback, neither-helped, reset and horizontal overflow at 390px and 1280px. No production fetch, paid API call or email send was executed.

### Reproducible browser smoke

With Node.js, Playwright and Chrome/Chromium installed:

```bash
NODE_PATH=/path/to/node_modules CHROME_PATH=/path/to/chrome node scripts/smoke_signal_lab.cjs
```

This optional development check does not call APIs or email. It opens the local HTML, checks all nine control combinations and feedback/reset behavior at 390px and 1280px, and saves screenshots to a temporary folder. Install Playwright separately in a disposable development environment; no browser dependency is added to the production digest.
