from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import types

import pytest

MODULE_ROOT = Path(__file__).parents[2] / "vnpy" / "model_production"


def _runtime():
    package_name = "vnpy.model_production"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE_ROOT)]
    sys.modules[package_name] = package
    for name in ("contracts", "runtime"):
        qualified = f"{package_name}.{name}"
        spec = importlib.util.spec_from_file_location(qualified, MODULE_ROOT / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.runtime"]


def _digest(label: str) -> str:
    return "blake3:" + hashlib.blake2s(label.encode("ascii")).hexdigest()


def _preparation(runtime):
    return runtime.LoadPreparation(
        preparation_id="prepare-1",
        package_digest=_digest("new"),
        expected_old_package_digest=_digest("old"),
        configuration_digest=_digest("configuration"),
        stage="simulation",
        symbols=("600000.SH",),
        feature_schema_digest=_digest("feature"),
        context_schema_digest=_digest("context"),
        policy_digest=_digest("policy"),
        evidence_bundle_digest=_digest("evidence"),
        ready_token=_digest("ready"),
        expected_old_revision=4,
        cutover_input_sequence=100,
        created_at_ms=1_000,
        expires_at_ms=2_000,
    )


def test_vnpy_issues_exact_signed_commit_and_applies_only_matching_ack(tmp_path) -> None:
    runtime = _runtime()
    authority = runtime.LifecycleRuntimeAuthority(tmp_path / "runtime.sqlite", bytes([17]) * 32)
    authority.initialize_active(_digest("old"), 4)
    request = authority.accept_preparation(_preparation(runtime), 1_100)
    commit = authority.issue_activation_commit(request.preparation_id, 1_200)
    assert commit.producer_identity == "vnpy:model-lifecycle"
    assert commit.expected_old_revision == 4
    assert commit.applied_revision == 5
    assert len(commit.signature) == 128
    assert authority.issue_activation_commit(request.preparation_id, 1_300) == commit

    ack = runtime.ActivationAck(
        commit_id=commit.commit_id,
        old_package_digest=_digest("old"),
        new_package_digest=_digest("new"),
        applied_revision=5,
        activation_epoch=5,
        cutover_sequence=100,
    )
    snapshot = authority.acknowledge(commit.commit_id, ack, 1_400)
    assert snapshot.revision == 5
    assert snapshot.active_package_digest == _digest("new")
    assert authority.acknowledge(commit.commit_id, ack, 1_500) == snapshot


def test_stale_prepare_bad_ack_and_failure_keep_prior_generation(tmp_path) -> None:
    runtime = _runtime()
    authority = runtime.LifecycleRuntimeAuthority(tmp_path / "runtime.sqlite", bytes([17]) * 32)
    authority.initialize_active(_digest("old"), 4)
    stale = runtime.LoadPreparation(
        **{**_preparation(runtime).__dict__, "expected_old_revision": 3}
    )
    with pytest.raises(runtime.RuntimeAuthorityError, match="STALE_ACTIVE_GENERATION"):
        authority.accept_preparation(stale, 1_100)

    request = authority.accept_preparation(_preparation(runtime), 1_100)
    commit = authority.issue_activation_commit(request.preparation_id, 1_200)
    bad_ack = runtime.ActivationAck(
        commit_id=commit.commit_id,
        old_package_digest=_digest("old"),
        new_package_digest=_digest("wrong"),
        applied_revision=5,
        activation_epoch=5,
        cutover_sequence=100,
    )
    with pytest.raises(runtime.RuntimeAuthorityError, match="ACTIVATION_ACK_MISMATCH"):
        authority.acknowledge(commit.commit_id, bad_ack, 1_300)
    assert authority.snapshot().active_package_digest == _digest("old")
    failed = authority.record_failure(request.preparation_id, "WARMUP_FAILED")
    assert failed.revision == 4
    assert failed.active_package_digest == _digest("old")
