from __future__ import annotations

import pytest
from vnpy_tora import ToraStockGateway
from vnpy_xtp import XtpGateway

from vnpy.demo_web.gateway_settings import GatewaySettingsError, map_gateway_settings


def test_xtp_mapping_matches_installed_official_gateway_fields_exactly() -> None:
    mapped = map_gateway_settings(
        "XTP",
        {
            "account": "xtp-demo",
            "client_id": 11,
            "quote_address": "quote.sim.invalid",
            "quote_port": 6001,
            "trading_address": "trade.sim.invalid",
            "trading_port": 6002,
            "log_level": "INFO",
            "quote_protocol": "TCP",
        },
        {"password": "write-only", "authorization_code": "write-only-auth"},
    )

    assert set(mapped) == set(XtpGateway.default_setting)
    assert mapped == {
        "账号": "xtp-demo",
        "密码": "write-only",
        "客户号": 11,
        "授权码": "write-only-auth",
        "行情地址": "quote.sim.invalid",
        "行情端口": 6001,
        "交易地址": "trade.sim.invalid",
        "交易端口": 6002,
        "行情协议": "TCP",
        "日志级别": "INFO",
    }


def test_tora_mapping_matches_installed_official_gateway_fields_exactly() -> None:
    mapped = map_gateway_settings(
        "TORA",
        {
            "account": "tora-demo",
            "product_id": "vnpy",
            "account_type": "资金账号",
            "address_type": "前置地址",
            "quote_server": "tcp://quote.sim.invalid:7001",
            "trading_server": "tcp://trade.sim.invalid:7002",
        },
        {"password": "write-only", "dynamic_key": "write-only-key"},
    )

    assert set(mapped) == set(ToraStockGateway.default_setting)
    assert mapped["动态密钥"] == "write-only-key"
    assert mapped["行情服务器"].startswith("tcp://")


@pytest.mark.parametrize(
    ("gateway", "public", "secrets", "code"),
    [
        ("XTP", {"account": "a"}, {}, "GATEWAY_SETTING_MISSING"),
        ("TORA", {"account": "a"}, {}, "GATEWAY_SETTING_MISSING"),
        ("OTHER", {}, {}, "GATEWAY_NOT_SUPPORTED"),
    ],
)
def test_gateway_mapping_rejects_missing_or_unknown_settings(
    gateway: str,
    public: dict[str, object],
    secrets: dict[str, str],
    code: str,
) -> None:
    with pytest.raises(GatewaySettingsError, match=code):
        map_gateway_settings(gateway, public, secrets)
