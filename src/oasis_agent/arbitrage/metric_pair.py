"""Prepare a read-only Metric/V3 comparison using the same Ethereum parent state."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

from eth_hash.auto import keccak
from pydantic import Field, model_validator

from ..evidence.rpc_validation import rpc_quantity, strict_list, strict_object
from ..evidence.state_trie import prague_header
from ..models.chain import BlockSnapshot
from ..models.common import canonical_json, sha256_hex
from ..uniswap_v4.liquidity_math import MAX_SQRT_PRICE, MIN_SQRT_PRICE
from . import metric_builder, metric_probe, metric_probe_build, titan_state
from .cycle_plan import Address, Asset, Frozen, Hash, Size
from .cycle_protocols import abi, data_bytes, topic
from .metric_v3 import IMMUTABLE_TYPES

PROBE = "0x7ea6bd0d34716015ff9674546daf291077febc22"
OBSERVER = "0x9474348ddc6a4689fe885064cc20a0c976cf3084"
REVIEWED_ARTIFACT = "e9717bbaf9ef385ba6e09b7f58d86ee94041c7b85f205c441a6ba090a1616def"


class Design(Frozen):
    schema_version: Literal["oasis-metric-parent-pair-v1"] = Field(alias="schema")
    parent_hash: Hash
    target_hash: Hash
    target_number: Annotated[int, Field(gt=1, lt=1 << 64)]
    target_timestamp: Annotated[int, Field(gt=0, lt=1 << 64)]
    source: Literal["publicnode_ethereum", "alchemy_public_ethereum"]
    assets: tuple[Asset, Asset]
    metric: Address
    metric_hash: Hash
    immutables_hash: Hash
    v3: Address
    v3_hash: Hash
    provider: Address
    provider_hash: Hash
    v3_factory: Address
    v3_fee: Annotated[int, Field(gt=0, le=1_000_000)]
    start_amounts: tuple[Size, Size]
    build_artifact_sha256: metric_builder.Digest
    evidence_sha256: dict[str, metric_builder.Digest]

    @model_validator(mode="after")
    def scope(self) -> Self:
        if self.build_artifact_sha256 != REVIEWED_ARTIFACT:
            raise ValueError("pair_unreviewed_build_artifact")
        a, b = self.assets
        if (
            a.address is None
            or b.address is None
            or a.address >= b.address
            or max(a.decimals, b.decimals) > 18
        ):
            raise ValueError("pair_asset_order")
        if set(self.evidence_sha256) != {
            "parent",
            "target",
            "peer_parent",
            "peer_target",
            "proof",
            "titan",
            "analysis",
            "feeds",
        }:
            raise ValueError("pair_evidence_bindings_missing")
        return self


def headers(design: Design, parent: object, target: object) -> tuple[BlockSnapshot, dict[str, str]]:
    parent_block, _ = prague_header(parent, design.target_number - 1, design.parent_hash)
    target_block, _ = prague_header(target, design.target_number, design.target_hash)
    if (
        target_block.parent_hash != parent_block.ref.block_hash
        or target_block.ref.timestamp != design.target_timestamp
        or parent_block.ref.timestamp is None
        or parent_block.ref.timestamp >= design.target_timestamp
    ):
        raise ValueError("pair_header_lineage_mismatch")
    raw = strict_object(target, "target header")
    block_fields = {
        "number": "number",
        "time": "timestamp",
        "gasLimit": "gasLimit",
        "feeRecipient": "miner",
        "prevRandao": "mixHash",
        "baseFeePerGas": "baseFeePerGas",
    }
    overrides = {key: str(raw[field]) for key, field in block_fields.items()}
    overrides["prevRandao"] = hex(int(overrides["prevRandao"], 16))
    return parent_block, overrides


def selected_diff(
    design: Design,
    feed_plan: metric_builder.BuilderFeedPlan,
    symbols: dict[str, dict[str, Any]],
    analysis: titan_state.TitanAnalysisPlan,
    snapshot: object,
    received_ns: int,
) -> dict[str, str]:
    if feed_plan.pool != design.metric or {f.token for f in feed_plan.feeds} != {
        a.address for a in design.assets
    }:
        raise ValueError("pair_feed_identity")
    decoded = titan_state.inspect_snapshot(analysis, snapshot, received_ns)
    selected = [r for r in decoded["selected"] if r["quote_address"] == feed_plan.oracle]
    if (
        len(selected) != 1
        or decoded["missing"]
        or (
            selected[0]["reasons"]
            or selected[0]["target_block"] != design.target_number
            or selected[0]["target_timestamp_seconds"] != design.target_timestamp
            or selected[0]["snapshot_timestamp_ns"] > received_ns
            or received_ns >= design.target_timestamp * 1_000_000_000
        )
    ):
        raise ValueError("pair_titan_target_or_time")
    feeds = metric_builder.interpret(feed_plan, snapshot, received_ns, symbols)
    if feeds["status"] != "decoded_builder_claims":
        raise ValueError("pair_builder_feed_checks_failed")
    raw = strict_object(snapshot, "Titan snapshot")
    venue = next(
        row
        for item in strict_list(raw["venues"], "venues")
        if titan_state.address((row := strict_object(item, "venue"))["quoteAddress"])
        == feed_plan.oracle
    )
    accounts = strict_object(venue["stateOverride"], "Titan accounts")
    entry = next(
        fields for address, fields in accounts.items() if address.lower() == feed_plan.oracle
    )
    words = titan_state.storage(strict_object(entry, "oracle state")["stateDiff"])
    return {
        key: words[key]
        for key in sorted(metric_builder.feed_slot(f.feed_id) for f in feed_plan.feeds)
    }


def check_identity(
    design: Design, feed_plan: metric_builder.BuilderFeedPlan, records: list[dict[str, Any]]
) -> dict[str, Any]:
    reads = {}
    pin = {"blockHash": design.parent_hash, "requireCanonical": True}
    for row in records[2:-1]:
        req = row["request"]
        if req["params"][-1] != pin:
            raise ValueError("pair_identity_pin")
        reads[canonical_json((req["method"], req["params"][0]))] = row["response"]["result"]

    def read(method: str, params: object) -> object:
        return reads[canonical_json((method, params))]

    def getter(address: str, signature: str, *kinds: str) -> tuple[Any, ...]:
        return abi(kinds, raw_call(address, signature))

    def raw_call(address: str, signature: str) -> bytes:
        return data_bytes(read("eth_call", {"to": address, "data": topic(signature)[:10]}))

    for address, digest in (
        (design.metric, design.metric_hash),
        (design.v3, design.v3_hash),
        (design.provider, design.provider_hash),
        (feed_plan.oracle, metric_builder.LAYOUT_RUNTIME),
        *((asset.address, asset.runtime_hash) for asset in design.assets),
    ):
        if "0x" + keccak(data_bytes(read("eth_getCode", address))).hex() != digest:
            raise ValueError("pair_runtime_hash_mismatch")
    if read("eth_getCode", PROBE) != "0x" or rpc_quantity(read("eth_getBalance", PROBE)):
        raise ValueError("pair_probe_account_occupied")
    raw = raw_call(design.metric, "getImmutables()")
    imm = abi(IMMUTABLE_TYPES, raw)
    by_asset = {asset.address: asset.decimals for asset in design.assets}
    if (
        "0x" + keccak(raw).hex() != design.immutables_hash
        or set(imm[1:3]) != by_asset.keys()
        or imm[8] != design.provider
        or imm[3:5] != tuple(10 ** (18 - by_asset[token]) for token in imm[1:3])
    ):
        raise ValueError("pair_metric_identity_mismatch")
    assert all(a.address is not None for a in design.assets)
    for asset in design.assets:
        assert asset.address is not None
        if getter(asset.address, "decimals()", "uint8") != (asset.decimals,):
            raise ValueError("pair_token_decimals")
    for signature, kind, expect in (
        ("factory()", "address", design.v3_factory),
        ("fee()", "uint24", design.v3_fee),
        ("token0()", "address", design.assets[0].address),
        ("token1()", "address", design.assets[1].address),
    ):
        if getter(design.v3, signature, kind) != (expect,):
            raise ValueError("pair_v3_configuration")
    if getter(design.provider, "offchainOracle()", "address") != (feed_plan.oracle,):
        raise ValueError("pair_provider_oracle_mismatch")
    by_token = {f.token: f for f in feed_plan.feeds}
    for role, token in zip(("base", "quote"), imm[1:3], strict=True):
        binding = by_token[token]
        if getter(design.provider, f"{role}FeedId()", "bytes32") != (
            binding.feed_id.to_bytes(32, "big"),
        ) or getter(design.provider, f"{role}MaxStaleness()", "uint256") != (
            binding.max_age_seconds,
        ):
            raise ValueError("pair_provider_feed_configuration")
    return {
        "metric_token0_is_a": imm[1] == design.assets[0].address,
        "extensions": list(imm[11:18]),
    }


@dataclass(frozen=True)
class PairedCalls:
    plans: tuple[metric_probe.Plan, ...]
    runtime: bytes
    oracle: str
    diff: dict[str, str]
    block_fields: dict[str, str]
    parent_hash: str
    source: str
    chain_id: int = 1

    def requests(self) -> tuple[tuple[str, list[object]], ...]:
        head: tuple[str, list[object]] = (
            "eth_getBlockByNumber",
            [hex(self.plans[0].block_number - 1), False],
        )
        requests: list[tuple[str, list[object]]] = [("eth_chainId", []), head]
        pin = {"blockHash": self.parent_hash, "requireCanonical": True}
        for plan in self.plans:
            call = metric_probe.requests(plan)[-1]["params"][0]
            baseline = {PROBE: {"code": "0x" + self.runtime.hex()}}
            override = {**baseline, self.oracle: {"stateDiff": self.diff}}
            requests.extend(
                ("eth_call", [call, pin, state, self.block_fields])
                for state in (baseline, override)
            )
        return (*requests, head)

    @property
    def call_end(self) -> int:
        return 2 + 2 * len(self.plans)

    def outcome(self, index: int, response: dict[str, object]) -> dict[str, Any]:
        if not 2 <= index < self.call_end or set(response) != {"jsonrpc", "id", "error"}:
            raise ValueError("pair_expected_probe_revert")
        if (
            response["jsonrpc"] != "2.0"
            or type(response["id"]) is not int
            or response["id"] != index + 1
        ):
            raise ValueError("pair_probe_response_id")
        error = strict_object(response["error"], "simulation revert")
        if (
            set(error) != {"code", "message", "data"}
            or type(error["code"]) is not int
            or error["code"] != 3
            or not isinstance(error["message"], str)
        ):
            raise ValueError("pair_unrecognized_revert_envelope")
        raw = data_bytes(error["data"])
        plan = self.plans[(index - 2) // 2]
        if raw[:4] == bytes.fromhex(topic(metric_probe.RESULT_SIGNATURE)[2:10]):
            return {"status": "both_legs_repaid", "result": metric_probe.decode_result(plan, raw)}
        for signature, codes, label in (
            ("Rejected(uint8)", range(1, 18), "probe_rejected"),
            ("ExternalFailure(uint8)", range(4), "external_failure"),
        ):
            if raw[:4] == bytes.fromhex(topic(signature)[2:10]):
                (code,) = abi(("uint8",), raw[4:])
                if code in codes:
                    return {"status": label, "code": code}
        raise ValueError("pair_unrecognized_probe_revert")

    def check_response(self, index: int, response: dict[str, object], prior: object) -> object:
        if index == 0:
            if rpc_quantity(response.get("result")) != self.chain_id:
                raise ValueError("pair_chain_mismatch")
        elif index in (1, self.call_end):
            block, _ = prague_header(
                response.get("result"), self.plans[0].block_number - 1, self.parent_hash
            )
            if index == 1:
                return block
            if block != prior:
                raise ValueError("pair_closing_parent_changed")
        else:
            if not isinstance(prior, BlockSnapshot):
                raise ValueError("pair_parent_missing")
            self.outcome(index, response)
        return prior


def build_calls(
    design: Design,
    identity: dict[str, Any],
    feeds: metric_builder.BuilderFeedPlan,
    diff: dict[str, str],
    block_fields: dict[str, str],
    artifact: bytes,
    cutoff: str,
    endpoint: str,
) -> PairedCalls:
    plans = []
    for start_with_a, amount in zip((True, False), design.start_amounts, strict=True):
        for metric_first in (True, False):
            metric_in_a = start_with_a == metric_first
            metric_sell = metric_in_a == identity["metric_token0_is_a"]
            plan = metric_probe.Plan(
                schema="oasis-metric-probe-plan-v1",
                scenario="retained_simulation_claim",
                source=design.source,
                endpoint_sha256=sha256_hex(endpoint.encode()),
                chain_id=1,
                block_number=design.target_number,
                block_hash=design.target_hash,
                block_timestamp=design.target_timestamp,
                cutoff_utc=cutoff,
                probe=PROBE,
                observer=OBSERVER,
                metric=design.metric,
                v3=design.v3,
                assets=design.assets,
                metric_token0_is_a=identity["metric_token0_is_a"],
                metric_runtime_hash=design.metric_hash,
                v3_runtime_hash=design.v3_hash,
                build_artifact_sha256=design.build_artifact_sha256,
                probe_runtime_hash="0x" + "00" * 32,
                gas_limit=3_000_000,
                request=metric_probe.Request(
                    metric_first=metric_first,
                    start_with_a=start_with_a,
                    amount_in=amount,
                    minimum_profit=0,
                    metric_price_limit_x64=0 if metric_sell else (1 << 128) - 1,
                    v3_sqrt_price_limit_x96=MAX_SQRT_PRICE - 1
                    if metric_in_a
                    else MIN_SQRT_PRICE + 1,
                    extension_data="0x",
                ),
            )
            plans.append(plan)
    runtime = metric_probe_build.build_runtime(plans[0], artifact)
    digest = "0x" + keccak(runtime).hex()
    return PairedCalls(
        tuple(p.model_copy(update={"probe_runtime_hash": digest}) for p in plans),
        runtime,
        feeds.oracle,
        diff,
        block_fields,
        design.parent_hash,
        design.source,
    )
