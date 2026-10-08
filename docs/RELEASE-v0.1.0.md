# Signal Lab v0.1.0
Baseline: 42ff632619e2e6ec4f0984c9196ee1a705c50fec.
Status: local patch awaiting independent review, not a shipped release.

## Before -> after
Before: hand-tuned digest configuration, no browser comparison pilot.
After: offline phone-friendly persona/topic controls compare trusted-first and discovery-first rankings of the same synthetic examples. A selection records session feedback only, never starts a task. Production digest remains unchanged.

## Try it
1. Open docs/signal-lab.html and read the synthetic-data notice.
2. Change persona and topic. Compare ordering, fit reason and next action.
3. Select an action or Neither helped; read the session-only feedback.
4. Reset controls. Reload to confirm feedback is cleared.

## Evidence and limits
194 baseline tests; 208 with feature, all passing in a disposable requirements.in environment with explicit httpx. Hash-locked installation failed on Python 3.10 due to an unpinned conditional dependency; not silently bypassed or fixed. The included optional `scripts/smoke_signal_lab.cjs` browser assertions cover all nine choices, feedback, reset and no horizontal overflow at 390px and 1280px. Separate checker still required on the revised bundle. All three personas currently rank identically within each topic; fit values/reasons change, not ranking. This does not demonstrate personalized ranking. No live feed, model, email, paid API, measured quality lift or production persona integration.

## Undo
Revert this feature commit. It adds the Python contract, HTML pilot, data-build script, optional browser-smoke script, tests and release notes, and edits the README. No database migration, network service or production scheduler change.

## Next proposed step
Ask whether the comparison changes a decision. Improve ranking explanations before approving real-source ingestion or a paid API budget.
