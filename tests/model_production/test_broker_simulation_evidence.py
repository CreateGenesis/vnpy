from __future__ import annotations

from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vnpy.model_production.broker_evidence import (
    BrokerEvidenceLedger,
    BrokerRunEvidenceInput,
    evaluate_dual_gateway_readiness,
    verify_evidence_signature,
)


SESSIONS = (
    date(2026, 7, 20),
    date(2026, 7, 21),
    date(2026, 7, 22),
    date(2026, 7, 23),
    date(2026, 7, 24),
)


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def run_input(gateway: str = "XTP", *, ending_equity_minor: int = 1_012_000) -> BrokerRunEvidenceInput:
    return BrokerRunEvidenceInput(
        campaign_id="b53bc59c-c626-4f16-8a3e-a3185c7dad23",
        run_id=(
            "46ef075e-fc3e-4e89-9a9f-4cc951032129"
            if gateway == "XTP"
            else "29a79443-5a03-46ea-b46f-fdb34ddc0f9d"
        ),
        candidate_digest=digest("candidate"),
        gateway=gateway,
        gateway_binding_digest=digest(f"binding:{gateway}"),
        sessions=SESSIONS,
        starting_equity_minor=1_000_000,
        ending_equity_minor=ending_equity_minor,
        external_cashflow_minor=2_000,
        realized_profit_minor=13_000,
        unrealized_profit_minor=1_000,
        fees_minor=4_000,
        reconciled=True,
        hard_limit_breaches=0,
        unresolved_outcomes=0,
        created_at_ms=1_721_961_000_000,
    )


def ledger(tmp_path: Path) -> BrokerEvidenceLedger:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    return BrokerEvidenceLedger(tmp_path, "vnpy:evidence-v1", private_key)


def test_profit_arithmetic_five_sessions_hash_chain_and_signature(tmp_path: Path) -> None:
    evidence_ledger = ledger(tmp_path)
    evidence = evidence_ledger.append(
        run_input(),
        events=(
            {"sequence": 1, "kind": "starting_equity", "amount_minor": 1_000_000},
            {"sequence": 2, "kind": "fill", "order_id": "redacted:order-1"},
            {"sequence": 3, "kind": "ending_equity", "amount_minor": 1_012_000},
        ),
    )

    assert evidence.manifest.net_profit_minor == 10_000
    assert evidence.manifest.net_profit_minor == (
        evidence.manifest.ending_equity_minor
        - evidence.manifest.starting_equity_minor
        - evidence.manifest.external_cashflow_minor
    )
    assert evidence.manifest.net_profit_minor == (
        evidence.manifest.realized_profit_minor
        + evidence.manifest.unrealized_profit_minor
        - evidence.manifest.fees_minor
    )
    assert evidence.manifest.sessions == SESSIONS
    assert evidence.manifest.event_chain_root.startswith("sha256:")
    assert len(evidence.manifest.signature) == 86
    assert verify_evidence_signature(evidence.manifest, evidence_ledger.public_key)
    UUID(evidence.manifest.campaign_id)
    UUID(evidence.manifest.run_id)


def test_nonconsecutive_or_nonfive_session_windows_fail_closed(tmp_path: Path) -> None:
    evidence_ledger = ledger(tmp_path)
    short_window = replace(run_input(), sessions=SESSIONS[:-1])
    broken_window = replace(
        run_input(),
        sessions=(*SESSIONS[:-1], date(2026, 7, 27)),
    )

    for invalid in (short_window, broken_window):
        try:
            evidence_ledger.append(invalid, events=({"sequence": 1, "kind": "invalid"},))
        except ValueError as exc:
            assert "EVIDENCE_SESSION_WINDOW_INVALID" in str(exc)
        else:
            raise AssertionError("invalid evidence window must fail closed")


def test_dual_gateway_requires_independent_positive_reconciled_results(tmp_path: Path) -> None:
    evidence_ledger = ledger(tmp_path)
    xtp = evidence_ledger.append(run_input("XTP"), events=({"sequence": 1, "kind": "close"},))
    tora = evidence_ledger.append(run_input("TORA"), events=({"sequence": 1, "kind": "close"},))

    ready = evaluate_dual_gateway_readiness((xtp.manifest, tora.manifest))
    assert ready.ready
    assert ready.reason_codes == ()
    assert ready.gateway_net_profit_minor == {"TORA": 10_000, "XTP": 10_000}

    losing_tora = replace(tora.manifest, ending_equity_minor=1_001_000, net_profit_minor=-1_000)
    blocked = evaluate_dual_gateway_readiness((xtp.manifest, losing_tora))
    assert not blocked.ready
    assert "GATEWAY_NET_PROFIT_NOT_POSITIVE" in blocked.reason_codes


def test_failed_evidence_is_append_only_hash_chained_and_survives_restart(tmp_path: Path) -> None:
    first_ledger = ledger(tmp_path)
    successful = first_ledger.append(
        run_input("XTP"),
        events=({"sequence": 1, "kind": "complete"},),
    )
    failed = first_ledger.append(
        replace(
            run_input("TORA", ending_equity_minor=1_001_000),
            realized_profit_minor=1_000,
            unrealized_profit_minor=0,
            fees_minor=2_000,
            reconciled=False,
            unresolved_outcomes=1,
        ),
        events=({"sequence": 1, "kind": "unresolved"},),
    )

    assert failed.previous_record_digest == successful.record_digest
    assert not evaluate_dual_gateway_readiness((successful.manifest, failed.manifest)).ready

    recovered = ledger(tmp_path).records()
    assert [record.record_digest for record in recovered] == [
        successful.record_digest,
        failed.record_digest,
    ]
    assert recovered[1].manifest.reconciled is False
    assert recovered[1].manifest.unresolved_outcomes == 1
