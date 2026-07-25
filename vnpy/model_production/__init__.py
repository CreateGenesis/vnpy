"""Authoritative vn.py model-production lifecycle and risk application."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ActivationAck": ("runtime", "ActivationAck"),
    "ActivationCommit": ("runtime", "ActivationCommit"),
    "AuthoritativeDecisionEngine": ("engine", "AuthoritativeDecisionEngine"),
    "AuthoritativeDecisionResult": ("engine", "AuthoritativeDecisionResult"),
    "AuthoritativeRiskContext": ("risk", "AuthoritativeRiskContext"),
    "HardSafetyController": ("safety", "HardSafetyController"),
    "HardSafetyNotification": ("safety", "HardSafetyNotification"),
    "HardSafetySnapshot": ("safety", "HardSafetySnapshot"),
    "LifecycleRuntimeAuthority": ("runtime", "LifecycleRuntimeAuthority"),
    "LoadPreparation": ("runtime", "LoadPreparation"),
    "ModelIntent": ("risk", "ModelIntent"),
    "ModelProductionApp": ("app", "ModelProductionApp"),
    "ModelProductionEngine": ("app_engine", "ModelProductionEngine"),
    "ModelProductionJournal": ("journal", "ModelProductionJournal"),
    "ModelProductionSnapshot": ("app_engine", "ModelProductionSnapshot"),
    "RiskDecision": ("risk", "RiskDecision"),
    "RiskEvaluator": ("risk", "RiskEvaluator"),
    "RuntimeAuthoritySnapshot": ("runtime", "RuntimeAuthoritySnapshot"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value
