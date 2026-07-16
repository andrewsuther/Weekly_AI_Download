# Agent + Tool Interaction Diagram

Orchestrator invocation sequence, data contracts between tools, and external API boundaries.

Solid arrows = invocation or data handoff.
Dotted arrows = external API calls.
Labels on tool nodes = input/output contract.

```mermaid
graph LR
    subgraph agent["Agent"]
        O["main.py\nOrchestrator"]
    end

    subgraph tools["Tools"]
        FX["fetch_x\n─\nIn: handle list\nOut: tweet[]"]
        FA["fetch_arxiv\n─\nIn: domain configs\nOut: paper[]"]
        SC["score_x + classify_x\n─\nIn: tweet[]\nOut: high[] · failure[]"]
        AN["analyze_papers\n─\nIn: paper[] + failure[]\nOut: tiered · trends · takeaway"]
        BR["build_report\n─\nIn: analysis + high[]\nOut: digest.md"]
        SE["send_email\n─\nIn: digest.md\nOut: email sent"]
    end

    subgraph apis["External APIs"]
        XAPI["X API v2"]
        ARXIV["arXiv"]
        CLAUDE["Claude\n─\n12 calls:\ntier → deep x9\n→ synth → annotate"]
        RESEND["Resend"]
    end

    O -->|"1"| FX
    O -->|"1"| FA
    O -->|"2"| SC
    O -->|"3"| AN
    O -->|"4"| BR
    O -->|"5"| SE

    FX -->|"tweet[]"| SC
    FA -->|"paper[]"| AN
    SC -->|"high[]"| BR
    SC -->|"failure[]"| AN
    AN -->|"analysis"| BR
    BR -->|"digest.md"| SE

    FX -.-> XAPI
    FA -.-> ARXIV
    AN -.-> CLAUDE
    SE -.-> RESEND
```

## Claude Sub-Orchestration

`analyze_papers` is the only tool with internal state. `run_analysis()` sequences 12 Claude calls — each is a stateless, independent API call with no shared context:

```
run_analysis()
│
├─ Call 1:    tier_assign()          — all papers in one prompt → JSON {arxiv_id: tier}
│
├─ Calls 2–10: deep_analyze()      — one paper per call, tier-specific template
│             Tier 1 template: core idea · technique · why it changes the game · cross-domain take · caveats
│             Tier 2 template: problem+solution · results · tools released · cross-domain take · adoption barriers
│             Tier 3 template: key insight · real-world tradeoff · how to use it · performance gains
│
├─ Call 11:  synthesize_trends()    — all deep analyses in → trends markdown + takeaway (extracted via marker)
│
└─ Call 12:  annotate_failure_signals() — all failure tweets in one prompt → JSON {index: why_it_matters}
```

Two calls return structured JSON (`tier_assign`, `annotate_failure_signals`). Two return free-form markdown (`deep_analyze`, `synthesize_trends`). All prompts frame output for a Pricing PM audience.
