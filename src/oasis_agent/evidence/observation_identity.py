"""Exact v4 observation deployment graph; not a release dossier."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from eth_hash.auto import keccak

from ..models.chain import BlockSnapshot
from ..models.common import (
    ZERO_ADDRESS,
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from ..models.v4 import PoolKey
from ..rpc.client import RpcClient
from ..rpc.transport import RpcExchange
from ..uniswap_v4.pool_id import pool_id_from_key
from .exact_capture import (
    RecordedSession,
    decode_address_word,
    decode_header,
    require_code_hash,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from .rpc_validation import restore_exchanges, strict_json_object
from .store import EvidenceStore

POOL_MANAGER_SELECTOR = "0x" + keccak(b"poolManager()")[:4].hex()


@dataclass(frozen=True, slots=True)
class RuntimeIdentityPin:
    address: str
    runtime_keccak: str

    def __post_init__(self) -> None:
        address = normalize_address(self.address)
        if address == ZERO_ADDRESS:
            raise ValueError("runtime pin address must be nonzero")
        object.__setattr__(self, "address", address)
        object.__setattr__(self, "runtime_keccak", normalize_hash32(self.runtime_keccak))


@dataclass(frozen=True, slots=True)
class V4ObservationPolicy:
    chain_id: int
    pool_key: PoolKey
    pool_manager: RuntimeIdentityPin
    position_manager: RuntimeIdentityPin
    state_view: RuntimeIdentityPin
    hook: RuntimeIdentityPin | None

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("positive observation chain required")
        pins = (self.pool_manager, self.position_manager, self.state_view)
        if len({item.address for item in pins}) != len(pins):
            raise ValueError("observation roles require unique addresses")
        if self.pool_key.hooks == ZERO_ADDRESS:
            if self.hook is not None:
                raise ValueError("hookless pool cannot carry a hook runtime pin")
        elif self.hook is None or self.hook.address != self.pool_key.hooks:
            raise ValueError("hook runtime pin must match PoolKey")


@dataclass(frozen=True, slots=True)
class V4ObservationIdentity:
    policy: V4ObservationPolicy
    pool_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id))
        if self.pool_id != pool_id_from_key(self.policy.pool_key):
            raise ValueError("observation PoolId does not match PoolKey")

    @property
    def hook(self) -> RuntimeIdentityPin | None:
        return self.policy.hook


@dataclass(frozen=True, slots=True)
class V4ObservationCapture:
    source: str
    header: BlockSnapshot
    identity: V4ObservationIdentity
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("observation source required")
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        _validate_capture(self)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.v4-observation-identity.v1",
                "source": self.source,
                "header": asdict(self.header),
                "identity": asdict(self.identity),
            }
        )


@dataclass(frozen=True, slots=True)
class V4ObservationIdentityQuorum:
    left: V4ObservationCapture
    right: V4ObservationCapture

    def __post_init__(self) -> None:
        if self.left.source == self.right.source:
            raise ValueError("observation source aliases must be distinct")
        if self.left.identity != self.right.identity or self.left.header != self.right.header:
            raise ValueError("observation identity providers disagree")


def _targets(policy: V4ObservationPolicy) -> tuple[RuntimeIdentityPin, ...]:
    values = [policy.pool_manager, policy.position_manager, policy.state_view]
    if policy.hook is not None:
        values.append(policy.hook)
    return tuple(values)


def _expected(capture: V4ObservationCapture) -> list[tuple[str, list[object]]]:
    block = capture.header.ref
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    policy = capture.identity.policy
    expected: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]
    expected.extend(("eth_getCode", [item.address, pin]) for item in _targets(policy))
    expected.extend(
        [
            (
                "eth_call",
                [{"to": policy.position_manager.address, "data": POOL_MANAGER_SELECTOR}, pin],
            ),
            (
                "eth_call",
                [{"to": policy.state_view.address, "data": POOL_MANAGER_SELECTOR}, pin],
            ),
            ("eth_getBlockByNumber", [hex(block.number), False]),
        ]
    )
    return expected


def _validate_capture(capture: V4ObservationCapture) -> None:
    policy = capture.identity.policy
    results = successful_results(capture.exchanges, _expected(capture))
    if int(str(results[0]), 16) != policy.chain_id:
        raise ValueError("observation chain mismatch")
    if decode_header(results[1], chain_id=policy.chain_id) != capture.header:
        raise ValueError("observation opening header mismatch")
    target_count = len(_targets(policy))
    for target, result in zip(_targets(policy), results[2 : 2 + target_count], strict=True):
        require_code_hash(result, target.runtime_keccak)
    manager_results = results[2 + target_count : 4 + target_count]
    if any(decode_address_word(value) != policy.pool_manager.address for value in manager_results):
        raise ValueError("observation pool manager getter mismatch")
    if decode_header(results[-1], chain_id=policy.chain_id) != capture.header:
        raise ValueError("observation closing header mismatch")


def _collect_one(
    session: RecordedSession, *, policy: V4ObservationPolicy, block_number: int
) -> V4ObservationCapture:
    transport, recorder, source = session
    client = RpcClient(transport, expected_chain_id=policy.chain_id)
    header = client.get_block_snapshot(block_number)
    pin = {"blockHash": header.ref.block_hash, "requireCanonical": True}
    for target in _targets(policy):
        transport.request("eth_getCode", [target.address, pin])
    for target in (policy.position_manager, policy.state_view):
        transport.request("eth_call", [{"to": target.address, "data": POOL_MANAGER_SELECTOR}, pin])
    if client.get_block_snapshot(block_number) != header:
        raise ValueError("observation identity block changed")
    return V4ObservationCapture(
        source,
        header,
        V4ObservationIdentity(policy, pool_id_from_key(policy.pool_key)),
        recorder.exchanges,
    )


def collect_observation_identity_quorum(
    primary: RecordedSession,
    secondary: RecordedSession,
    *,
    policy: V4ObservationPolicy,
    block_number: int,
) -> V4ObservationIdentityQuorum:
    validate_session_pair(primary, secondary)
    if type(block_number) is not int or block_number < 0:
        raise ValueError("explicit nonnegative observation block required")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lp-observation-identity") as pool:
        left = pool.submit(_collect_one, primary, policy=policy, block_number=block_number)
        right = pool.submit(_collect_one, secondary, policy=policy, block_number=block_number)
        return V4ObservationIdentityQuorum(left.result(), right.result())


def _persist_one(
    store: EvidenceStore,
    capture: V4ObservationCapture,
    *,
    retrieved_at: str,
) -> EvidenceRef:
    return store.put(
        "v4_observation_identity",
        capture.payload,
        capture.header.ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation={
            "schema": "oasis.v4-observation-identity-evidence.v1",
            "raw_rpc_reconstruction": True,
            "release_approved": False,
            "execution_eligible": False,
        },
        retrieved_at=retrieved_at,
        schema_version=1,
    )


def persist_observation_identity_evidence(
    store: EvidenceStore,
    quorum: V4ObservationIdentityQuorum,
    *,
    retrieved_at: str | None = None,
) -> tuple[EvidenceRef, EvidenceRef]:
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    return (
        _persist_one(store, quorum.left, retrieved_at=timestamp),
        _persist_one(store, quorum.right, retrieved_at=timestamp),
    )


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _pin(value: object, name: str) -> RuntimeIdentityPin:
    data = _object(value, name)
    if set(data) != {"address", "runtime_keccak"}:
        raise ValueError(f"{name} has invalid fields")
    return RuntimeIdentityPin(
        _string(data["address"], f"{name} address"),
        _string(data["runtime_keccak"], f"{name} runtime"),
    )


def _parse_capture(payload: bytes) -> tuple[BlockSnapshot, V4ObservationIdentity]:
    data = strict_json_object(payload)
    if set(data) != {"schema", "source", "header", "identity"}:
        raise ValueError("observation identity payload has invalid fields")
    if data["schema"] != "oasis.v4-observation-identity.v1":
        raise ValueError("unsupported observation identity schema")
    header_data = _object(data["header"], "header")
    if set(header_data) != {"ref", "parent_hash"}:
        raise ValueError("observation header has invalid fields")
    ref_data = _object(header_data["ref"], "block reference")
    if set(ref_data) != {"chain_id", "number", "block_hash", "timestamp"}:
        raise ValueError("observation block has invalid fields")
    header = BlockSnapshot(
        BlockRef(
            _integer(ref_data["chain_id"], "chain ID"),
            _integer(ref_data["number"], "block number"),
            _string(ref_data["block_hash"], "block hash"),
            _integer(ref_data["timestamp"], "block timestamp"),
        ),
        _string(header_data["parent_hash"], "parent hash"),
    )
    identity_data = _object(data["identity"], "identity")
    if set(identity_data) != {"policy", "pool_id"}:
        raise ValueError("observation identity has invalid fields")
    return header, _parse_identity(identity_data)


def _parse_identity(data: dict[str, object]) -> V4ObservationIdentity:
    policy_data = _object(data["policy"], "policy")
    expected = {
        "chain_id",
        "pool_key",
        "pool_manager",
        "position_manager",
        "state_view",
        "hook",
    }
    if set(policy_data) != expected:
        raise ValueError("observation policy has invalid fields")
    pool_data = _object(policy_data["pool_key"], "PoolKey")
    if set(pool_data) != {"currency0", "currency1", "fee", "tick_spacing", "hooks"}:
        raise ValueError("PoolKey has invalid fields")
    pool_key = PoolKey(
        _string(pool_data["currency0"], "currency0"),
        _string(pool_data["currency1"], "currency1"),
        _integer(pool_data["fee"], "fee"),
        _integer(pool_data["tick_spacing"], "tick spacing"),
        _string(pool_data["hooks"], "hooks"),
    )
    hook_data = policy_data["hook"]
    policy = V4ObservationPolicy(
        _integer(policy_data["chain_id"], "chain ID"),
        pool_key,
        _pin(policy_data["pool_manager"], "PoolManager"),
        _pin(policy_data["position_manager"], "PositionManager"),
        _pin(policy_data["state_view"], "StateView"),
        None if hook_data is None else _pin(hook_data, "hook"),
    )
    return V4ObservationIdentity(policy, _string(data["pool_id"], "PoolId"))


def replay_observation_identity_evidence(
    store: EvidenceStore, reference: EvidenceRef
) -> V4ObservationCapture:
    record = store.get_record(reference)
    if record.request_payload is None:
        raise ValueError("observation identity lacks exact request evidence")
    header, identity = _parse_capture(record.payload)
    capture = V4ObservationCapture(
        reference.source,
        header,
        identity,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload or header.ref != reference.block:
        raise ValueError("observation identity differs from raw reconstruction")
    return capture


__all__ = [
    "RuntimeIdentityPin",
    "V4ObservationCapture",
    "V4ObservationIdentity",
    "V4ObservationIdentityQuorum",
    "V4ObservationPolicy",
    "collect_observation_identity_quorum",
    "persist_observation_identity_evidence",
    "replay_observation_identity_evidence",
]
