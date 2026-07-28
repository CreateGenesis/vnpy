"""Map typed internal gateway settings to exact installed vn.py plugin keys."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class GatewaySettingsError(RuntimeError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _XtpPublic(_Strict):
    account: str = Field(min_length=1, max_length=128)
    client_id: int = Field(ge=1, le=255)
    quote_address: str = Field(min_length=1, max_length=512)
    quote_port: int = Field(ge=1, le=65_535)
    trading_address: str = Field(min_length=1, max_length=512)
    trading_port: int = Field(ge=1, le=65_535)
    log_level: Literal["FATAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"]
    quote_protocol: Literal["TCP", "UDP"]


class _XtpSecrets(_Strict):
    password: str = Field(min_length=1, max_length=512)
    authorization_code: str = Field(min_length=1, max_length=512)


class _ToraPublic(_Strict):
    account: str = Field(min_length=1, max_length=128)
    product_id: str = Field(min_length=1, max_length=128)
    account_type: Literal["用户代码", "资金账号"]
    address_type: Literal["前置地址", "FENS地址"]
    quote_server: str = Field(min_length=1, max_length=512)
    trading_server: str = Field(min_length=1, max_length=512)


class _ToraSecrets(_Strict):
    password: str = Field(min_length=1, max_length=512)
    dynamic_key: str = Field(min_length=1, max_length=512)


XTP_SETTING_KEYS = frozenset(
    {"账号", "密码", "客户号", "授权码", "行情地址", "行情端口", "交易地址", "交易端口", "行情协议", "日志级别"}
)
TORA_SETTING_KEYS = frozenset(
    {"账号", "密码", "产品标识", "账号类型", "地址类型", "行情服务器", "交易服务器", "动态密钥"}
)


def map_gateway_settings(
    gateway: str,
    public: dict[str, Any],
    secrets: dict[str, str],
) -> dict[str, str | int]:
    try:
        if gateway == "XTP":
            config = _XtpPublic.model_validate(public)
            secret = _XtpSecrets.model_validate(secrets)
            return {
                "账号": config.account,
                "密码": secret.password,
                "客户号": config.client_id,
                "授权码": secret.authorization_code,
                "行情地址": config.quote_address,
                "行情端口": config.quote_port,
                "交易地址": config.trading_address,
                "交易端口": config.trading_port,
                "行情协议": config.quote_protocol,
                "日志级别": config.log_level,
            }
        if gateway == "TORA":
            config = _ToraPublic.model_validate(public)
            secret = _ToraSecrets.model_validate(secrets)
            return {
                "账号": config.account,
                "密码": secret.password,
                "产品标识": config.product_id,
                "账号类型": config.account_type,
                "地址类型": config.address_type,
                "行情服务器": config.quote_server,
                "交易服务器": config.trading_server,
                "动态密钥": secret.dynamic_key,
            }
    except ValidationError as exc:
        raise GatewaySettingsError("GATEWAY_SETTING_MISSING") from exc
    raise GatewaySettingsError("GATEWAY_NOT_SUPPORTED")
