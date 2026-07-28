from __future__ import annotations

from vnpy.demo_web.contracts import SUPPORTED_ACTIONS, build_action_catalog


def by_id(states: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["action_id"]): item for item in states}


def test_catalog_is_complete_and_blocked_actions_keep_remediation() -> None:
    states = build_action_catalog(
        revision=7,
        configuration_active=False,
        candidate_ready=False,
        selected_gateways=set(),
        gateway_states={"XTP": "unconfigured", "TORA": "unconfigured"},
        campaign_state="stopped",
    )
    indexed = by_id(states)

    assert set(indexed) == set(SUPPORTED_ACTIONS)
    assert indexed["gateway.xtp.start"]["state"] == "blocked"
    assert indexed["gateway.xtp.start"]["blockers"][0]["code"] == "CONFIGURATION_NOT_ACTIVE"
    assert indexed["gateway.xtp.start"]["remediation"] == ["open_settings"]
    assert indexed["campaign.emergency_stop"]["state"] == "enabled"


def test_campaign_readiness_ignores_unselected_gateway_and_research_services() -> None:
    states = by_id(
        build_action_catalog(
            revision=9,
            configuration_active=True,
            candidate_ready=True,
            selected_gateways={"XTP"},
            gateway_states={"XTP": "connected", "TORA": "unavailable"},
            campaign_state="stopped",
        )
    )

    assert states["campaign.start"]["state"] == "enabled"
    assert states["gateway.tora.start"]["state"] == "enabled"
    assert states["campaign.start"]["blockers"] == []
