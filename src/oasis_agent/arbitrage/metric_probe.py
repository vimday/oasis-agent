"""Offline Metric/V3 probe packets and strict return decoding; never execution authority."""

from typing import Annotated, Any, Literal, Self

from eth_abi.abi import encode
from eth_hash.auto import keccak
from pydantic import Field, StringConstraints, model_validator

from ..evidence.rpc_validation import rpc_quantity, strict_json_object, strict_object
from ..models.common import ZERO_ADDRESS, canonical_json, sha256_hex
from .cycle_evidence import header
from .cycle_plan import Address, Asset, Frozen, Hash, Label, Size, utc
from .cycle_protocols import abi, calldata, data_bytes, topic
from .metric_builder import Digest

REQUEST_TYPE = "(bool,bool,uint128,uint128,uint128,uint160,bytes)"
PROBE_SIGNATURE = f"probe({REQUEST_TYPE})"
RESULT_SIGNATURE = "ProbeResult(bytes32,uint256,uint256,uint256,uint256,uint256)"
RESULT_TYPES = ("bytes32",) + ("uint256",) * 5
HexBytes = Annotated[str, StringConstraints(pattern=r"^0x(?:[0-9a-f]{2})*$")]


class Request(Frozen):
    metric_first: bool
    start_with_a: bool
    amount_in: Size
    minimum_profit: Annotated[int, Field(ge=0, lt=1 << 128)]
    metric_price_limit_x64: Annotated[int, Field(ge=0, lt=1 << 128)]
    v3_sqrt_price_limit_x96: Annotated[int, Field(gt=0, lt=1 << 160)]
    extension_data: Annotated[HexBytes, Field(max_length=2050)]

    def values(self) -> tuple[object, ...]:
        return (
            self.metric_first,
            self.start_with_a,
            self.amount_in,
            self.minimum_profit,
            self.metric_price_limit_x64,
            self.v3_sqrt_price_limit_x96,
            data_bytes(self.extension_data),
        )


class Plan(Frozen):
    schema_version: Literal["oasis-metric-probe-plan-v1"] = Field(alias="schema")
    scenario: Literal["synthetic_local_evm", "retained_simulation_claim"]
    source: Label
    endpoint_sha256: Digest
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    block_number: Annotated[int, Field(gt=0, lt=1 << 64)]
    block_hash: Hash
    block_timestamp: Annotated[int, Field(gt=0, lt=1 << 64)]
    cutoff_utc: str
    probe: Address
    observer: Address
    metric: Address
    v3: Address
    assets: tuple[Asset, Asset]
    metric_token0_is_a: bool
    metric_runtime_hash: Hash
    v3_runtime_hash: Hash
    build_artifact_sha256: Digest
    probe_runtime_hash: Hash
    gas_limit: Annotated[int, Field(ge=100_000, le=30_000_000)]
    request: Request

    @model_validator(mode="after")
    def scope(self) -> Self:
        a, b = self.assets
        if any(x.address is None or x.runtime_hash is None or x.decimals > 36 for x in (a, b)):
            raise ValueError("two_pinned_erc20_assets_required")
        assert a.address is not None and b.address is not None
        addresses = (self.probe, self.observer, self.metric, self.v3, a.address, b.address)
        if ZERO_ADDRESS in addresses or len(set(addresses)) != 6 or a.address >= b.address:
            raise ValueError("distinct_addresses_and_canonical_asset_order_required")
        if utc(self.cutoff_utc).timestamp() < self.block_timestamp:
            raise ValueError("cutoff_precedes_block")
        return self


def immutable_values(plan: Plan) -> dict[str, tuple[str, object]]:
    a, b = plan.assets
    return {
        "observer": ("address", plan.observer),
        "metric": ("address", plan.metric),
        "v3": ("address", plan.v3),
        "assetA": ("address", a.address),
        "assetB": ("address", b.address),
        "metricToken0IsA": ("bool", plan.metric_token0_is_a),
        "metricCode": ("bytes32", data_bytes(plan.metric_runtime_hash)),
        "v3Code": ("bytes32", data_bytes(plan.v3_runtime_hash)),
        "assetACode": ("bytes32", data_bytes(a.runtime_hash)),
        "assetBCode": ("bytes32", data_bytes(b.runtime_hash)),
        "expectedChain": ("uint256", plan.chain_id),
        "expectedBlock": ("uint256", plan.block_number),
        "expectedTimestamp": ("uint256", plan.block_timestamp),
    }


def request_hash(plan: Plan) -> str:
    raw = encode(
        ("address", "address", "uint256", "uint256", "uint256", REQUEST_TYPE),
        (
            plan.probe,
            plan.observer,
            plan.chain_id,
            plan.block_number,
            plan.block_timestamp,
            plan.request.values(),
        ),
    )
    return "0x" + keccak(raw).hex()


def requests(plan: Plan) -> list[dict[str, Any]]:
    pin = {"blockHash": plan.block_hash, "requireCanonical": True}
    specs = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(plan.block_number), False]),
        ("eth_getCode", [plan.probe, pin]),
        (
            "eth_call",
            [
                {
                    "from": plan.observer,
                    "to": plan.probe,
                    "value": "0x0",
                    "gas": hex(plan.gas_limit),
                    "data": calldata(PROBE_SIGNATURE, (REQUEST_TYPE,), (plan.request.values(),)),
                },
                pin,
            ],
        ),
    ]
    return [
        {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
        for i, (method, params) in enumerate(specs, 1)
    ]


class Exchange(Frozen):
    request_hex: Annotated[HexBytes, Field(max_length=16386)]
    response_hex: Annotated[HexBytes, Field(max_length=262146)]
    received_utc: str


class Capture(Frozen):
    schema_version: Literal["oasis-metric-probe-retained-v1"] = Field(alias="schema")
    plan_file_sha256: Digest
    source: Label
    endpoint_sha256: Digest
    exchanges: tuple[Exchange, Exchange, Exchange, Exchange]


def decode_result(plan: Plan, raw: bytes) -> dict[str, Any]:
    if len(raw) != 196 or raw[:4] != data_bytes(topic(RESULT_SIGNATURE)[:10]):
        raise ValueError("probe_result_shape_or_selector")
    digest, principal, middle, output, profit, gas = abi(RESULT_TYPES, raw[4:])
    if "0x" + digest.hex() != request_hash(plan) or principal != plan.request.amount_in:
        raise ValueError("probe_request_binding_mismatch")
    if (
        not 0 < middle < 1 << 127
        or not principal <= output < 1 << 127
        or profit != output - principal
        or profit < plan.request.minimum_profit
        or not 0 < gas <= plan.gas_limit
    ):
        raise ValueError("probe_result_accounting_or_bounds")
    start = 0 if plan.request.start_with_a else 1
    return {
        "request_hash": request_hash(plan),
        "start_asset": plan.assets[start].model_dump(),
        "intermediate_asset": plan.assets[1 - start].model_dump(),
        "principal_raw": principal,
        "intermediate_raw": middle,
        "output_raw": output,
        "gross_profit_raw": profit,
        "inner_gas_used": gas,
        "full_transaction_gas": None,
        "net_profit_raw": None,
    }


def evaluate(plan: Plan, capture: Capture, runtime: bytes) -> dict[str, Any]:
    if not 0 < len(runtime) <= 24576 or "0x" + keccak(runtime).hex() != plan.probe_runtime_hash:
        raise ValueError("probe_materialized_runtime_hash_mismatch")
    if capture.source != plan.source or capture.endpoint_sha256 != plan.endpoint_sha256:
        raise ValueError("capture_source_mismatch")
    expected = requests(plan)
    replies, evidence = [], []
    last_time = None
    for spec, exchange in zip(expected, capture.exchanges, strict=True):
        request_raw, response_raw = (
            data_bytes(exchange.request_hex),
            data_bytes(exchange.response_hex),
        )
        req, res = strict_json_object(request_raw), strict_json_object(response_raw)
        if canonical_json(req) != canonical_json(spec):
            raise ValueError("request_outside_frozen_probe_packet")
        if (
            res.get("jsonrpc") != "2.0"
            or type(res.get("id")) is not int
            or res["id"] != spec["id"]
            or set(res) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"})
        ):
            raise ValueError("probe_response_envelope")
        received = utc(exchange.received_utc)
        if (
            received.timestamp() < plan.block_timestamp
            or received > utc(plan.cutoff_utc)
            or (last_time is not None and received < last_time)
        ):
            raise ValueError("probe_capture_time_outside_context")
        last_time = received
        replies.append(res)
        evidence.append(
            {
                "method": spec["method"],
                "received_utc": exchange.received_utc,
                "raw_request_sha256": sha256_hex(request_raw),
                "raw_response_sha256": sha256_hex(response_raw),
            }
        )
    result, reason = None, None
    try:
        if any("error" in r for r in replies[:3]):
            raise ValueError("probe_identity_read_failed")
        if rpc_quantity(replies[0]["result"]) != plan.chain_id:
            raise ValueError("probe_chain_mismatch")
        block = header(replies[1]["result"], plan.chain_id, plan.block_number)
        if block.ref.block_hash != plan.block_hash or block.ref.timestamp != plan.block_timestamp:
            raise ValueError("probe_block_context_mismatch")
        if data_bytes(replies[2]["result"]) != runtime:
            raise ValueError("probe_runtime_or_immutable_mismatch")
        response = replies[3]
        if "result" in response:
            raise ValueError("probe_unexpected_nonreverting_return")
        error = strict_object(response["error"], "probe error")
        if set(error) != {"code", "message", "data"} or type(error["code"]) is not int:
            raise ValueError("probe_error_shape")
        # Only the explicitly supported execution-reverted envelope, never nested/message scraping.
        if error["code"] != 3 or not isinstance(error["message"], str):
            raise ValueError("probe_error_code")
        result = decode_result(plan, data_bytes(error["data"]))
    except ValueError as exc:
        # All reasons above are our own fixed labels; never propagate a hostile RPC message.
        reason = str(exc) if str(exc).startswith("probe_") else "probe_malformed_context_or_result"
    return {
        "schema": "oasis-metric-probe-report-v1",
        "evidence_class": plan.scenario,
        "status": "bound_revert_payload" if result is not None else "rejected_or_incomplete",
        "reason": reason,
        "chain_id": plan.chain_id,
        "block_number": plan.block_number,
        "block_hash": plan.block_hash,
        "block_timestamp": plan.block_timestamp,
        "source": capture.source,
        "endpoint_sha256": capture.endpoint_sha256,
        "request_hash": request_hash(plan),
        "observed_result": result,
        "exchanges": evidence,
        "build_artifact_sha256": plan.build_artifact_sha256,
        "probe_runtime_hash": plan.probe_runtime_hash,
        "retained_bytes_decoded": True,
        "original_transport_verified": False,
        "build_source_equivalence_independently_verified": False,
        "historical_state_and_overlay_verified": False,
        "target_pool_configuration_verified": False,
        "token_metadata_verified": False,
        "simulation_gas_budget_verified": False,
        "settlement_verified": False,
        "independent_providers_verified": False,
        "execution_eligible": False,
        "realized_pnl_raw": None,
    }
