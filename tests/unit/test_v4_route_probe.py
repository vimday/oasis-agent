import json
from pathlib import Path

import pytest
from eth_abi.abi import encode

from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.uniswap_v4.quotes import QuoteDirection, QuoteKind, SinglePoolQuoteRequest
from scripts.run_v4_route_probe import (
    CONFIG_SCHEMA,
    NOT_ENOUGH_LIQUIDITY,
    UNEXPECTED_REVERT_BYTES,
    _classify_quote_error,
    _impact_ppm,
    load_config,
)


def config_payload() -> dict[str, object]:
    probes = []
    for kind in QuoteKind:
        for direction in QuoteDirection:
            probes.extend(
                {"kind": kind.value, "direction": direction.value, "amount": amount}
                for amount in (10, 100, 1_000)
            )
    return {
        "schema": CONFIG_SCHEMA,
        "chain_id": 4663,
        "providers": [
            {"name": "provider-a", "url_env": "RPC_A"},
            {"name": "provider-b", "url_env": "RPC_B"},
        ],
        "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        "universal_router": "0x8876789976decbfcbbbe364623c63652db8c0904",
        "pool_key": {
            "currency0": "0x0000000000000000000000000000000000000001",
            "currency1": "0x0000000000000000000000000000000000000002",
            "fee": 0x800000,
            "tick_spacing": 60,
            "hooks": "0x0000000000000000000000000000000000002880",
        },
        "probes": probes,
        "expected_hashes": {},
    }


def write_config(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_config_resolves_provider_urls_without_retaining_them_in_json(tmp_path: Path) -> None:
    path = write_config(tmp_path / "route.json", config_payload())

    config = load_config(
        path,
        {"RPC_A": "https://rpc-a.example/key-a", "RPC_B": "https://rpc-b.example/key-b"},
    )

    assert config.chain_id == 4663
    assert len(config.endpoints) == 2
    assert config.endpoints[0].url_env == "RPC_A"
    assert len(config.probes) == 12
    assert "key-a" not in path.read_text(encoding="utf-8")


def test_load_config_fails_closed_on_missing_grid_or_provider_secret(tmp_path: Path) -> None:
    payload = config_payload()
    payload["probes"] = list(payload["probes"])[1:]  # type: ignore[arg-type]
    path = write_config(tmp_path / "route.json", payload)

    with pytest.raises(ValueError, match="three sizes"):
        load_config(path, {"RPC_A": "https://rpc-a.example", "RPC_B": "https://rpc-b.example"})

    with pytest.raises(ValueError, match="RPC_B"):
        load_config(
            write_config(tmp_path / "route-2.json", config_payload()),
            {"RPC_A": "https://rpc-a.example"},
        )


def test_impact_uses_output_per_input_for_both_quote_kinds() -> None:
    exact_input = SinglePoolQuoteRequest(QuoteKind.EXACT_INPUT, QuoteDirection.ZERO_FOR_ONE, 1_000)
    exact_output = SinglePoolQuoteRequest(
        QuoteKind.EXACT_OUTPUT, QuoteDirection.ZERO_FOR_ONE, 1_000
    )

    assert _impact_ppm(exact_input, quoted=900, base_amount=100, base_quoted=100) == 100_000
    assert _impact_ppm(exact_output, quoted=1_250, base_amount=100, base_quoted=100) == 200_000


def test_classifies_wrapped_quoter_revert_without_copying_rpc_message() -> None:
    pool_id = bytes.fromhex("12" * 32)
    inner = NOT_ENOUGH_LIQUIDITY + encode(["bytes32"], [pool_id])
    outer = UNEXPECTED_REVERT_BYTES + encode(["bytes"], [inner])
    response = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": 3, "message": "provider-specific text", "data": "0x" + outer.hex()},
        }
    ).encode()
    exchange = RpcExchange("eth_call", 1, b"request", response, 1)

    assert _classify_quote_error(exchange) == {
        "reason": "not_enough_liquidity",
        "revert_selector": "0x7a5ed734",
        "pool_id": "0x" + pool_id.hex(),
    }
