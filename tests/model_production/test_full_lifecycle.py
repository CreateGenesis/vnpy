from __future__ import annotations

from vnpy.model_production.lifecycle import LifecycleAuthority, LifecycleRequest, ProductionEligibility


def test_exact_package_advances_replay_backtest_paper_shadow_gray_and_production() -> None:
    digest = "blake3:" + "a" * 64
    authority = LifecycleAuthority(digest, "blake3:" + "b" * 64, "blake3:" + "c" * 64, "evaluated", 3)
    for index, target in enumerate(("simulation", "paper", "shadow", "gray"), start=1):
        result = authority.apply(LifecycleRequest.master(f"request-{index}", target, authority.snapshot(), 1_000, 2_000), 1_500, gates=())
        assert result.accepted
    eligibility = ProductionEligibility(10, 200, True, 0, 0, 0, "request-production")
    request = LifecycleRequest.master("request-production", "production", authority.snapshot(), 1_000, 2_000)
    result = authority.apply(request, 1_500, gates=eligibility.reason_codes("request-gray"))
    assert result.accepted
    assert authority.snapshot().stage == "production"
