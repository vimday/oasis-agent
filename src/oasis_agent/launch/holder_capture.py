"""Bounded two-source ERC-20 balances with exact wire replay, never execution authority.

Candidates are explicit, not an indexer completeness claim. Supply coverage is conditional
on truthful standard ERC-20 semantics; proxy/admin/token safety remains unresolved.
"""

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from eth_hash.auto import keccak
from pydantic import Field, TypeAdapter

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    require_code_hash,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex
from ..rpc.transport import RpcExchange
from .models import (
    Address,
    Cycle,
    HolderBalance,
    HolderObservation,
    HolderTarget,
    Snapshot,
    StrictModel,
)
from .watcher import coherent

DECIMALS_SELECTOR = "0x" + keccak(b"decimals()")[:4].hex()
SUPPLY_SELECTOR = "0x" + keccak(b"totalSupply()")[:4].hex()
BALANCE_SELECTOR = "0x" + keccak(b"balanceOf(address)")[:4].hex()
MAX_CANDIDATES = 512


def _candidates(values: tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(values) <= MAX_CANDIDATES:
        raise ValueError("holder capture requires 1..512 explicit candidate addresses")
    checked = tuple(TypeAdapter(Address).validate_python(item, strict=True) for item in values)
    if len(set(checked)) != len(checked) or any(int(item, 16) == 0 for item in checked):
        raise ValueError("holder candidates must be unique nonzero addresses")
    return tuple(sorted(checked))


def _balance_data(address: str) -> str:
    return BALANCE_SELECTOR + "0" * 24 + address[2:]


def _uint_word(value: object) -> int:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ValueError("holder call must return exactly one ABI uint256 word")
    if any(char not in "0123456789abcdef" for char in value[2:]):
        raise ValueError("holder call contains noncanonical hex bytes")
    return int(value, 16)


class CapturePayload(StrictModel):
    schema_: Literal["oasis.holder-capture.v1"] = Field(alias="schema")
    candidates: Annotated[tuple[Address, ...], Field(min_length=1, max_length=MAX_CANDIDATES)]
    observation: HolderObservation


@dataclass(frozen=True, slots=True)
class HolderCapture:
    observation: HolderObservation
    candidates: tuple[str, ...]
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        if _candidates(self.candidates) != self.candidates:
            raise ValueError("holder candidates must be canonically ordered")
        sample = self.observation
        block = sample.snapshot
        pin: dict[str, object] = {"blockHash": block.block_hash, "requireCanonical": True}
        expected: list[tuple[str, list[object]]] = [
            ("eth_chainId", []),
            ("eth_getBlockByNumber", [hex(block.number), False]),
            ("eth_getCode", [sample.target_ca, pin]),
        ]
        expected.extend(
            ("eth_call", [{"to": sample.target_ca, "data": data}, pin])
            for data in (
                DECIMALS_SELECTOR,
                SUPPLY_SELECTOR,
                *(_balance_data(address) for address in self.candidates),
            )
        )
        expected.append(("eth_getBlockByNumber", [hex(block.number), False]))
        results = successful_results(self.exchanges, expected)
        if rpc_quantity(results[0]) != block.chain_id:
            raise ValueError("holder capture chain mismatch")
        for raw in (results[1], results[-1]):
            header = decode_header(raw, chain_id=block.chain_id)
            if header.ref != self.block or header.parent_hash != block.parent_hash:
                raise ValueError("holder capture header mismatch or reorg")
        require_code_hash(results[2], sample.runtime_hash)
        if _uint_word(results[3]) != sample.token_decimals:
            raise ValueError("holder decimals differ from raw response")
        if _uint_word(results[4]) != sample.total_supply_base_units:
            raise ValueError("holder supply differs from raw response")
        balances = tuple(
            HolderBalance(address=address, balance_base_units=value)
            for address, raw in zip(self.candidates, results[5:-1], strict=True)
            if (value := _uint_word(raw)) > 0
        )
        if balances != sample.balances:
            raise ValueError("holder balances differ from raw responses")
        complete = (
            sum(item.balance_base_units for item in balances) == sample.total_supply_base_units
        )
        if sample.complete != complete:
            raise ValueError("holder coverage differs from raw responses")
        if sample.raw_evidence_digest != sha256_hex(
            canonical_json(wire_envelope(self.exchanges, response=True))
        ):
            raise ValueError("holder raw evidence digest mismatch")
        if sample.retrieved_at < block.timestamp:
            raise ValueError("holder retrieval precedes block timestamp")

    @property
    def block(self) -> BlockRef:
        block = self.observation.snapshot
        return BlockRef(block.chain_id, block.number, block.block_hash, block.timestamp)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.holder-capture.v1",
                "candidates": self.candidates,
                "observation": self.observation.model_dump(mode="json"),
            }
        )


@dataclass(frozen=True, slots=True)
class HolderQuorum:
    left: HolderCapture
    right: HolderCapture

    def __post_init__(self) -> None:
        left, right = self.left, self.right
        if left.observation.source == right.observation.source:
            raise ValueError("holder quorum requires distinct sources")
        excluded = {"source", "retrieved_at", "raw_evidence_digest"}
        if left.candidates != right.candidates or left.observation.model_dump(
            exclude=excluded
        ) != right.observation.model_dump(exclude=excluded):
            raise ValueError("holder providers disagree")


def _collect_one(
    session: RecordedSession,
    target: HolderTarget,
    block_number: int,
    candidates: tuple[str, ...],
    deadline: float,
    clock: Callable[[], float],
) -> HolderCapture:
    transport, recorder, source = session

    def request(method: str, params: list[object]) -> object:
        if clock() >= deadline:
            raise TimeoutError("holder acquisition deadline exceeded")
        result = transport.request(method, params)
        if clock() >= deadline:
            raise TimeoutError("holder acquisition deadline exceeded")
        return result

    if rpc_quantity(request("eth_chainId", [])) != target.chain_id:
        raise ValueError("holder RPC chain mismatch")
    header = decode_header(
        request("eth_getBlockByNumber", [hex(block_number), False]), chain_id=target.chain_id
    )
    if header.ref.number != block_number:
        raise ValueError("holder RPC returned wrong block")
    pin: dict[str, object] = {"blockHash": header.ref.block_hash, "requireCanonical": True}
    assert target.runtime_hash is not None
    require_code_hash(request("eth_getCode", [target.target_ca, pin]), target.runtime_hash)

    def call(data: str) -> int:
        return _uint_word(request("eth_call", [{"to": target.target_ca, "data": data}, pin]))

    decimals, supply = call(DECIMALS_SELECTOR), call(SUPPLY_SELECTOR)
    if decimals != target.token_decimals or supply == 0:
        raise ValueError("holder token decimals mismatch or zero supply")
    balances = tuple(
        HolderBalance(address=address, balance_base_units=value)
        for address in candidates
        if (value := call(_balance_data(address))) > 0
    )
    if (
        decode_header(
            request("eth_getBlockByNumber", [hex(block_number), False]), chain_id=target.chain_id
        )
        != header
    ):
        raise ValueError("holder block changed during acquisition")
    if header.ref.timestamp is None:
        raise ValueError("holder block timestamp missing")
    observation = HolderObservation(
        snapshot=Snapshot(
            chain_id=target.chain_id,
            number=block_number,
            block_hash=header.ref.block_hash,
            timestamp=header.ref.timestamp,
            parent_hash=header.parent_hash,
        ),
        source=source,
        retrieved_at=int(datetime.now(UTC).timestamp()),
        raw_evidence_digest=sha256_hex(
            canonical_json(wire_envelope(recorder.exchanges, response=True))
        ),
        target_ca=target.target_ca,
        runtime_hash=target.runtime_hash,
        token_decimals=decimals,
        total_supply_base_units=supply,
        balances=balances,
        complete=sum(item.balance_base_units for item in balances) == supply,
    )
    return HolderCapture(observation, candidates, recorder.exchanges)


def collect_holder_quorum(
    primary: RecordedSession,
    secondary: RecordedSession,
    *,
    target: HolderTarget,
    block_number: int,
    candidates: tuple[str, ...],
    deadline_seconds: int = 60,
    clock: Callable[[], float] = time.monotonic,
) -> HolderQuorum:
    """Parallel providers, sequential pinned calls per provider, bounded wall-time budget.

    Transports must also have bounded per-request timeout and retries=0. The deadline
    detects an overrun after an in-flight request; it does not interrupt HTTP itself.
    """
    validate_session_pair(primary, secondary)
    addresses = _candidates(candidates)
    if type(block_number) is not int or block_number < 0:
        raise ValueError("explicit nonnegative holder block required")
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 300:
        raise ValueError("holder deadline must be 1..300 seconds")
    if target.runtime_hash is None or target.token_decimals is None:
        raise ValueError("frozen holder runtime and decimals required")
    deadline = clock() + deadline_seconds
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="holders") as workers:
        left = workers.submit(
            _collect_one, primary, target, block_number, addresses, deadline, clock
        )
        right = workers.submit(
            _collect_one, secondary, target, block_number, addresses, deadline, clock
        )
        return HolderQuorum(left.result(), right.result())


def persist_holder_quorum(
    store: EvidenceStore, quorum: HolderQuorum
) -> tuple[EvidenceRef, EvidenceRef]:
    def retain(capture: HolderCapture) -> EvidenceRef:
        sample = capture.observation
        return store.put(
            "holder_capture",
            capture.payload,
            capture.block,
            sample.source,
            request=wire_envelope(capture.exchanges, response=False),
            raw_response=wire_envelope(capture.exchanges, response=True),
            interpretation={"schema": "oasis.holder-capture.v1", "execution_eligible": False},
            retrieved_at=datetime.fromtimestamp(sample.retrieved_at, UTC).isoformat(),
        )

    return retain(quorum.left), retain(quorum.right)


def replay_holder_evidence(store: EvidenceStore, reference: EvidenceRef) -> HolderCapture:
    record = store.get_record(reference)
    if reference.kind != "holder_capture" or record.request_payload is None:
        raise ValueError("holder reference kind or request missing")
    payload = CapturePayload.from_json(record.payload)
    capture = HolderCapture(
        payload.observation,
        payload.candidates,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    sample = capture.observation
    if (
        capture.payload != record.payload
        or reference.block != capture.block
        or reference.source != sample.source
        or reference.raw_response_hash != sample.raw_evidence_digest
        or reference.retrieved_at != datetime.fromtimestamp(sample.retrieved_at, UTC).isoformat()
    ):
        raise ValueError("holder evidence provenance binding mismatch")
    return capture


def bind_holder_evidence(
    store: EvidenceStore,
    references: tuple[EvidenceRef, EvidenceRef],
    cycle: Cycle,
) -> Cycle:
    """Attach replayed balances acquired before a market cycle's evaluation time.

    Does not advance clocks or amend a historical decision. The producer must collect
    all required inputs before constructing the final cycle and independently verify
    market evidence. Raw holder replay alone cannot qualify that market observation.
    """
    if not coherent(cycle.observations) or cycle.holder_observations is not None:
        raise ValueError("fresh coherent market cycle without holders required")
    quorum = HolderQuorum(*(replay_holder_evidence(store, ref) for ref in references))
    captures = {item.observation.source: item.observation for item in (quorum.left, quorum.right)}
    for market in cycle.observations:
        holder = captures.get(market.source)
        if (
            holder is None
            or holder.snapshot != market.snapshot
            or holder.target_ca != market.target_ca
            or holder.runtime_hash != market.runtime_hash
            or holder.token_decimals != market.token_decimals
            or not holder.snapshot.timestamp <= holder.retrieved_at <= cycle.evaluated_at
        ):
            raise ValueError(
                "holder evidence differs from market identity, snapshot or decision time"
            )
    return Cycle.model_validate(
        cycle.model_dump()
        | {
            "holder_observations": tuple(captures[item.source] for item in cycle.observations),
        }
    )
