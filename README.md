# Weekly AI Download

Automated weekly digest pipeline. Fetches AI/ML signals from X and arXiv, scores and classifies them, runs tiered analysis via Claude, assembles a report, and delivers it as an email via Resend.

---

## Quick Start

```bash
cp .env.example .env          # fill in 5 secrets
python3 src/main.py           # run the full pipeline
```

Dev test — sends a stub report to Resend without hitting X, arXiv, or Claude:

```bash
python3 dev_test_send.py
```

---

## Secrets

| Secret | Purpose |
|---|---|
| `X_API_BEARER_TOKEN` | X API v2 bearer token |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `RESEND_API_KEY` | Resend API key |
| `RESEND_FROM_EMAIL` | Verified sender address |
| `RESEND_TO_EMAIL` | Recipient address |

For CI: add all 5 as GitHub repo secrets. The workflow fires every Sunday at 18:00 UTC or manually via Actions → Run workflow.

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

### Pipeline

| # | Stage | Modules | External |
|---|---|---|---|
| 1 | Fetch | `fetch_x`, `fetch_arxiv` | X API v2, arXiv Export |
| 2 | Score + Classify | `score_x`, `classify_x` | — |
| 3 | Analyze | `analyze_papers` | Claude API (12 calls) |
| 4 | Build Report | `build_report` | — |
| 5 | Deliver | `send_email` | Resend |

---

## Execution Flow

```
Entry (GH Actions cron or python3 src/main.py)
│
├─ [1] FETCH
│   ├─ fetch_x:     20 handles → resolve IDs (cached) → pull 15 tweets each
│   │               Rate-limit aware: sleep on 429, single retry
│   │               GUARD: X_API_BEARER_TOKEN unset → return []
│   └─ fetch_arxiv: 8 domain queries → parse Atom feed → dedup by arxiv_id
│                   3s courtesy delay between queries
│
├─ [2] SCORE + CLASSIFY
│   ├─ score_x:     eng(0.45) + td(0.15) + sg(0.25) + snr(0.15)
│   │               SNR hard-floor: < 5 total engagements → score = 0
│   └─ classify_x:  Priority order:
│                   1. failure_signal — >= 2 distinct failure group matches
│                   2. high_signal   — >= 1 high group match AND score >= 0.3
│                   3. noise         — dropped from report
│
├─ [3] ANALYZE
│   │               GUARD: papers == [] → skip all Claude calls, use empty stub
│   └─ 12 sequential Claude calls:
│       1.    tier_assign       → batch tier map (1/2/3/null, max 3 per tier)
│       2-10. deep_analyze      → per-paper analysis, tier-specific prompt template
│       11.   synthesize_trends → cross-paper trends + one-line takeaway
│       12.   annotate_failures → per failure tweet "why it matters"
│
├─ [4] BUILD REPORT
│   └─ Markdown: YAML front-matter → 3 tiers → trends
│                → X signals (OSS / Industry) → failures → takeaway
│       Writes: output/digest_report.md
│
└─ [5] DELIVER
    └─ md → HTML (custom inline-style renderer) + .md attachment → Resend
```

---

## Configuration

All tuning below requires zero code changes:

| File | Controls |
|---|---|
| `config/trusted_accounts.yaml` | 20 X handles to monitor |
| `config/x_keywords.yaml` | 5 high-signal + 5 failure-signal keyword groups |
| `config/scoring_weights.yaml` | All composite weights, engagement weights, half-lives, SNR thresholds |
| `config/arxiv_categories.yaml` | 8 research domains → arXiv category codes + search keywords |
| `config/domains.yaml` | Target persona — `role` field flows into every Claude prompt |

---

## Extension Points

**Config-only (no code):** accounts, keywords, scoring weights, arXiv domains, persona.

**Requires code changes:**

| What | Location |
|---|---|
| Classification thresholds (2-group floor, score gate) | `src/score/classify_x.py:31` |
| Scoring formula structure (4 components, SNR hard-floor) | `src/score/score_x.py:104` |
| Claude prompt templates | `src/analyze/analyze_papers.py:46–116` |
| Report section layout and OSS/Industry bucketing | `src/report/build_report.py:86` |
| HTML renderer — adding tables, code blocks, nested lists | `src/deliver/send_email.py:28` |

---

## Risks

| Risk | Location | Severity |
|---|---|---|
| No retry or fallback on any Claude call | `analyze_papers.py` | **High** — single failure crashes the pipeline |
| JSON parse assumes clean Claude output | `analyze_papers.py:27` | **High** — preamble text outside fences = crash |
| No retry on arXiv fetch | `fetch_arxiv.py:93` | Medium — transient outage kills the run |
| Handle→ID cache never invalidated | `config/.account_id_cache.json` | Medium — suspended accounts silently return zero tweets |
| Zero test coverage on external integrations | fetch, analyze, deliver | **High** — every failure path is untested |
| No cross-stream dedup | pipeline-wide | Low — a paper could appear in tiers and X signals |
| YAML re-read on every tweet | `classify_x.py:43`, `score_x.py:113` | Low — 600 redundant file reads per run |

---

## Tests

```bash
python3 -m pytest tests/ -v    # 51 tests, all passing
```

Covered: `score_x`, `classify_x`, `build_report`.
Not covered: `fetch_x`, `fetch_arxiv`, `analyze_papers`, `send_email`.
