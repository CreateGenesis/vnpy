# Autonomous Model Production in vn.py

The `ModelProductionEngine` is the authoritative A-share lifecycle, hard-risk, reconciliation,
emergency-stop, and broker boundary for Auto Trade `0.1.0`. Rust and Agents provide observation,
research, model artifacts, and versioned requests; they never receive a buy, sell, cancel, gateway,
or risk-mutation API.

## Lifecycle

The evidence path is:

```text
candidate -> pre-training Audit -> training/calibration -> deterministic evaluation -> package
-> release Audit -> simulation/replay/backtest -> paper -> shadow -> gray -> production
```

Replay, backtest, paper, and shadow use broker-inaccessible adapters. Gray and production require a
fresh exact-package request plus independent vn.py admission. A passing review is eligibility only;
it does not apply a stage transition. Package, policy, risk, feature, threshold, prompt, reviewer, or
evidence drift invalidates downstream admission.

An active model emits a bounded `ModelDecision`. `agent_interest` may create one deduplicated Master
wakeup. `order_intent` goes directly to vn.py, where schema, producer, package, stage, sequence,
freshness, account, A-share rules, T+1, lot, cash/position, price limit, breaker, reconciliation, and
gray limits are checked. Only an accepted and durably recorded `RiskDecision` may become an
`OrderRequest`.

## Operator View

The Agent Console consumes one revisioned, digest-chained projection shared with the Master. It shows
bounded Task/Workflow progress, workers, Skills/CLIs, Audit decisions, model packages and stages,
memory health/conflicts/proposals, remaining budgets, intents, risk dispositions, incidents, and
rollback state. It never renders credentials, raw prompts, unrestricted paid content, or worker
context.

The configured operator should not perform routine research or approvals. Deterministic code and
independent Audit Agents own those gates. Operator controls remain for scoped automation disablement
and the external emergency path; no operator or Agent UI action can edit signed evidence or fabricate
approval.

## Recovery and Safety

Unknown broker outcomes block duplicate dispatch, new exposure, model switches, and promotion until
reconciliation. Emergency stop, cancellation, reduction, close, and reconciliation remain available
when Agents, memory, observer, or model inference are unavailable. Rollback targets one exact retained
package and never restores expired policy, stale review, revoked permission, or unreconciled state.

Memory is research-only. vn.py receives redacted working revision, episode health, temporal validity,
conflicts, procedure approval, retrieval cutoff, consolidation, budget, and recovery state. Market
data, accounts, positions, orders, risk, lifecycle, and broker state remain authoritative vn.py data.

## Validation

```powershell
& .venv\Scripts\python.exe -m pytest -q tests\model_production tests\agent_bridge
```

The co-release is eligible only when the Rust workspace, Agent assets, vn.py tests, compatibility,
performance, chaos, authority, secret, live-model, supply-chain, rollback, and objective acceptance
manifests all match the same release digest.
