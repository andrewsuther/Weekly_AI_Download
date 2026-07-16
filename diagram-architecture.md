# Architecture Diagram

System-level view. Entry points, pipeline stages, external services, config layer, output artifacts.

Solid arrows = runtime data / control flow.
Dotted arrows = config reads or external API calls.

```mermaid
graph TD
    GHA["GitHub Actions\nCron: Sun 18:00 UTC / Manual"] --> MAIN
    CLI["python3 src/main.py"] --> MAIN

    MAIN["main.py — Orchestrator"]

    MAIN --> FETCH["1 — FETCH\nfetch_x · fetch_arxiv"]
    FETCH --> SCORE["2 — SCORE + CLASSIFY\nscore_x · classify_x"]
    SCORE --> ANALYZE["3 — ANALYZE\nanalyze_papers"]
    ANALYZE --> REPORT["4 — BUILD REPORT\nbuild_report"]
    REPORT --> DELIVER["5 — DELIVER\nsend_email"]

    FETCH -.->|"timelines"| XAPI["X API v2"]
    FETCH -.->|"papers"| ARXIV["arXiv Export"]
    ANALYZE -.->|"12 calls"| CLAUDE["Claude API"]
    DELIVER -.->|"email + .md attachment"| RESEND["Resend"]

    CFG["Config Layer\n5 YAML files\naccounts · keywords · weights\narXiv categories · persona"] -.->|"drives"| FETCH
    CFG -.->|"drives"| SCORE
    CFG -.->|"drives"| ANALYZE

    REPORT -->|"writes"| OUT["output/digest_report.md"]
    OUT -->|"uploaded"| ART["GH Actions Artifact"]
```
