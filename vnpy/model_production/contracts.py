"""Strict deterministic encodings shared with the Rust model-production boundary."""

from __future__ import annotations

import json
import math
import struct
import unicodedata
from typing import Any


GOLDEN_MESSAGEPACK_HEX = (
    "84b0636f6e74726163745f76657273696f6e01ab656e746974795f74797065"
    "a6676f6c64656ea46e616d65a2c3a9a676616c756573950001ffc3c0"
)


def decode_contract_json(raw: bytes, *, expected_version: int) -> Any:
    """Decode strict JSON and reject duplicate keys, non-finite values, or version drift."""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError(f"non-finite number: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=pairs_hook, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid contract JSON") from exc
    if not isinstance(value, dict) or value.get("contract_version") != expected_version:
        raise ValueError("unsupported contract version")
    return value


def canonical_json_v1(value: Any) -> bytes:
    """Return sorted NFC UTF-8 JSON with finite numbers and normalized negative zero."""

    normalized = _normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_messagepack_v1(value: Any) -> bytes:
    """Encode the canonical subset used by cross-language contract vectors."""

    return _pack(_normalize(value))


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return 0.0 if value == 0.0 else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("contract keys must be strings")
            key = unicodedata.normalize("NFC", key)
            if key in normalized:
                raise ValueError("keys collide after Unicode normalization")
            normalized[key] = _normalize(item)
        return {key: normalized[key] for key in sorted(normalized)}
    raise ValueError(f"unsupported contract value: {type(value).__name__}")


def _pack(value: Any) -> bytes:
    if value is None:
        return b"\xc0"
    if value is False:
        return b"\xc2"
    if value is True:
        return b"\xc3"
    if isinstance(value, int):
        if 0 <= value <= 0x7F:
            return bytes((value,))
        if -32 <= value < 0:
            return bytes((value & 0xFF,))
        if -(2**63) <= value < 0:
            return b"\xd3" + struct.pack(">q", value)
        if value <= 2**64 - 1:
            return b"\xcf" + struct.pack(">Q", value)
        raise ValueError("integer outside MessagePack range")
    if isinstance(value, float):
        return b"\xcb" + struct.pack(">d", value)
    if isinstance(value, str):
        data = value.encode("utf-8")
        if len(data) <= 31:
            return bytes((0xA0 | len(data),)) + data
        if len(data) <= 255:
            return b"\xd9" + bytes((len(data),)) + data
        if len(data) <= 65535:
            return b"\xda" + struct.pack(">H", len(data)) + data
        raise ValueError("string exceeds canonical bound")
    if isinstance(value, list):
        if len(value) > 15:
            raise ValueError("array exceeds canonical golden bound")
        return bytes((0x90 | len(value),)) + b"".join(_pack(item) for item in value)
    if isinstance(value, dict):
        if len(value) > 15:
            raise ValueError("map exceeds canonical golden bound")
        return bytes((0x80 | len(value),)) + b"".join(
            _pack(key) + _pack(item) for key, item in value.items()
        )
    raise ValueError("unsupported MessagePack value")
