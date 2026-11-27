"""Check the old two-pool gas estimates and score the later independently frozen envelope."""

import json
from pathlib import Path
from statistics import median_low

from eth_abi import decode
from eth_hash.auto import keccak

from oasis_agent.evidence.robinhood_quote import (
    CHAIN_ID,
    parse_routes,
    quote_multicall,
    read_quotes,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import measure_robinhood_quote_delivery as quote

ROOT = Path(__file__).resolve().parents[3]
RESEARCH = ROOT / "reports/research"
LOCK = RESEARCH / "robinhood-route-gas-calibration-20261127/rule-lock.json"
NEW = RESEARCH / "robinhood-gold-window-20261127/window"
SOURCES = {"official", "publicnode"}


def load(path):
    return json.loads(path.read_bytes())


def wire_pairs(directory):
    index = {}
    for path in directory.glob("*-*.request"):
        request = load(path)
        key = (request["method"], canonical_json(request["params"]))
        lookup = index.setdefault(key, {})
        source = path.stem.split("-")[-1]
        assert source not in lookup
        lookup[source] = path

    def pair(method, params):
        found = index[method, canonical_json(params)]
        assert set(found) == SOURCES
        out = []
        for source in sorted(SOURCES):
            path = found[source]
            meta = load(path.with_suffix(".meta"))
            response = path.with_suffix(".response").read_bytes()
            payload = json.loads(response)
            assert meta["status"] == 200 and meta["received_ns"] >= meta["sent_ns"]
            assert meta["request_sha256"] == sha256_hex(path.read_bytes())
            assert meta["response_sha256"] == sha256_hex(response)
            assert payload["id"] == load(path)["id"] and payload["jsonrpc"] == "2.0"
            out.append(
                payload["result"] if "result" in payload else {"revert": payload["error"]["data"]}
            )
        return out

    return pair, index


def inspect_window(directory):
    frozen = load(directory / "frozen.json")
    tx = load(directory / "envelope.json")
    captured = load(directory / "result.json")
    pins = load(directory / "plan.json")["runtime_keccaks"]
    assert (
        sha256_hex(canonical_json(tx)) == frozen["envelope_sha256"] and captured["frozen"] == frozen
    )
    routes = parse_routes(load(directory / "routes.json"))
    route = next(r for r in routes if r.name == frozen["route"])
    assert tx["data"] == route.shadow_router_data(frozen["minout_wei"], frozen["deadline"])
    assert (
        route.amount == int(tx["value"], 16) == frozen["principal_wei"]
        and int(tx["chainId"], 16) == CHAIN_ID
    )
    assert int(tx["maxPriorityFeePerGas"], 16) == 0
    tx_common = {k: v for k, v in tx.items() if k not in ("nonce", "value", "data")}
    keys = [*quote._runtime_pins(), quote.MULTICALL3, tx["to"]]
    code = (
        b"".join(
            b"\x73" + bytes.fromhex(addr[2:]) + b"\x3f\x60" + bytes([i * 32]) + b"\x52"
            for i, addr in enumerate(keys)
        )
        + b"\x60\xa0\x60\x00\xf3"
    )
    probe = {"data": "0x" + code.hex(), "gas": "0x30000"}
    quote_call = {
        "to": quote.MULTICALL3,
        "data": quote_multicall(routes),
        "gas": hex(quote.TRANSPORT_GAS),
    }
    pair, index = wire_pairs(directory)
    phases = {}
    for state in captured["states"]:
        height = state["n"]
        pin = {"blockHash": state["h"], "requireCanonical": True}
        fee = state["fee"]
        hdrs = pair("eth_getBlockByNumber", [hex(height), False])
        assert all(b["hash"] == state["h"] and int(b["baseFeePerGas"], 16) == fee for b in hdrs)
        assert pair("eth_call", [probe, pin]) == ["0x" + "".join(pins[k] for k in keys)] * 2
        quotes = pair("eth_call", [quote_call, pin])
        assert quotes[0] == quotes[1]
        result = read_quotes(quotes[0], routes, fee)
        observation = next(x for x in result["quotes"] if x["route"] == route.name)
        assert observation in state["quotes"]
        calls = pair("eth_call", [tx, pin])
        gas = None
        revert_gap = None
        if calls == ["0x"] * 2:
            gas = [int(r, 16) for r in pair("eth_estimateGas", [tx, pin])]
            assert gas == state["estimate_gas"]
        else:
            assert calls[0] == calls[1]
            raw = bytes.fromhex(calls[0]["revert"][2:])
            assert raw[:4] == keccak(b"V4TooLittleReceived(uint256,uint256)")[:4]
            minimum, returned = decode(["uint256", "uint256"], raw[4:])
            assert (
                minimum == frozen["minout_wei"] and returned == observation["output_wei"] < minimum
            )
            assert ("eth_estimateGas", canonical_json([tx, pin])) not in index
            revert_gap = minimum - returned
        phases[state["stage"]] = {
            "height": height,
            "block_hash": state["h"],
            "base_fee_wei": fee,
            "gross_wei": observation["gross_wei"],
            "paired_estimated_gas": gas,
            "minout_shortfall_wei": revert_gap,
        }
    return {
        "directory": str(directory.relative_to(ROOT)),
        "route": route.name,
        "principal_wei": route.amount,
        "frozen_utc": frozen["frozen"]["utc"],
        "minimum_output_wei": frozen["minout_wei"],
        "envelope_sha256": frozen["envelope_sha256"],
        "router_runtime_keccak": pins[tx["to"]],
        "caller": tx["from"],
        "router": tx["to"],
        "fee_tiers_in_order": [pool.fee for pool in route.pools],
        "phases": phases,
    }, (route.pools, route.path(), tx_common, pins)


def main():
    if not __debug__:
        raise SystemExit("run without optimization")
    lock = load(LOCK)
    assert (
        sha256_hex(LOCK.read_bytes())
        == "4b08205314d5061f03ac985fa984a09bbf815ca3e614830ceacbc1aef43a8e9d"
    )
    assert sha256_hex((NEW / "frozen.json").read_bytes()) == lock["new_frozen_sha256"]
    old = []
    for path, digest in zip(lock["old_dirs"], lock["old_result_sha256"], strict=True):
        directory = ROOT / path
        assert sha256_hex((directory / "result.json").read_bytes()) == digest
        old.append(inspect_window(directory))
    units = [max(r["phases"]["discovery"]["paired_estimated_gas"]) for r, _ in old]
    typical = median_low(units)
    optimistic = min(units)
    assert (optimistic, typical) == (lock["gas_optimistic"], lock["gas_point"])
    new, identity = inspect_window(NEW)
    assert all(
        prior_identity == identity and prior["frozen_utc"] < new["frozen_utc"]
        for prior, prior_identity in old
    )
    for state in new["phases"].values():
        gross, fee, estimate = (
            state["gross_wei"],
            state["base_fee_wei"],
            state["paired_estimated_gas"],
        )
        actual = max(estimate) if estimate else None
        state["strict_max_affordable_gas"] = (gross - 1) // fee
        state["net_21000_gas_wei"] = gross - 21000 * fee
        state["optimistic_predicted_net_wei"] = gross - optimistic * fee
        state["median_predicted_net_wei"] = gross - typical * fee
        state["whole_estimated_net_wei"] = None if actual is None else gross - actual * fee
        state["optimistic_net_error_wei"] = None if actual is None else (actual - optimistic) * fee
        state["median_net_error_wei"] = None if actual is None else (actual - typical) * fee
        state["optimistic_gas_error"] = None if actual is None else optimistic - actual
        state["median_gas_error"] = None if actual is None else typical - actual
    print(
        json.dumps(
            {
                "chain_id": CHAIN_ID,
                "rule_lock_sha256": sha256_hex(LOCK.read_bytes()),
                "old_windows": [r for r, _ in old],
                "historical_same_principal_windows": sum(
                    r["principal_wei"] == new["principal_wei"] for r, _ in old
                ),
                "unit_estimates": {"optimistic": optimistic, "point_median": typical},
                "holdout": new,
                "realized_all_cost_net_wei": None,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
