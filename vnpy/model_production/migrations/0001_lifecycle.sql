PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stage_evidence (
    evidence_digest TEXT PRIMARY KEY, package_digest TEXT NOT NULL, stage TEXT NOT NULL,
    kind TEXT NOT NULL, issued_at_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
    passed INTEGER NOT NULL, invalidated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lifecycle_decisions (
    request_id TEXT PRIMARY KEY, package_digest TEXT NOT NULL, previous_stage TEXT NOT NULL,
    requested_stage TEXT NOT NULL, previous_revision INTEGER NOT NULL,
    applied_revision INTEGER, status TEXT NOT NULL, reason_codes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_outcomes (
    effect_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
    order_id TEXT, reconciliation_revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reconciliation_snapshots (
    revision INTEGER PRIMARY KEY, unresolved_effects_json TEXT NOT NULL,
    discrepancies_json TEXT NOT NULL, evidence_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_incidents (
    incident_id TEXT PRIMARY KEY, package_digest TEXT NOT NULL, lifecycle_revision INTEGER NOT NULL,
    cause TEXT NOT NULL, residual_exposure_json TEXT NOT NULL, unknown_outcomes_json TEXT NOT NULL,
    terminal_state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authoritative_risk_decisions (
    risk_decision_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL UNIQUE,
    accepted INTEGER NOT NULL, reason_codes_json TEXT NOT NULL,
    context_revision INTEGER NOT NULL, evidence_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hard_safety_breakers (
    breaker_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, reason_code TEXT NOT NULL,
    severity TEXT NOT NULL, active INTEGER NOT NULL, evidence_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rollback_records (
    rollback_id TEXT PRIMARY KEY, failed_package_digest TEXT NOT NULL,
    target_package_digest TEXT NOT NULL, previous_revision INTEGER NOT NULL,
    applied_revision INTEGER, residual_exposure_json TEXT NOT NULL,
    unknown_outcomes_json TEXT NOT NULL, state TEXT NOT NULL
);
