"""Closed synthetic genesis and one empty-account probe override; no historical-state claim."""

import gzip
import io
from collections.abc import Callable
from typing import Annotated, Any, Literal, Self

from eth_hash.auto import keccak
from pydantic import Field, model_validator

from ..evidence.rpc_validation import (
    rpc_quantity,
    strict_integer,
    strict_json_object,
    strict_object,
)
from ..models.common import ZERO_ADDRESS, canonical_json, sha256_hex
from .cycle_evidence import header
from .cycle_plan import Address, Frozen, Hash, Raw
from .cycle_protocols import data_bytes
from .metric_probe import HexBytes, Plan, decode_result, requests

MAX_STATE_BYTES = 8 * 1024 * 1024
EMPTY_CODE = "0x" + keccak(b"").hex()
EMPTY_STORAGE = "0x" + keccak(b"\x80").hex()


class AccountState(Frozen):
    address: Address
    nonce: Annotated[int, Field(ge=0, lt=1 << 64)]
    balance: Raw
    code: Annotated[HexBytes, Field(max_length=49154)]
    storage: Annotated[dict[Hash, Hash], Field(max_length=256)]

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.address == ZERO_ADDRESS or any(int(v, 16) == 0 for v in self.storage.values()):
            raise ValueError("local_image_zero_address_or_explicit_zero_slot")
        return self

    def state(self) -> dict[str, Any]:
        return {
            "nonce": self.nonce,
            "balance": hex(self.balance),
            "code": self.code,
            "storage": dict(self.storage),
        }


class Image(Frozen):
    schema_version: Literal["oasis-metric-local-image-v1"] = Field(alias="schema")
    origin: Literal["synthetic_fixture"]
    accounts: Annotated[tuple[AccountState, ...], Field(min_length=4, max_length=64)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({a.address for a in self.accounts}) != len(self.accounts):
            raise ValueError("local_image_duplicate_account")
        return self

    def validate_plan(self, plan: Plan) -> None:
        if plan.scenario != "synthetic_local_evm":
            raise ValueError("local_image_cannot_substitute_historical_state")
        accounts = {a.address: a for a in self.accounts}
        if plan.probe in accounts or plan.observer in accounts:
            raise ValueError("local_probe_and_observer_must_be_absent_from_genesis")
        expected = {
            plan.metric: plan.metric_runtime_hash,
            plan.v3: plan.v3_runtime_hash,
            **{a.address: a.runtime_hash for a in plan.assets},
        }
        for address, code_hash in expected.items():
            if (
                address not in accounts
                or "0x" + keccak(data_bytes(accounts[address].code)).hex() != code_hash
            ):
                raise ValueError("local_image_role_runtime_mismatch")

    def genesis(self, plan: Plan) -> dict[str, Any]:
        self.validate_plan(plan)
        alloc = {a.address: {**a.state(), "nonce": hex(a.nonce)} for a in self.accounts}
        return {
            "config": {
                "chainId": plan.chain_id,
                "londonBlock": 0,
                "shanghaiTime": 0,
                "cancunTime": 0,
            },
            "difficulty": "0x0",
            "gasLimit": hex(30_000_000),
            "timestamp": hex(plan.block_timestamp),
            "number": hex(plan.block_number),
            "baseFeePerGas": "0x0",
            "alloc": alloc,
        }


def snapshot(value: object, plan: Plan, image: Image) -> dict[str, object]:
    compressed = data_bytes(value)
    if len(compressed) > MAX_STATE_BYTES:
        raise ValueError("local_snapshot_size")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as handle:
        raw = handle.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("local_snapshot_expansion")
    obj = strict_json_object(raw)
    block = strict_object(obj.get("block"), "local block")
    if (
        rpc_quantity(block.get("number")) != plan.block_number
        or rpc_quantity(block.get("timestamp")) != plan.block_timestamp
        or strict_integer(obj.get("best_block_number"), "best block") != plan.block_number
        or obj.get("transactions") != []
    ):
        raise ValueError("local_snapshot_context")
    actual = strict_object(obj.get("accounts"), "local accounts")
    expected = {a.address: a.state() for a in image.accounts}
    if canonical_json(actual) != canonical_json(expected):
        raise ValueError("local_genesis_state_mismatch_or_implicit_accounts")
    return obj


def empty_account(value: object, address: str) -> None:
    obj = strict_object(value, "empty account observation")
    if (
        obj.get("address") != address
        or rpc_quantity(obj.get("nonce")) != 0
        or rpc_quantity(obj.get("balance")) != 0
        or obj.get("codeHash") != EMPTY_CODE
        or obj.get("storageHash") != EMPTY_STORAGE
        or obj.get("storageProof") != []
    ):
        raise ValueError("local_probe_account_not_empty")


def override_call(plan: Plan, runtime: bytes) -> list[object]:
    if not runtime or "0x" + keccak(runtime).hex() != plan.probe_runtime_hash:
        raise ValueError("local_override_runtime_mismatch")
    # Only the absent probe receives code and empty storage. Never balance/nonce, pool/oracle
    # writes, a block override, a generic caller-supplied method or a persistent setCode operation.
    return [*requests(plan)[3]["params"], {plan.probe: {"code": "0x" + runtime.hex(), "state": {}}}]


def exercise(
    template: Plan,
    image: Image,
    runtime: bytes,
    endpoint_digest: str,
    call: Callable[[str, list[object]], dict[str, Any]],
) -> dict[str, Any]:
    """The same bounded sequence drives an owned child or retained-byte replay."""
    image.validate_plan(template)

    def value(method: str, params: list[object]) -> Any:
        res = call(method, params)
        if "result" not in res:
            raise ValueError("local_required_read_failed")
        return res["result"]

    version = value("web3_clientVersion", [])
    if not isinstance(version, str) or not version.startswith("anvil/"):
        raise ValueError("local_anvil_identity")
    if value("eth_accounts", []) != []:
        raise ValueError("local_unlocked_accounts_present")
    if rpc_quantity(value("eth_chainId", [])) != template.chain_id:
        raise ValueError("local_chain_mismatch")
    opening = value("eth_getBlockByNumber", [hex(template.block_number), False])
    block = header(opening, template.chain_id, template.block_number)
    if block.ref.timestamp != template.block_timestamp:
        raise ValueError("local_virtual_time_mismatch")
    # A synthetic genesis has its own hash. Keep the input template separately; never replace
    # a historical pin or label the derived local header as canonical mainnet evidence.
    obj = template.model_dump(by_alias=True, mode="json")
    obj.update(
        block_hash=block.ref.block_hash, source="owned_anvil_child", endpoint_sha256=endpoint_digest
    )
    plan = Plan.model_validate_json(canonical_json(obj))
    pin = {"blockHash": plan.block_hash, "requireCanonical": True}
    before = value("anvil_dumpState", [])
    snapshot(before, plan, image)
    empty_account(value("eth_getProof", [plan.probe, [], pin]), plan.probe)
    if value("eth_getCode", [plan.probe, pin]) != "0x":
        raise ValueError("local_probe_code_present")
    reply = call("eth_call", override_call(plan, runtime))
    after = value("anvil_dumpState", [])
    if before != after:
        raise ValueError("local_state_changed")
    empty_account(value("eth_getProof", [plan.probe, [], pin]), plan.probe)
    closing = value("eth_getBlockByNumber", [hex(plan.block_number), False])
    if canonical_json(opening) != canonical_json(closing):
        raise ValueError("local_header_changed")
    result, reason = None, None
    try:
        if "result" in reply:
            raise ValueError("probe_unexpected_nonreverting_return")
        error = strict_object(reply.get("error"), "local revert")
        if (
            set(error) != {"code", "message", "data"}
            or type(error["code"]) is not int
            or error["code"] != 3
            or not isinstance(error["message"], str)
        ):
            raise ValueError("probe_error_shape")
        result = decode_result(plan, data_bytes(error["data"]))
    except ValueError as exc:
        reason = str(exc) if str(exc).startswith("probe_") else "probe_malformed_result"
    return {
        "schema": "oasis-metric-local-outcome-v1",
        "evidence_class": "synthetic_local_anvil",
        "status": "local_probe_observed" if result is not None else "local_probe_rejected",
        "reason": reason,
        "local_plan": plan.model_dump(by_alias=True, mode="json"),
        "observed_result": result,
        "anvil_client_version": version,
        "state_before_sha256": sha256_hex(data_bytes(before)),
        "state_after_sha256": sha256_hex(data_bytes(after)),
        "state_unchanged": True,
        "probe_absent_before_and_after": True,
        "historical_state_verified": False,
        "genesis_root_independently_verified": False,
        "full_transaction_gas_verified": False,
        "execution_eligible": False,
        "realized_pnl_raw": None,
    }
