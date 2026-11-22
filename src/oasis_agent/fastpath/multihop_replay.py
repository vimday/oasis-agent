"""Persist and validate Base shadow inputs for offline model replay."""

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeVar

from ..evidence.base_denim import BASE_CHAIN_ID, validate_header_timestamp_ms
from ..evidence.rpc_validation import (
    rpc_quantity,
    strict_digest,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from ..models.common import canonical_json, normalize_address, normalize_hash32, sha256_hex
from ..rpc.protocol import response_envelope
from ..sniping.venues import USDC, WETH
from .incremental_scanner import BlockPoolSnapshot
from .pool_graph import PoolNode

REPLAY_SCHEMA: Final[str] = "oasis.base-multihop-replay.v1"
REPLAY_DIRECTORY: Final[str] = "multihop_replay"
REPLAY_MANIFEST: Final[str] = "manifest.json"
_U256_MAX: Final[int] = (1 << 256) - 1

RpcSend = Callable[[str, bytes, float], bytes]
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ReplayDecodedState:
    pools: tuple[PoolNode, ...]
    l1_fee_wei: int
    eth_price_micro_usdc: int


# The offline decoder must use the original plan and issue the exact retained requests.
ReplayDecode = Callable[[str, list[object], RpcSend, str], ReplayDecodedState]


@dataclass(frozen=True, slots=True)
class ReplayAnchorInput:
    base_token: str
    min_amount_in: int
    max_amount_in: int
    min_net_profit: int
    gas_cost_in_base: int
    l1_fee_in_base: int

    def to_wire(self) -> dict[str, str]:
        return {
            "base_token": self.base_token,
            "min_amount_in": str(self.min_amount_in),
            "max_amount_in": str(self.max_amount_in),
            "min_net_profit": str(self.min_net_profit),
            "gas_cost_in_base": str(self.gas_cost_in_base),
            "l1_fee_in_base": str(self.l1_fee_in_base),
        }

    @classmethod
    def from_wire(cls, raw: object) -> ReplayAnchorInput:
        item = strict_object(raw, "scan anchor input")
        result = cls(
            base_token=normalize_address(strict_string(item.get("base_token"), "base_token")),
            min_amount_in=_uint256(item.get("min_amount_in"), "min_amount_in"),
            max_amount_in=_uint256(item.get("max_amount_in"), "max_amount_in"),
            min_net_profit=_uint256(item.get("min_net_profit"), "min_net_profit"),
            gas_cost_in_base=_uint256(item.get("gas_cost_in_base"), "gas_cost_in_base"),
            l1_fee_in_base=_uint256(item.get("l1_fee_in_base"), "l1_fee_in_base"),
        )
        if result.min_amount_in <= 0 or result.min_amount_in > result.max_amount_in:
            raise ValueError("incoherent replay size bounds")
        if item != result.to_wire():
            raise ValueError("noncanonical scan anchor input")
        return result


def _uint256(value: object, name: str) -> int:
    raw = strict_string(value, name)
    if not raw.isascii() or not raw.isdigit() or len(raw) > 78:
        raise ValueError(f"{name} must be a decimal uint256")
    parsed = int(raw)
    if parsed > _U256_MAX or raw != str(parsed):
        raise ValueError(f"{name} must be a canonical decimal uint256")
    return parsed


@dataclass(frozen=True, slots=True)
class _RetainedCall:
    request: bytes
    response: bytes


@dataclass(frozen=True, slots=True)
class _ReplayBlock:
    number: int
    block_hash: str
    parent_hash: str
    state_root: str
    timestamp_ms: int
    base_fee_per_gas: int

    def to_wire(self) -> dict[str, object]:
        return {
            "block_number": self.number,
            "block_hash": self.block_hash,
            "parent_hash": self.parent_hash,
            "state_root": self.state_root,
            "timestamp_ms": self.timestamp_ms,
            "base_fee_per_gas_wei": str(self.base_fee_per_gas),
        }


def _snapshot_wire_header(calls: Sequence[_RetainedCall]) -> tuple[_ReplayBlock, str]:
    if len(calls) != 2:
        raise ValueError("replay snapshot requires exactly one header and one Multicall3 exchange")
    header_req = strict_json_object(calls[0].request)
    params = strict_list(header_req.get("params"), "header request params")
    if (
        header_req.get("method") != "eth_getBlockByNumber"
        or len(params) != 2
        or params[1] is not False
    ):
        raise ValueError("invalid replay header request")
    block_tag = strict_string(params[0], "block tag")
    hdr_reply = response_envelope(
        calls[0].response, strict_integer(header_req.get("id"), "header request ID")
    )
    header = strict_object(hdr_reply.get("result"), "replay header")
    number = rpc_quantity(header.get("number"))
    if block_tag != "latest" and rpc_quantity(block_tag) != number:
        raise ValueError("block header does not match the requested height")
    timestamp_ms = validate_header_timestamp_ms(header, chain_id=BASE_CHAIN_ID)
    if timestamp_ms is None:
        raise ValueError("replay header is missing the checked millisecond timestamp")
    block = _ReplayBlock(
        number=number,
        block_hash=normalize_hash32(strict_string(header.get("hash"), "hash"), "hash"),
        parent_hash=normalize_hash32(
            strict_string(header.get("parentHash"), "parentHash"), "parentHash"
        ),
        state_root=normalize_hash32(
            strict_string(header.get("stateRoot"), "stateRoot"), "stateRoot"
        ),
        timestamp_ms=timestamp_ms,
        base_fee_per_gas=rpc_quantity(header.get("baseFeePerGas")),
    )
    if block.block_hash == block.parent_hash:
        raise ValueError("replay block is its own parent")
    call_req = strict_json_object(calls[1].request)
    params = strict_list(call_req.get("params"), "Multicall3 request params")
    if (
        call_req.get("method") != "eth_call"
        or len(params) != 2
        or params[1] != {"blockHash": block.block_hash, "requireCanonical": True}
        or strict_object(params[1], "block parameter").get("requireCanonical") is not True
    ):
        raise ValueError("replay Multicall3 request is not pinned to the canonical block hash")
    return block, block_tag


def _write_new(path: Path, payload: bytes) -> str:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return sha256_hex(payload)


def _read_verified(directory: Path, filename: str, digest: object) -> bytes:
    path = directory / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing or non-regular replay artifact {filename}")
    raw = path.read_bytes()
    if sha256_hex(raw) != strict_digest(digest, filename):
        raise ValueError(f"replay digest mismatch for {filename}")
    return raw


class MultihopReplayCapture:
    """Append original RPC bodies and interpreted snapshots to a fresh shadow artifact."""

    def __init__(self, directory: Path, *, source: str, mode: str, ticks_requested: int) -> None:
        directory.mkdir()
        self.directory = directory
        self.source = source
        self.mode = mode
        self.ticks_requested = ticks_requested
        self.attempts: list[list[dict[str, object]]] = []
        self.frames: list[dict[str, object]] = []
        self.last_calls: tuple[_RetainedCall, ...] = ()

    def record_attempt(self, send: RpcSend, read: Callable[[RpcSend], _T]) -> _T:
        refs: list[dict[str, object]] = []
        self.attempts.append(refs)
        attempt = len(self.attempts)
        calls: list[_RetainedCall] = []
        self.last_calls = ()

        def record(url: str, request: bytes, timeout: float) -> bytes:
            prefix = f"rpc-{attempt:08d}-{len(refs) + 1:03d}"
            request_sha256 = _write_new(self.directory / f"{prefix}.request.json", request)
            ref: dict[str, object] = {
                "request_sha256": request_sha256,
                "response_sha256": None,
                "received_unix_ns": None,
                "received_monotonic_ns": None,
            }
            refs.append(ref)
            response = send(url, request, timeout)
            ref["received_unix_ns"] = time.time_ns()
            ref["received_monotonic_ns"] = time.monotonic_ns()
            ref["response_sha256"] = _write_new(
                self.directory / f"{prefix}.response.json", response
            )
            calls.append(_RetainedCall(request, response))
            return response

        result = read(record)
        self.last_calls = tuple(calls)
        return result

    def save_snapshot(
        self,
        *,
        block_number: int,
        block_hash: str,
        pools: Sequence[PoolNode],
        anchors: Sequence[ReplayAnchorInput],
        gas_limit_units: int,
        priority_fee_wei: int,
        eth_price_micro_usdc: int,
        headroom_blocked: bool,
    ) -> None:
        block = self._validated_block(block_number, block_hash)
        snapshot = {
            **block.to_wire(),
            "attempt": len(self.attempts),
            "pools": [pool.to_wire() for pool in pools],
            "anchors": [anchor.to_wire() for anchor in anchors],
            "gas_limit_units": gas_limit_units,
            "priority_fee_wei": str(priority_fee_wei),
            "eth_price_micro_usdc": str(eth_price_micro_usdc),
            "headroom_blocked": headroom_blocked,
        }
        filename = f"snapshot-{len(self.frames) + 1:08d}.json"
        digest = _write_new(self.directory / filename, canonical_json(snapshot) + b"\n")
        self.frames.append({"sha256": digest})

    def validate_snapshot(self, block_number: int, block_hash: str) -> None:
        self._validated_block(block_number, block_hash)

    def _validated_block(self, block_number: int, block_hash: str) -> _ReplayBlock:
        block, _ = _snapshot_wire_header(self.last_calls)
        if block.number != block_number or block.block_hash != block_hash:
            raise ValueError("used replay snapshot does not match the retained block")
        return block

    def finish(
        self,
        *,
        pool_specs: Sequence[dict[str, object]],
        snapshot_format: str,
        stopped_early: bool,
        canonical_conflict: bool,
        campaign_summary_sha256: str,
    ) -> None:
        body = {
            "schema": REPLAY_SCHEMA,
            "chain_id": BASE_CHAIN_ID,
            "source": self.source,
            "mode": self.mode,
            "snapshot_format": snapshot_format,
            "cl_state": "synthetic_model_bracket",
            "pool_runtime_identity": "not_verified",
            "source_quorum": "single_transport",
            "cost_model": "fixed_l2_gas_limit_and_l1_fee_probe",
            "max_hops": 3,
            "deduplicate_cycles": False,
            "execution_eligible": False,
            "release_permitted": False,
            "ticks_requested": self.ticks_requested,
            "stopped_early": stopped_early,
            "canonical_conflict": canonical_conflict,
            "campaign_summary_sha256": campaign_summary_sha256,
            "pool_specs": list(pool_specs),
            "attempts": self.attempts,
            "snapshots": self.frames,
        }
        manifest = {**body, "manifest_sha256": sha256_hex(canonical_json(body))}
        _write_new(self.directory / REPLAY_MANIFEST, canonical_json(manifest) + b"\n")


def _call_pair(
    directory: Path, attempts: list[object], attempt: int, *, allow_failed: bool = False
) -> tuple[_RetainedCall, ...]:
    if not 1 <= attempt <= len(attempts):
        raise ValueError("replay snapshot references a missing RPC attempt")
    refs = strict_list(attempts[attempt - 1], "RPC attempt")
    calls = []
    last_receive = 0
    for ordinal, value in enumerate(refs, 1):
        ref = strict_object(value, "RPC artifact reference")
        prefix = f"rpc-{attempt:08d}-{ordinal:03d}"
        request = _read_verified(directory, f"{prefix}.request.json", ref.get("request_sha256"))
        if ref.get("response_sha256") is None:
            if (
                not allow_failed
                or ordinal != len(refs)
                or ref.get("received_unix_ns") is not None
                or ref.get("received_monotonic_ns") is not None
            ):
                raise ValueError("missing snapshot response")
            break
        if strict_integer(ref.get("received_unix_ns"), "receive UTC") <= 0:
            raise ValueError("invalid UTC receive clock")
        receive = strict_integer(ref.get("received_monotonic_ns"), "receive monotonic")
        if receive <= 0 or receive < last_receive:
            raise ValueError("invalid monotonic receive clock order")
        last_receive = receive
        calls.append(
            _RetainedCall(
                request,
                _read_verified(directory, f"{prefix}.response.json", ref.get("response_sha256")),
            )
        )
    return tuple(calls)


def _decode_original(
    calls: Sequence[_RetainedCall],
    decode_snapshot: ReplayDecode,
    fmt: str,
    specs: list[object],
    block_tag: str,
) -> ReplayDecodedState:
    consumed = 0

    def offline_send(_url: str, request: bytes, _timeout: float) -> bytes:
        nonlocal consumed
        if consumed >= len(calls) or request != calls[consumed].request:
            raise ValueError("replay decoder request differs from retained request bytes")
        response = calls[consumed].response
        consumed += 1
        return response

    state = decode_snapshot(fmt, specs, offline_send, block_tag)
    if consumed != len(calls):
        raise ValueError("replay decoder did not consume every retained RPC exchange")
    return state


def _validate_replay_cost(
    frame: dict[str, object],
    block: _ReplayBlock,
    state: ReplayDecodedState,
    anchors: list[ReplayAnchorInput],
) -> None:
    gas_limit_units = strict_integer(frame.get("gas_limit_units"), "gas_limit_units")
    priority_fee_wei = _uint256(frame.get("priority_fee_wei"), "priority_fee_wei")
    eth_price = _uint256(frame.get("eth_price_micro_usdc"), "eth_price_micro_usdc")
    if gas_limit_units <= 0 or eth_price <= 0 or eth_price != state.eth_price_micro_usdc:
        raise ValueError("missing or inconsistent replay fee input")
    l2_wei = gas_limit_units * (block.base_fee_per_gas + priority_fee_wei)
    wei_per_eth = 10**18
    expected = {
        normalize_address(WETH): (l2_wei, state.l1_fee_wei),
        normalize_address(USDC): (
            (l2_wei * eth_price + wei_per_eth - 1) // wei_per_eth,
            (state.l1_fee_wei * eth_price + wei_per_eth - 1) // wei_per_eth,
        ),
    }
    if {a.base_token: (a.gas_cost_in_base, a.l1_fee_in_base) for a in anchors} != expected:
        raise ValueError("captured anchor costs differ from the retained fee inputs")


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    snapshots: tuple[BlockPoolSnapshot, ...]
    max_hops: int
    metadata: dict[str, object]


def load_multihop_replay(
    manifest_path: Path, *, base_token: str, decode_snapshot: ReplayDecode
) -> ReplayDataset:
    """Verify the retained hash-pinned exchanges and replay them without network access."""
    anchor_token = normalize_address(base_token)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("replay manifest must be a regular file")
    body = strict_json_object(manifest_path.read_bytes())
    manifest_sha256 = strict_digest(body.pop("manifest_sha256", None), "manifest_sha256")
    if sha256_hex(canonical_json(body)) != manifest_sha256:
        raise ValueError("replay manifest digest mismatch")
    if (
        body.get("schema") != REPLAY_SCHEMA
        or strict_integer(body.get("chain_id"), "chain ID") != BASE_CHAIN_ID
        or body.get("execution_eligible") is not False
        or body.get("release_permitted") is not False
        or body.get("deduplicate_cycles") is not False
        or body.get("cl_state") != "synthetic_model_bracket"
        or body.get("pool_runtime_identity") != "not_verified"
        or body.get("source_quorum") != "single_transport"
        or body.get("cost_model") != "fixed_l2_gas_limit_and_l1_fee_probe"
    ):
        raise ValueError("unsupported replay schema, evidence scope, or chain")
    source = strict_string(body.get("source"), "source")
    mode = strict_string(body.get("mode"), "mode")
    if mode not in {"fixture", "injected", "public-shadow"} or not source:
        raise ValueError("invalid replay source or mode")
    fmt = strict_string(body.get("snapshot_format"), "snapshot_format")
    max_hops = strict_integer(body.get("max_hops"), "max_hops")
    if not 2 <= max_hops <= 4:
        raise ValueError("unsupported replay hop limit")
    specs = strict_list(body.get("pool_specs"), "pool_specs")
    attempts = strict_list(body.get("attempts"), "attempts")
    entries = strict_list(body.get("snapshots"), "snapshots")
    ticks_requested = strict_integer(body.get("ticks_requested"), "ticks_requested")
    if (
        not entries
        or ticks_requested < len(entries)
        or body.get("stopped_early") is not (len(entries) < ticks_requested)
    ):
        raise ValueError("incomplete replay capture or inconsistent coverage")
    if body.get("canonical_conflict") is not False:
        raise ValueError("replay capture contains a canonical conflict")
    snapshots: list[BlockPoolSnapshot] = []
    prior: _ReplayBlock | None = None
    prior_pools: list[dict[str, object]] | None = None
    gap_blocks = duplicate_ticks = 0
    last_attempt = headroom_blocked_ticks = 0
    checked_attempts: set[int] = set()
    for seq, entry in enumerate(entries, 1):
        ref = strict_object(entry, "snapshot reference")
        raw = _read_verified(manifest_path.parent, f"snapshot-{seq:08d}.json", ref.get("sha256"))
        frame = strict_json_object(raw)
        attempt = strict_integer(frame.get("attempt"), "snapshot attempt")
        if attempt <= last_attempt:
            raise ValueError("replay snapshot RPC attempts must strictly increase")
        last_attempt = attempt
        calls = _call_pair(manifest_path.parent, attempts, attempt)
        checked_attempts.add(attempt)
        block, tag = _snapshot_wire_header(calls)
        if any(frame.get(k) != v for k, v in block.to_wire().items()):
            raise ValueError("snapshot block identity differs from retained header")
        state = _decode_original(calls, decode_snapshot, fmt, specs, tag)
        pools = state.pools
        pool_wire = [pool.to_wire() for pool in pools]
        if (
            not pools
            or frame.get("pools") != pool_wire
            or len({pool.pool_id for pool in pools}) != len(pools)
            or any(
                pool.chain_id != BASE_CHAIN_ID
                or pool.block_number != block.number
                or pool.timestamp_ms != block.timestamp_ms
                for pool in pools
            )
        ):
            raise ValueError("parsed pool graph differs from original snapshot responses")
        gap = False
        if prior is not None:
            if block.number < prior.number or block.timestamp_ms < prior.timestamp_ms:
                raise ValueError("replay block regressed")
            if block.number == prior.number:
                if block != prior or pool_wire != prior_pools:
                    raise ValueError("conflicting replay state at the same block height")
                duplicate_ticks += 1
            elif block.number == prior.number + 1:
                if block.parent_hash != prior.block_hash:
                    raise ValueError("replay reorg: parent hash differs from previous block")
            else:
                gap = True
                gap_blocks += block.number - prior.number - 1
        anchors = [
            ReplayAnchorInput.from_wire(item)
            for item in strict_list(frame.get("anchors"), "anchors")
        ]
        matching = [anchor for anchor in anchors if anchor.base_token == anchor_token]
        if len(matching) != 1 or len({anchor.base_token for anchor in anchors}) != len(anchors):
            raise ValueError("requested base token is missing or duplicated in captured inputs")
        _validate_replay_cost(frame, block, state, anchors)
        blocked = frame.get("headroom_blocked")
        if type(blocked) is not bool:
            raise ValueError("missing recorded headroom state")
        headroom_blocked_ticks += blocked
        anchor = matching[0]
        snapshots.append(
            BlockPoolSnapshot(
                block_number=block.number,
                timestamp_ms=block.timestamp_ms,
                pools=pools,
                base_tokens=(anchor.base_token,),
                gas_cost_in_base=anchor.gas_cost_in_base,
                l1_fee_in_base=anchor.l1_fee_in_base,
                min_amount_in=anchor.min_amount_in,
                max_amount_in=anchor.max_amount_in,
                min_net_profit=anchor.min_net_profit,
                force_full_scan=gap,
                headroom_blocked=blocked,
            )
        )
        prior, prior_pools = block, pool_wire

    for attempt in range(1, len(attempts) + 1):
        if attempt not in checked_attempts:
            _call_pair(manifest_path.parent, attempts, attempt, allow_failed=True)
    return ReplayDataset(
        snapshots=tuple(snapshots),
        max_hops=max_hops,
        metadata={
            "schema": REPLAY_SCHEMA,
            "manifest_sha256": manifest_sha256,
            "campaign_summary_sha256": strict_digest(
                body.get("campaign_summary_sha256"), "campaign summary SHA-256"
            ),
            "chain_id": BASE_CHAIN_ID,
            "source": source,
            "mode": mode,
            "cl_state": body["cl_state"],
            "exact_tick_coverage": "unavailable",
            "pool_runtime_identity": body["pool_runtime_identity"],
            "source_quorum": body["source_quorum"],
            "cost_model": body["cost_model"],
            "anchor": anchor_token,
            "missing_heights_between_samples": gap_blocks,
            "duplicate_ticks": duplicate_ticks,
            "headroom_blocked_ticks": headroom_blocked_ticks,
            "unique_blocks": len(snapshots) - duplicate_ticks,
            "ticks_requested": ticks_requested,
            "stopped_early": body["stopped_early"],
        },
    )
