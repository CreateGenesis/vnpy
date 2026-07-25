from copy import deepcopy

from vnpy.agent_console import AgentConsoleEngine
from vnpy.agent_console.models import compute_projection_digest


SECTION_NAMES = (
    "task",
    "workflow",
    "workers",
    "qualifications",
    "skills",
    "tools_and_cli",
    "pattern_similarity",
    "solidification",
    "stateless_audit",
    "memory",
    "resources",
    "recovery",
    "model_pipeline",
    "vnpy_authority",
)


def digest(byte: int) -> str:
    return f"blake3:{byte:02x}" + f"{byte:02x}" * 31


def projection(source_revision: int, projection_revision: int, previous: str | None) -> dict:
    value = {
        "contract_version": 1,
        "entity_type": "unified_workflow_projection",
        "projection_id": "workflow-console",
        "projection_revision": projection_revision,
        "projection_digest": digest(250),
        "previous_projection_digest": previous,
        "correlation_id": f"correlation-{projection_revision}",
        "authoritative_source_revisions": {
            name: source_revision for name in SECTION_NAMES
        },
        "created_at_ms": 1_000,
        "expires_at_ms": 10_000,
    }
    for offset, name in enumerate(SECTION_NAMES):
        value[name] = {
            "source_revision": source_revision,
            "source_digest": digest(offset + 1),
            "state": "ready",
            "certainty": "certain",
            "freshness": "fresh",
            "summary": {"count": source_revision, "score": 1.25},
            "evidence_refs": [digest(offset + 30)],
            "permitted_next_actions": ["inspect"],
            "last_error_code": None,
            "updated_at_ms": 1_000 + source_revision,
            "stale": False,
        }
    value["projection_digest"] = compute_projection_digest(value)
    return value


def test_projection_is_monotonic_and_acknowledged_within_two_seconds() -> None:
    console = AgentConsoleEngine()
    first = projection(1, 1, None)
    assert first["projection_digest"] == (
        "blake3:fbed5f7f1b0fb3e6b7ad23a302792503769a88b1aadecb34abdb491a92215ffa"
    )
    ack = console.apply_projection(first, received_at_ms=1_500, rendered_at_ms=1_700)

    assert ack.status == "applied"
    assert ack.latency_ms == 700
    assert ack.latency_ms <= 2_000
    assert console.state.unified_projection_revision == 1
    assert console.state.unified_projection_digest == first["projection_digest"]
    assert console.state.unified_source_revisions["workflow"] == 1
    assert console.next_projection_ack() == ack

    duplicate = console.apply_projection(first, received_at_ms=1_600, rendered_at_ms=1_800)
    assert duplicate.status == "stale_rejected"
    assert duplicate.error_code == "DUPLICATE_PROJECTION"
    assert console.state.unified_projection_revision == 1

    second = projection(2, 2, first["projection_digest"])
    applied = console.apply_projection(second, received_at_ms=1_700, rendered_at_ms=1_900)
    assert applied.status == "applied"
    assert console.state.unified_projection_revision == 2
    assert console.state.unified_projection["workflow"]["source_revision"] == 2


def test_stale_out_of_order_drift_secret_and_digest_updates_preserve_last_valid() -> None:
    console = AgentConsoleEngine()
    first = projection(2, 1, None)
    assert console.apply_projection(first, received_at_ms=1_200, rendered_at_ms=1_300).status == "applied"

    stale = projection(1, 2, first["projection_digest"])
    stale_ack = console.apply_projection(stale, received_at_ms=1_300, rendered_at_ms=1_400)
    assert stale_ack.status == "stale_rejected"
    assert stale_ack.error_code == "STALE_SOURCE_REVISION"

    drift = projection(2, 2, first["projection_digest"])
    drift["task"]["summary"]["count"] = 99
    drift["projection_digest"] = compute_projection_digest(drift)
    drift_ack = console.apply_projection(drift, received_at_ms=1_300, rendered_at_ms=1_400)
    assert drift_ack.status == "invalid_rejected"
    assert drift_ack.error_code == "SOURCE_REVISION_COLLISION"

    secret = projection(3, 2, first["projection_digest"])
    secret["task"]["summary"]["api_key"] = "CANARY_SECRET"
    secret["projection_digest"] = compute_projection_digest(secret)
    secret_ack = console.apply_projection(secret, received_at_ms=1_300, rendered_at_ms=1_400)
    assert secret_ack.status == "invalid_rejected"
    assert secret_ack.error_code == "REDACTION_FAILED"

    tampered = projection(3, 2, first["projection_digest"])
    tampered["projection_digest"] = digest(249)
    tampered_ack = console.apply_projection(tampered, received_at_ms=1_300, rendered_at_ms=1_400)
    assert tampered_ack.status == "invalid_rejected"
    assert tampered_ack.error_code == "DIGEST_MISMATCH"

    out_of_order = projection(4, 3, first["projection_digest"])
    out_of_order_ack = console.apply_projection(
        out_of_order,
        received_at_ms=1_300,
        rendered_at_ms=1_400,
    )
    assert out_of_order_ack.status == "invalid_rejected"
    assert out_of_order_ack.error_code == "OUT_OF_ORDER_PROJECTION"

    assert console.state.unified_projection_revision == 1
    assert console.state.unified_projection_digest == first["projection_digest"]
    assert console.state.unified_projection == first


def test_same_projection_revision_with_different_digest_is_rejected() -> None:
    console = AgentConsoleEngine()
    first = projection(1, 1, None)
    console.apply_projection(first, received_at_ms=1_100, rendered_at_ms=1_200)
    collision = deepcopy(first)
    collision["task"]["summary"]["count"] = 9
    collision["projection_digest"] = compute_projection_digest(collision)
    ack = console.apply_projection(collision, received_at_ms=1_200, rendered_at_ms=1_300)
    assert ack.status == "invalid_rejected"
    assert ack.error_code == "PROJECTION_REVISION_COLLISION"
    assert console.state.unified_projection == first
