import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "vnpy" / "model_production" / "contracts.py"


def _contracts():
    spec = importlib.util.spec_from_file_location("model_production_contracts", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_canonical_json_matches_rust_golden_vector() -> None:
    contracts = _contracts()
    value = {
        "values": [0, 1, -1, True, None],
        "name": "e\u0301",
        "entity_type": "golden",
        "contract_version": 1,
    }
    expected = (
        '{"contract_version":1,"entity_type":"golden","name":"\u00e9",'
        '"values":[0,1,-1,true,null]}'
    ).encode("utf-8")
    assert contracts.canonical_json_v1(value) == expected
    assert contracts.canonical_messagepack_v1(value).hex() == contracts.GOLDEN_MESSAGEPACK_HEX


def test_duplicate_keys_non_finite_numbers_and_unknown_versions_are_rejected() -> None:
    contracts = _contracts()
    for raw in (
        b'{"contract_version":1,"contract_version":2}',
        b'{"contract_version":1,"value":NaN}',
        b'{"contract_version":999}',
    ):
        try:
            contracts.decode_contract_json(raw, expected_version=1)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe contract accepted: {raw!r}")
