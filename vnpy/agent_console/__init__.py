"""Read models and research-only controls for Agent visibility in vn.py.

Exports are loaded lazily so pure projection consumers do not import the full trading engine,
TA-Lib, or Qt stack merely to validate an incoming read-model page.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AgentConsoleApp": (".app", "AgentConsoleApp"),
    "AgentConsoleEngine": (".engine", "AgentConsoleEngine"),
    # Keep the package-level import UI-neutral for projection consumers. The
    # real Qt workspace remains available from ``agent_console.ui`` and is
    # the class registered by AgentConsoleApp.
    "AgentConsoleWidget": (".widget", "AgentConsoleWidget"),
    "ConsolePanels": (".widget", "ConsolePanels"),
    "ConsoleState": (".models", "ConsoleState"),
    "ProjectionConsumerAck": (".models", "ProjectionConsumerAck"),
    "EvaluationViewState": (".evaluation", "EvaluationViewState"),
    "ModelCandidateViewState": (".model_candidate", "ModelCandidateViewState"),
    "ModelAuditViewState": (".model_audit", "ModelAuditViewState"),
    "ModelRuntimeViewState": (".model_runtime", "ModelRuntimeViewState"),
    "ModelLifecycleViewState": (".model_lifecycle", "ModelLifecycleViewState"),
    "MemoryProjectionView": (".memory", "MemoryProjectionView"),
    "RedactedModelIntentView": (".model_runtime", "RedactedModelIntentView"),
    "AuditReviewerOutcomeView": (".model_audit", "AuditReviewerOutcomeView"),
    "QualificationViewState": (".qualification", "QualificationViewState"),
    "TikHubViewState": (".tikhub", "TikHubViewState"),
    "WorkflowProjectionView": (".workflow", "WorkflowProjectionView"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
