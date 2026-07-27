PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS broker_simulation_gateway_bindings (
    binding_digest TEXT PRIMARY KEY,
    gateway TEXT NOT NULL CHECK (gateway IN ('XTP', 'TORA')),
    environment TEXT NOT NULL CHECK (environment = 'broker_simulation'),
    server_fingerprint TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    process_identity TEXT NOT NULL,
    rpc_endpoint TEXT NOT NULL CHECK (rpc_endpoint LIKE '127.0.0.1:%'),
    state_store_path TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    UNIQUE (gateway, server_fingerprint, account_fingerprint, process_identity)
);

CREATE TABLE IF NOT EXISTS broker_simulation_campaigns (
    campaign_id TEXT PRIMARY KEY,
    candidate_digest TEXT NOT NULL,
    package_digest TEXT NOT NULL,
    configuration_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    symbol_set_json TEXT NOT NULL,
    calendar_sessions_json TEXT NOT NULL,
    operator_identity_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'starting', 'active', 'paused', 'invalid', 'completed', 'ready', 'stopped')
    ),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    evidence_digest TEXT,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms)
);

CREATE TABLE IF NOT EXISTS broker_simulation_runs (
    run_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES broker_simulation_campaigns(campaign_id),
    gateway_binding_digest TEXT NOT NULL REFERENCES broker_simulation_gateway_bindings(binding_digest),
    lifecycle_revision INTEGER NOT NULL CHECK (lifecycle_revision >= 1),
    stage TEXT NOT NULL CHECK (stage = 'broker_simulation'),
    session_progress_json TEXT NOT NULL,
    risk_budget_json TEXT NOT NULL,
    ledger_revision INTEGER NOT NULL DEFAULT 0 CHECK (ledger_revision >= 0),
    reconciliation_revision INTEGER NOT NULL DEFAULT 0 CHECK (reconciliation_revision >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'connecting', 'active', 'blocking', 'reconciling', 'completed', 'invalid', 'stopped')
    ),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    UNIQUE (campaign_id, gateway_binding_digest)
);

CREATE TABLE IF NOT EXISTS broker_simulation_operations (
    operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES broker_simulation_runs(run_id),
    intent_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    gateway TEXT NOT NULL CHECK (gateway IN ('XTP', 'TORA')),
    request_digest TEXT NOT NULL,
    risk_decision_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'risk_accepted', 'dispatching', 'accepted', 'rejected', 'partial', 'cancel_pending', 'cancelled', 'unknown', 'reconciled')
    ),
    broker_order_id TEXT,
    reconciliation_revision INTEGER NOT NULL DEFAULT 0 CHECK (reconciliation_revision >= 0),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    UNIQUE (run_id, intent_id)
);

CREATE TABLE IF NOT EXISTS broker_simulation_evidence (
    evidence_digest TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES broker_simulation_campaigns(campaign_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES broker_simulation_runs(run_id),
    gateway_binding_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    event_chain_root TEXT NOT NULL,
    signer_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    net_profit_minor INTEGER NOT NULL,
    reconciled INTEGER NOT NULL CHECK (reconciled IN (0, 1)),
    hard_limit_breaches INTEGER NOT NULL CHECK (hard_limit_breaches >= 0),
    unresolved_outcomes INTEGER NOT NULL CHECK (unresolved_outcomes >= 0),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0)
);

CREATE TABLE IF NOT EXISTS side_master_approval_proposals (
    proposal_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    side_master_identity TEXT NOT NULL,
    source_turn_digest TEXT NOT NULL,
    interpretation TEXT NOT NULL,
    proposed_guidance TEXT NOT NULL,
    provider_outcome TEXT NOT NULL CHECK (provider_outcome IN ('certain', 'uncertain')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'confirmed', 'rejected', 'expired', 'uncertain')),
    confirmation_digest TEXT,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms > created_at_ms),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms)
);

CREATE INDEX IF NOT EXISTS idx_broker_simulation_campaign_state
    ON broker_simulation_campaigns(state, updated_at_ms);
CREATE INDEX IF NOT EXISTS idx_broker_simulation_run_campaign
    ON broker_simulation_runs(campaign_id, state);
CREATE INDEX IF NOT EXISTS idx_broker_simulation_operation_recovery
    ON broker_simulation_operations(run_id, state, updated_at_ms);
CREATE INDEX IF NOT EXISTS idx_side_master_proposal_state
    ON side_master_approval_proposals(state, expires_at_ms);
