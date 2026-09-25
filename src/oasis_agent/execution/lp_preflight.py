"""Two-source execution preflight at one pinned head; never a signature.

The signer policy refuses any request unless two independent providers agree, at the
request's own current block, on the signing account's nonce, the executor's own last
nonce and the code hash of every role-bound runtime. This module produces exactly that
evidence. It reads one head, pins it by hash, issues every numeric read against that
pin, re-reads the same header at the close, and retains both providers' exact wire bytes
so a verifier can rebuild the identical payload offline.

It holds no key, has no signing or broadcast interface, and cannot widen the policy it is
asked to observe: a provider that disagrees on any observed field, a head that moved
between the opening and closing read, a runtime whose live code hash differs from the pin,
or a nonce the policy did not expect all refuse instead of producing a default.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    require_code_hash,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import (
    decode_exchange,
    restore_exchanges,
    rpc_quantity,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, normalize_address, normalize_hex
from ..rpc.client import RpcClient
from ..rpc.transport import RpcExchange
from .lp_signer_policy import LpSignerPolicy, preflight_payload
from .lp_state_machine import RuntimePin, RuntimeRole

PREFLIGHT_KIND = "execution-preflight"
LAST_NONCE_SELECTOR = "0x" + keccak(b"lastNonce()")[:4].hex()


@dataclass(frozen=True, slots=True)
class LpPreflightCapture:
    """One provider's answer for one pinned head, with the bytes that prove it."""

    source: str
    header: BlockSnapshot
    account: str
    account_nonce: int
    executor: str
    executor_last_nonce: int
    runtime_pins: tuple[RuntimePin, ...]
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.header, BlockSnapshot):
            raise TypeError("typed preflight header required")
        if not self.source.strip():
            raise ValueError("preflight source required")
        for name in ("account", "executor"):
            address = normalize_address(getattr(self, name))
            if int(address, 16) == 0:
                raise ValueError(f"preflight {name} must be nonzero")
            object.__setattr__(self, name, address)
        for name in ("account_nonce", "executor_last_nonce"):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value >= 1 << 64:
                raise ValueError(f"preflight {name} must be a nonnegative uint64")
        object.__setattr__(self, "runtime_pins", tuple(self.runtime_pins))
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        if not self.runtime_pins or any(
            not isinstance(pin, RuntimePin) for pin in self.runtime_pins
        ):
            raise ValueError("preflight requires typed runtime pins")
        roles = {pin.role for pin in self.runtime_pins}
        if (
            roles != set(RuntimeRole)
            or len(roles) != len(self.runtime_pins)
            or len({pin.address for pin in self.runtime_pins}) != len(self.runtime_pins)
        ):
            raise ValueError("preflight requires one unique runtime pin per role")
        executor = next(pin for pin in self.runtime_pins if pin.role is RuntimeRole.EXECUTOR)
        if executor.address != self.executor:
            raise ValueError("preflight executor pin must match the executor address")
        _validate_capture(self)

    @property
    def block(self) -> BlockRef:
        return self.header.ref

    @property
    def payload(self) -> bytes:
        return preflight_payload(
            block=self.header.ref,
            account=self.account,
            account_nonce=self.account_nonce,
            executor=self.executor,
            executor_last_nonce=self.executor_last_nonce,
            runtime_pins=self.runtime_pins,
        )


@dataclass(frozen=True, slots=True)
class LpPreflightQuorum:
    left: LpPreflightCapture
    right: LpPreflightCapture

    def __post_init__(self) -> None:
        if self.left.source == self.right.source:
            raise ValueError("preflight source aliases must be distinct")
        if self.left.header != self.right.header or self.left.payload != self.right.payload:
            raise ValueError("preflight providers disagree")


def _uint64_word(value: object, name: str) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{name} returned non-hex data")
    normalized = normalize_hex(value)
    if len(normalized) != 66 or normalized[2:50] != "0" * 48:
        raise ValueError(f"{name} returned a noncanonical uint64 word")
    return int(normalized[50:], 16)


def _expected(capture: LpPreflightCapture) -> list[tuple[str, list[object]]]:
    """The exact wire sequence one preflight must issue, and nothing else."""
    block = capture.header.ref
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    expected: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]
    expected.extend(("eth_getCode", [item.address, pin]) for item in capture.runtime_pins)
    expected.extend(
        [
            ("eth_call", [{"to": capture.executor, "data": LAST_NONCE_SELECTOR}, pin]),
            ("eth_getTransactionCount", [capture.account, pin]),
            ("eth_getBlockByNumber", [hex(block.number), False]),
        ]
    )
    return expected


def _validate_capture(capture: LpPreflightCapture) -> None:
    """Re-derive every observed value from the retained bytes, not from a parse."""
    results = successful_results(capture.exchanges, _expected(capture))
    chain_id = capture.header.ref.chain_id
    if rpc_quantity(results[0]) != chain_id:
        raise ValueError("preflight chain mismatch")
    if decode_header(results[1], chain_id=chain_id) != capture.header:
        raise ValueError("preflight opening header mismatch")
    pin_count = len(capture.runtime_pins)
    for pin, result in zip(capture.runtime_pins, results[2 : 2 + pin_count], strict=True):
        require_code_hash(result, pin.code_hash)
    if _uint64_word(results[2 + pin_count], "executor lastNonce") != capture.executor_last_nonce:
        raise ValueError("preflight executor nonce mismatch")
    if rpc_quantity(results[3 + pin_count]) != capture.account_nonce:
        raise ValueError("preflight account nonce mismatch")
    if decode_header(results[-1], chain_id=chain_id) != capture.header:
        raise ValueError("preflight closing header mismatch")


def _collect_one(
    session: RecordedSession, *, policy: LpSignerPolicy, block_number: int
) -> LpPreflightCapture:
    transport, recorder, source = session
    client = RpcClient(transport, expected_chain_id=policy.chain_id)
    header = client.get_block_snapshot(block_number)
    pin = {"blockHash": header.ref.block_hash, "requireCanonical": True}
    for runtime in policy.runtime_pins:
        require_code_hash(
            transport.request("eth_getCode", [runtime.address, pin]), runtime.code_hash
        )
    last_nonce = transport.request(
        "eth_call", [{"to": policy.executor, "data": LAST_NONCE_SELECTOR}, pin]
    )
    account_nonce = transport.request("eth_getTransactionCount", [policy.account, pin])
    if client.get_block_snapshot(block_number) != header:
        raise ValueError("preflight block changed between the opening and closing read")
    return LpPreflightCapture(
        source,
        header,
        policy.account,
        rpc_quantity(account_nonce),
        policy.executor,
        _uint64_word(last_nonce, "executor lastNonce"),
        policy.runtime_pins,
        recorder.exchanges,
    )


def collect_lp_preflight_quorum(
    primary: RecordedSession,
    secondary: RecordedSession,
    *,
    policy: LpSignerPolicy,
    block_number: int,
) -> LpPreflightQuorum:
    """Observe the policy's own identity at one head from two independent providers.

    Only the policy's chain, account, executor and runtime pins are read; its capital
    limits belong to the signer gate and are not re-decided here.
    """
    if not isinstance(policy, LpSignerPolicy):
        raise TypeError("typed signer policy required")
    validate_session_pair(primary, secondary)
    if type(block_number) is not int or block_number < 0:
        raise ValueError("explicit nonnegative preflight block required")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lp-preflight") as pool:
        left = pool.submit(_collect_one, primary, policy=policy, block_number=block_number)
        right = pool.submit(_collect_one, secondary, policy=policy, block_number=block_number)
        return LpPreflightQuorum(left.result(), right.result())


def _persist_one(
    store: EvidenceStore, capture: LpPreflightCapture, *, retrieved_at: str
) -> EvidenceRef:
    """Retain both providers as the identical payload the gate re-derives."""
    return store.put(
        PREFLIGHT_KIND,
        capture.payload,
        capture.header.ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation=capture.payload,
        retrieved_at=retrieved_at,
        schema_version=2,
    )


def persist_lp_preflight_evidence(
    store: EvidenceStore,
    quorum: LpPreflightQuorum,
    *,
    retrieved_at: str | None = None,
) -> tuple[EvidenceRef, EvidenceRef]:
    if not isinstance(quorum, LpPreflightQuorum):
        raise TypeError("typed preflight quorum required")
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    return (
        _persist_one(store, quorum.left, retrieved_at=timestamp),
        _persist_one(store, quorum.right, retrieved_at=timestamp),
    )


def _pin(value: object) -> RuntimePin:
    data = strict_object(value, "runtime pin")
    if set(data) != {"role", "address", "code_hash"}:
        raise ValueError("runtime pin has invalid fields")
    try:
        role = RuntimeRole(strict_string(data["role"], "runtime role"))
    except ValueError as exc:
        raise ValueError("runtime pin has an unknown role") from exc
    return RuntimePin(
        role,
        strict_string(data["address"], "runtime pin address"),
        strict_string(data["code_hash"], "runtime pin code hash"),
    )


def _parse_payload(
    payload: bytes,
) -> tuple[BlockRef, str, int, str, int, tuple[RuntimePin, ...]]:
    data = strict_json_object(payload)
    expected = {
        "schema",
        "block",
        "account",
        "account_nonce",
        "executor",
        "executor_last_nonce",
        "runtime_pins",
    }
    if set(data) != expected:
        raise ValueError("preflight payload has invalid fields")
    if data["schema"] != "oasis.lp-execution-preflight.v2":
        raise ValueError("unsupported preflight schema")
    block_data = strict_object(data["block"], "block")
    if set(block_data) != {"chain_id", "number", "block_hash", "timestamp"}:
        raise ValueError("preflight block has invalid fields")
    return (
        BlockRef(
            strict_integer(block_data["chain_id"], "chain ID"),
            strict_integer(block_data["number"], "block number"),
            strict_string(block_data["block_hash"], "block hash"),
            strict_integer(block_data["timestamp"], "block timestamp"),
        ),
        strict_string(data["account"], "account"),
        strict_integer(data["account_nonce"], "account nonce"),
        strict_string(data["executor"], "executor"),
        strict_integer(data["executor_last_nonce"], "executor last nonce"),
        tuple(_pin(item) for item in strict_list(data["runtime_pins"], "runtime pins")),
    )


def replay_lp_preflight_evidence(
    store: EvidenceStore, reference: EvidenceRef
) -> LpPreflightCapture:
    """Rebuild one capture from its retained bytes; a changed byte refuses."""
    if reference.kind != PREFLIGHT_KIND:
        raise ValueError("reference is not an execution preflight")
    record = store.get_record(reference)
    if record.request_payload is None:
        raise ValueError("preflight lacks exact request evidence")
    block, account, account_nonce, executor, executor_last_nonce, pins = _parse_payload(
        record.payload
    )
    exchanges = restore_exchanges(record.request_payload, record.raw_response_payload)
    if len(exchanges) < 2:
        raise ValueError("preflight evidence is incomplete")
    params, response = decode_exchange(exchanges[1])
    if params != [hex(block.number), False] or "error" in response:
        raise ValueError("preflight opening header is not the pinned request")
    # The payload binds the block reference but not its parent; the raw header is the
    # only place the parent hash can come from, so it is decoded, not trusted.
    header = decode_header(response["result"], chain_id=block.chain_id)
    if header.ref != block:
        raise ValueError("preflight header differs from its own payload")
    capture = LpPreflightCapture(
        reference.source,
        header,
        account,
        account_nonce,
        executor,
        executor_last_nonce,
        pins,
        exchanges,
    )
    if capture.payload != record.payload:
        raise ValueError("preflight differs from raw reconstruction")
    return capture


__all__ = [
    "LAST_NONCE_SELECTOR",
    "PREFLIGHT_KIND",
    "LpPreflightCapture",
    "LpPreflightQuorum",
    "collect_lp_preflight_quorum",
    "persist_lp_preflight_evidence",
    "replay_lp_preflight_evidence",
]
