"""Capture and rebuild bounded Base mixed state without network access on replay."""

import json
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Protocol, cast

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_hash.auto import keccak

from ..arbitrage.event_quote import V4
from ..errors import FeeEncodingError, OasisError, RpcError
from ..evidence.base_denim import BASE_TIME, validate_header_timestamp_ms
from ..evidence.rpc_validation import (
    rpc_quantity,
    strict_digest,
    strict_integer,
    strict_json_object,
    strict_object,
    strict_string,
)
from ..models.common import ZERO_ADDRESS, canonical_json, normalize_hash32, sha256_hex
from ..models.v4 import PoolKey
from ..rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from ..rpc.protocol import response_envelope
from ..uniswap_v4.fees import calculate_swap_fee
from ..uniswap_v4.liquidity_math import MAX_TICK, MIN_TICK, sqrt_price_at_tick
from ..uniswap_v4.pool_id import pool_id_from_key
from ..uniswap_v4.state_rpc import Slot0State
from .live_shadow_runner import (
    DEFAULT_BASE_RPC_URL,
    MAX_RESPONSE_BYTES,
    canonical_base_weth_usdc_pools,
)
from .multicall_universe import (
    MULTICALL3,
    DiscoveredPool,
    PinnedUniverseSnapshot,
    _decode_aggregate3_results,
    _decode_hex_bytes,
    _encode_aggregate3_calldata,
    fetch_pinned_universe_snapshot,
)
from .pool_graph import PoolKind, PoolNode, TickLiquidity

PUBLICNODE = "publicnode_base"
OFFICIAL = "base_official"
OFFICIAL_URL = "https://mainnet.base.org"
SOURCES = {PUBLICNODE: DEFAULT_BASE_RPC_URL, OFFICIAL: OFFICIAL_URL}
SCHEMA_V1 = "oasis.base-mixed-shadow.v1"
SCHEMA = "oasis.base-mixed-shadow.v2"
DIRECTORY = "mixed_base_state"
CHAIN_ID = 8453
ADDRESS_POOLS = 58
MANAGER = V4.address
MANAGER_RUNTIME = V4.runtime_hash
STATE_VIEW = "0xa3c0c9b65bad0b08107aa264b0f3db444b867a71"
POSITION_MANAGER = "0x7c5f5a4bbd8fd63184577525326123b519429bdc"
L1BLOCK = "0x4200000000000000000000000000000000000015"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
TAIL = "0xa9b631abcc4fd0bc766d7c0c8fcbf866e2bb0445"
DECIMALS = {WETH: 18, USDC: 6, USDBC: 6, TAIL: 18}
KEY_ABI = ["address", "address", "uint24", "int24", "address"]
# Fixed keys and bitmap words reproduced in BASE_STATE_DELIVERY_AB_20261123.md.
V4_KEYS = (
    (PoolKey(WETH, USDC, 3000, 60, ZERO_ADDRESS), -16),
    (PoolKey(ZERO_ADDRESS, TAIL, 3000, 60, ZERO_ADDRESS), 9),
    (PoolKey(USDC, USDBC, 3000, 60, ZERO_ADDRESS), -2),
    (PoolKey(USDC, USDBC, 6, 1, ZERO_ADDRESS), -2),
    (PoolKey(ZERO_ADDRESS, USDC, 500, 10, ZERO_ADDRESS), -81),
    (PoolKey(USDC, USDBC, 49, 1, ZERO_ADDRESS), -2),
    (PoolKey(USDC, TAIL, 75, 1, ZERO_ADDRESS), 1465),
    (PoolKey(USDC, USDBC, 100, 1, ZERO_ADDRESS), -2),
)
_ROLES = {
    "primary_header": PUBLICNODE,
    "secondary_header": OFFICIAL,
    "manager_code": OFFICIAL,
    "address_state": PUBLICNODE,
    "v4_state": OFFICIAL,
}
type Send = Callable[[str, bytes, float], bytes]
type Call = tuple[str, bool, bytes]


@dataclass(frozen=True, slots=True)
class BlockBinding:
    number: int
    block_hash: str
    parent_hash: str
    state_root: str
    timestamp_ms: int
    base_fee: int


@dataclass(frozen=True, slots=True)
class MixedState:
    block: BlockBinding
    address_state: PinnedUniverseSnapshot
    v4_pools: tuple[PoolNode, ...]
    bitmap_bounds: dict[str, tuple[int, int]]
    operator_scalar: int
    operator_constant: int

    @property
    def nodes(self) -> tuple[PoolNode, ...]:
        return (*self.address_state.pool_nodes, *self.v4_pools)

    def evidence(self) -> dict[str, object]:
        return {
            "block": asdict(self.block),
            "pools": [pool.to_wire() for pool in self.nodes],
            "address_pool_balances": self.address_state.balances_by_pool,
            "address_l1_fee_probe_wei": self.address_state.l1_fee_wei,
            "v4_bitmap_bounds": self.bitmap_bounds,
            "operator_scalar": self.operator_scalar,
            "operator_constant": self.operator_constant,
        }


@dataclass(frozen=True, slots=True)
class MixedCaptureResult:
    state: MixedState
    manifest_sha256: str
    manifest_path: Path
    state_ready_monotonic_ns: int


class _Exchange(Protocol):
    def __call__(self, role: str, request: bytes) -> bytes: ...


def address_roster(raw_specs: Sequence[object]) -> tuple[DiscoveredPool, ...]:
    def optional_int(val: object, field: str) -> int | None:
        return None if val is None else strict_integer(val, field)

    specs = []
    for item in raw_specs:
        row = strict_object(item, "address pool spec")

        specs.append(
            DiscoveredPool(
                pool_id=strict_string(row["pool_id"], "pool_id"),
                label=strict_string(row["label"], "label"),
                venue=strict_string(row["venue"], "venue"),
                kind=PoolKind(strict_string(row["kind"], "kind")),
                token0=strict_string(row["token0"], "token0"),
                token1=strict_string(row["token1"], "token1"),
                decimals0=strict_integer(row["decimals0"], "decimals0"),
                decimals1=strict_integer(row["decimals1"], "decimals1"),
                fixed_fee_pips=optional_int(row["fixed_fee_pips"], "fixed_fee_pips"),
                tick_spacing=optional_int(row["tick_spacing"], "tick_spacing"),
                factory=strict_string(row["factory"], "factory"),
                reserve0=strict_integer(row["reserve0"], "reserve0"),
                reserve1=strict_integer(row["reserve1"], "reserve1"),
            )
        )
    ids = {spec.pool_id for spec in specs}
    controls = {pool.address: pool for pool in canonical_base_weth_usdc_pools()}
    if len(specs) != ADDRESS_POOLS or len(ids) != ADDRESS_POOLS or not controls.keys() <= ids:
        raise ValueError("mixed Base requires the frozen 58 address pools and all four controls")
    for spec in specs:
        control = controls.get(spec.pool_id)
        if control is not None and (
            (spec.kind, spec.token0, spec.token1, spec.decimals0, spec.decimals1)
            != (control.kind, control.token0, control.token1, control.decimals0, control.decimals1)
            or spec.fixed_fee_pips != control.fixed_fee_pips
            or (control.factory is not None and spec.factory != control.factory)
        ):
            raise ValueError("mixed Base canonical control identity changed")
    return tuple(specs)


def _request(method: str, params: list[object], *, req_id: int = 1) -> bytes:
    # Identical encoding to the existing address snapshot transport.
    return json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
        separators=(",", ":"),
    ).encode()


def _result(raw: bytes, req_id: int = 1) -> object:
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("mixed Base RPC response exceeds byte limit")
    envelope = response_envelope(raw, req_id)
    if "error" in envelope or envelope.get("result") is None:
        raise ValueError("mixed Base RPC returned an error or null result")
    return envelope["result"]


def _header(raw: bytes) -> BlockBinding:
    header = strict_object(_result(raw), "mixed Base header")
    block = BlockBinding(
        rpc_quantity(header.get("number")),
        normalize_hash32(strict_string(header.get("hash"), "block hash"), "block hash"),
        normalize_hash32(strict_string(header.get("parentHash"), "parent hash"), "parent hash"),
        normalize_hash32(strict_string(header.get("stateRoot"), "state root"), "state root"),
        validate_header_timestamp_ms(header, chain_id=CHAIN_ID) or 0,
        rpc_quantity(header.get("baseFeePerGas")),
    )
    if not block.timestamp_ms or block.block_hash == block.parent_hash:
        raise ValueError("invalid Base mixed header")
    return block


def _calldata(signature: str, types: Sequence[str] = (), args: Sequence[object] = ()) -> bytes:
    return keccak(signature.encode())[:4] + encode(types, args)


def _words(types: Sequence[str], raw: bytes) -> tuple[object, ...]:
    try:
        values = decode(types, raw, strict=True)
        if encode(types, values) != raw:
            raise ValueError("noncanonical v4 result")
    except DecodingError as exc:
        raise ValueError("invalid v4 result ABI") from exc
    return tuple(values)


def _v4_calls() -> list[Call]:
    calls = [
        (MULTICALL3, False, _calldata("getChainId()")),
        (BASE_TIME, False, _calldata("timestampMs()")),
        (STATE_VIEW, False, _calldata("poolManager()")),
        (POSITION_MANAGER, False, _calldata("poolManager()")),
        (L1BLOCK, False, _calldata("operatorFeeScalar()")),
        (L1BLOCK, False, _calldata("operatorFeeConstant()")),
        *((token, False, _calldata("decimals()")) for token in DECIMALS),
    ]
    for key, first_word in V4_KEYS:
        pool_id = bytes.fromhex(pool_id_from_key(key)[2:])
        args = (pool_id,)
        calls.extend(
            [
                (
                    POSITION_MANAGER,
                    True,
                    _calldata("poolKeys(bytes25)", ["bytes25"], (pool_id[:25],)),
                ),
                (STATE_VIEW, False, _calldata("getSlot0(bytes32)", ["bytes32"], args)),
                (STATE_VIEW, False, _calldata("getLiquidity(bytes32)", ["bytes32"], args)),
                *(
                    (
                        STATE_VIEW,
                        False,
                        _calldata(
                            "getTickBitmap(bytes32,int16)", ["bytes32", "int16"], (pool_id, page)
                        ),
                    )
                    for page in range(first_word, first_word + 5)
                ),
            ]
        )
    return calls


def _decode_v4(
    raw: bytes,
    calls: Sequence[Call],
    block: BlockBinding,
) -> tuple[tuple[PoolNode, ...], dict[str, tuple[int, int]], int, int]:
    results = _decode_aggregate3_results(_decode_hex_bytes(_result(raw, 2), "V4"), len(calls))
    if any(not success and not call[1] for call, (success, _) in zip(calls, results, strict=True)):
        raise ValueError("required Base V4 Multicall subcall failed")
    returns = [item[1] for item in results]
    chain, timestamp, state_manager, pos_manager, scalar, constant, *decimals = (
        _words([kind], value)[0]
        for kind, value in zip(
            [
                "uint256",
                "uint256",
                "address",
                "address",
                "uint32",
                "uint64",
                *(["uint8"] * len(DECIMALS)),
            ],
            returns[: 6 + len(DECIMALS)],
            strict=True,
        )
    )
    if (chain, timestamp, state_manager, pos_manager) != (
        CHAIN_ID,
        block.timestamp_ms,
        MANAGER,
        MANAGER,
    ) or decimals != list(DECIMALS.values()):
        raise ValueError("Base V4 identity sentinels disagree")
    bounds = {}
    pools = []
    offset = 6 + len(DECIMALS)
    for expected_key, start_page in V4_KEYS:
        key_response, slot_response, liquidity_response, *pages = returns[offset : offset + 8]
        if results[offset][0]:
            chain_key = _words(KEY_ABI, key_response)
            empty_key = (ZERO_ADDRESS, ZERO_ADDRESS, 0, 0, ZERO_ADDRESS)
            expected = (
                expected_key.currency0,
                expected_key.currency1,
                expected_key.fee,
                expected_key.tick_spacing,
                expected_key.hooks,
            )
            if chain_key not in (expected, empty_key):
                raise ValueError("Base V4 full PoolKey disagrees with its frozen PoolId")
        offset += 8
        pool_id = pool_id_from_key(expected_key)
        price, tick, proto, lp_fee = cast(
            tuple[int, int, int, int],
            _words(["uint160", "int24", "uint24", "uint24"], slot_response),
        )
        try:
            slot = Slot0State(price, tick, proto, lp_fee)
        except FeeEncodingError as exc:
            raise ValueError("Base V4 slot0 fee is invalid") from exc
        active = cast(int, _words(["uint128"], liquidity_response)[0])
        spacing = expected_key.tick_spacing
        if (
            slot.lp_fee != expected_key.fee
            or proto & 0xFFF != proto >> 12
            or not 0 < active < 1 << 127
            or not MIN_TICK <= tick < MAX_TICK
            or not sqrt_price_at_tick(tick) <= price <= sqrt_price_at_tick(tick + 1)
            or not start_page <= tick // spacing // 256 < start_page + 5
        ):
            raise ValueError("Base V4 state differs from fixed active-range model")
        initialized: list[int] = []
        for page, word in enumerate(pages, start_page):
            bitmap = cast(int, _words(["uint256"], word)[0])
            while bitmap:
                low_bit = bitmap & -bitmap
                initialized.append((page * 256 + low_bit.bit_length() - 1) * spacing)
                bitmap ^= low_bit
        lower = max(
            (i for i in initialized if i <= tick),
            default=max(MIN_TICK, start_page * 256 * spacing),
        )
        upper = min(
            (i for i in initialized if i > tick),
            default=min(MAX_TICK, (start_page + 5) * 256 * spacing),
        )
        if not (MIN_TICK <= lower <= tick < upper <= MAX_TICK):
            raise ValueError("Base V4 unseen or inconsistent active tick bounds")
        bounds[pool_id] = (lower, upper)
        pools.append(
            PoolNode(
                chain_id=CHAIN_ID,
                pool_id=pool_id,
                kind=PoolKind.V4,
                token0=expected_key.currency0,
                token1=expected_key.currency1,
                decimals0=DECIMALS.get(expected_key.currency0, 18),
                decimals1=DECIMALS.get(expected_key.currency1, 18),
                fee_pips=calculate_swap_fee(lp_fee, proto >> 12),
                block_number=block.number,
                timestamp_ms=block.timestamp_ms,
                sqrt_price_x96=price,
                tick=tick,
                liquidity=active,
                ticks=(TickLiquidity(lower, active), TickLiquidity(upper, -active)),
            )
        )
    return tuple(pools), bounds, cast(int, scalar), cast(int, constant)


def _acquire(
    tag: str,
    specs: tuple[DiscoveredPool, ...],
    exchange: _Exchange,
    *,
    workers: ThreadPoolExecutor | None = None,
) -> MixedState:
    primary_request = _request("eth_getBlockByNumber", [tag, False])
    primary_raw = exchange("primary_header", primary_request)
    block = _header(primary_raw)
    if tag != "latest" and rpc_quantity(tag) != block.number:
        raise ValueError("mixed Base primary did not return the requested height")
    official = _header(
        exchange(
            "secondary_header",
            _request(
                "eth_getBlockByNumber",
                [hex(block.number), False],
            ),
        )
    )
    if official != block:
        raise ValueError("mixed Base providers disagree on pinned header")
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    code = _decode_hex_bytes(
        _result(exchange("manager_code", _request("eth_getCode", [MANAGER, pin]))),
        "Base V4 PoolManager runtime",
    )
    if f"0x{keccak(code).hex()}" != MANAGER_RUNTIME:
        raise ValueError("Base V4 PoolManager runtime changed")
    calls = _v4_calls()
    v4_request = _request(
        "eth_call",
        [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)}, pin],
        req_id=2,
    )

    def address_transport(_url: str, request: bytes, _timeout: float) -> bytes:
        if request == primary_request:
            return primary_raw
        return exchange("address_state", request)

    def address() -> PinnedUniverseSnapshot:
        return fetch_pinned_universe_snapshot(
            DEFAULT_BASE_RPC_URL,
            specs,
            transport=address_transport,
            block_tag=tag,
            pin_call_to_hash=True,
        )

    if workers is None:
        address_state = address()
        v4_raw = exchange("v4_state", v4_request)
    else:
        address_job = workers.submit(address)
        v4_job = workers.submit(exchange, "v4_state", v4_request)
        # Retain both outcomes. A later failure cannot replace the first observed one.
        failure: BaseException | None = None
        for job in as_completed((address_job, v4_job)):
            failure = failure or job.exception()
        if failure is not None:
            raise failure
        address_state, v4_raw = address_job.result(), v4_job.result()
    v4_pools, bounds, scalar, constant = _decode_v4(v4_raw, calls, block)
    if (
        address_state.block_hash != block.block_hash
        or address_state.parent_hash != block.parent_hash
        or address_state.block_number != block.number
        or address_state.base_time_ms != block.timestamp_ms
        or address_state.base_fee_per_gas_wei != block.base_fee
        or len(address_state.pool_nodes) != ADDRESS_POOLS
        or len({node.pool_id for node in (*address_state.pool_nodes, *v4_pools)})
        != ADDRESS_POOLS + len(V4_KEYS)
    ):
        raise ValueError("mixed Base snapshot is incomplete")
    return MixedState(block, address_state, v4_pools, bounds, scalar, constant)


def _write(path: Path, raw: bytes) -> str:
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return sha256_hex(raw)


class _Poster:
    def __init__(self, source: str, send: Send, timeout: float, stopped: Event) -> None:
        self.source, self.send, self.timeout, self.stopped = source, send, timeout, stopped

    def post(self, req: bytes) -> bytes:
        if self.stopped.is_set():
            raise RpcError("mixed Base scope stopped after a failed RPC")
        return self.send(SOURCES[self.source], req, self.timeout)


class MixedCanonicalConflict(RpcError):
    def __init__(self, before: BlockBinding, after: BlockBinding) -> None:
        super().__init__("mixed Base canonical block conflicts with its prior pin")
        self.before, self.after = before, after


class MixedRpcHttpError(RpcError):
    def __init__(self, message: str, status: int, body: bytes) -> None:
        super().__init__(message)
        self.status, self.body = status, body


class MixedBaseCapture:
    """Separate origin limits and readonly evidence for every complete or failed join."""

    def __init__(
        self,
        directory: Path,
        specs: tuple[DiscoveredPool, ...],
        prior_sha256: str,
        *,
        primary_limit: int,
        secondary_limit: int,
        injected: bool,
    ) -> None:
        directory.mkdir()
        self.directory, self.specs, self.prior_sha256 = directory, specs, prior_sha256
        self.injected = injected
        self.policies = tuple(
            BudgetPolicy(source, cap, 250)
            for source, cap in ((PUBLICNODE, primary_limit), (OFFICIAL, secondary_limit))
        )
        self.ledger = directory / "original-budget.sqlite3"
        with RpcBudget(self.ledger, self.policies):
            pass
        self.attempts = 0
        self.stopped = Event()
        self._stop_lock = Lock()
        self.stop_reason: str | None = None
        self._last_state: tuple[BlockBinding, str] | None = None

    def _stop_rpc(
        self,
        source: str,
        request: bytes,
        raw: bytes | None,
        reason: str,
        *,
        http_status: int | None = None,
    ) -> None:
        with self._stop_lock:
            if self.stop_reason is not None:
                return
            self.stop_reason = reason
            self.stopped.set()
            entry = {
                "schema": "oasis.base-mixed-stop.v1",
                "source": source,
                "url": SOURCES[source],
                "reason": reason,
                "http_status": http_status,
                "attempted_monotonic_ns": time.monotonic_ns(),
                "observed_unix_ns": time.time_ns(),
                "request_sha256": _write(self.directory / "stop.request.json", request),
                "response_sha256": (
                    _write(self.directory / "stop.response.json", raw)
                    if isinstance(raw, bytes)
                    else None
                ),
                "budget_counts": self.counts(),
            }
            _write(self.directory / "STOP.json", canonical_json(entry) + b"\n")

    def budgeted(self, source: str, send: Send) -> Send:
        def admitted(url: str, request: bytes, timeout: float) -> bytes:
            if url != SOURCES[source]:
                raise ValueError("mixed Base RPC origin mismatch")
            if self.stopped.is_set():
                raise RpcError("mixed Base scope stopped after a failed RPC")
            raw: bytes | None = None
            try:
                raw = BudgetedPoster(
                    _Poster(source, send, timeout, self.stopped),
                    self.ledger,
                    self.policies,
                    source,
                    require_existing=True,
                ).post(request)
                req = strict_json_object(request)
                request_id = strict_integer(req.get("id"), "RPC ID")
                envelope = response_envelope(raw, request_id)
                if "error" in envelope:
                    error = strict_object(envelope["error"], "RPC error")
                    code = strict_integer(error["code"], "RPC error code")
                    message = strict_string(error["message"], "RPC error message").lower()
                    business_revert = (
                        req.get("method") == "eth_call"
                        and code in (3, -32000)
                        and (
                            message == "execution reverted"
                            or message.startswith("execution reverted:")
                        )
                    )
                    if not business_revert:
                        self._stop_rpc(
                            source, request, raw, f"json_rpc_error_{code}", http_status=200
                        )
                return raw
            except MixedRpcHttpError as exc:
                self._stop_rpc(
                    source, request, exc.body, f"http_{exc.status}", http_status=exc.status
                )
                raise
            except (OasisError, OSError, KeyError, ValueError, TypeError) as exc:
                self._stop_rpc(source, request, raw, type(exc).__name__)
                raise

        return admitted

    def counts(self) -> dict[str, int]:
        with RpcBudget(self.ledger, self.policies, read_only=True) as budget:
            return budget.counts()

    def capture(
        self,
        tag: str,
        primary: Send,
        official: Send,
        timeout: float,
        workers: ThreadPoolExecutor,
    ) -> MixedCaptureResult:
        self.attempts += 1
        scope = self.directory / f"attempt-{self.attempts:08d}"
        scope.mkdir()
        records: dict[str, dict[str, object]] = {}
        content: dict[str, object] = {
            "schema": SCHEMA,
            "execution_eligible": False,
            "capture_mode": "injected" if self.injected else "public-shadow",
            "roster_parent_sha256": self.prior_sha256,
            "address_roster": [asdict(spec) for spec in self.specs],
            "block_tag": tag,
            "budget_policies": [asdict(policy) for policy in self.policies],
            "budget_before": self.counts(),
            "start_monotonic_ns": time.monotonic_ns(),
            "roles": records,
        }

        def exchange(role: str, request: bytes) -> bytes:
            source = _ROLES[role]
            item: dict[str, object] = {
                "source": source,
                "url": SOURCES[source],
                "request_sha256": _write(scope / f"{role}.request.json", request),
                "release_monotonic_ns": time.monotonic_ns(),
            }
            records[role] = item
            send = primary if source == PUBLICNODE else official
            try:
                reply = send(SOURCES[source], request, timeout)
            except MixedRpcHttpError as exc:
                item["error_type"] = type(exc).__name__
                item["http_status"] = exc.status
                item["received_monotonic_ns"] = time.monotonic_ns()
                item["received_unix_ns"] = time.time_ns()
                item["response_sha256"] = _write(scope / f"{role}.response.json", exc.body)
                raise
            except (OasisError, OSError, KeyError, ValueError, TypeError) as exc:
                item["error_type"] = type(exc).__name__
                raise
            item["received_monotonic_ns"] = time.monotonic_ns()
            item["received_unix_ns"] = time.time_ns()
            item["response_sha256"] = _write(scope / f"{role}.response.json", reply)
            return reply

        manifest_path = scope / "manifest.json"
        try:
            state = _acquire(tag, self.specs, exchange, workers=workers)
            state_bytes = canonical_json(state.evidence())
            digest = sha256_hex(state_bytes)
            if self._last_state is not None:
                previous, _ = self._last_state
                block = state.block
                if (
                    (block.number == previous.number and block.block_hash != previous.block_hash)
                    or (
                        block.number == previous.number + 1
                        and block.parent_hash != previous.block_hash
                    )
                    or (block.number != previous.number and block.block_hash == previous.block_hash)
                ):
                    raise MixedCanonicalConflict(previous, block)
                if block.number < previous.number or (
                    block.number == previous.number and (block, digest) != self._last_state
                ):
                    raise ValueError(
                        "mixed Base repeated or regressed state disagrees with prior pin"
                    )
        except (OSError, ValueError, OasisError, RuntimeError, TypeError) as exc:
            self.stopped.set()
            content.update(
                status="failed", error_type=type(exc).__name__, budget_after=self.counts()
            )
            if isinstance(exc, MixedCanonicalConflict):
                content["canonical_conflict"] = {
                    "previous": asdict(exc.before),
                    "observed": asdict(exc.after),
                }
            _write(manifest_path, canonical_json(content) + b"\n")
            raise
        ready_ns = time.monotonic_ns()
        content.update(
            status="complete", state_ready_monotonic_ns=ready_ns, budget_after=self.counts()
        )
        content["state_sha256"] = _write(scope / "state.json", state_bytes)
        self._last_state = state.block, digest
        digest = _write(manifest_path, canonical_json(content) + b"\n")
        return MixedCaptureResult(state, digest, manifest_path, ready_ns)


def _read(path: Path, digest: object) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESPONSE_BYTES:
        raise ValueError(f"missing or unbounded mixed Base artifact: {path.name}")
    raw = path.read_bytes()
    if sha256_hex(raw) != digest:
        raise ValueError(f"mixed Base artifact digest mismatch: {path.name}")
    return raw


def _check_replay_budget(scope: Path, body: dict[str, object]) -> None:
    ledger_path = scope.parent / "original-budget.sqlite3"
    policies_raw = body.get("budget_policies")
    if not isinstance(policies_raw, list) or len(policies_raw) != 2:
        raise ValueError("mixed Base source policies missing")
    caps = []
    for raw in policies_raw:
        policy = strict_object(raw, "mixed Base source policy")
        source = strict_string(policy.get("source"), "budget source")
        interval = strict_integer(policy.get("min_interval_ms"), "source minimum interval")
        if interval != 250:
            raise ValueError("mixed Base budget minimum interval changed")
        caps.append(
            BudgetPolicy(source, strict_integer(policy.get("max_requests"), "source cap"), interval)
        )
    if {item.source for item in caps} != SOURCES.keys() or ledger_path.is_symlink():
        raise ValueError("mixed Base source policies or original ledger invalid")
    with RpcBudget(ledger_path, tuple(caps), read_only=True) as ledger:
        current = ledger.counts()
    before = strict_object(body.get("budget_before"), "budget before state")
    after = strict_object(body.get("budget_after"), "budget after state")
    for source, step in ((PUBLICNODE, 2), (OFFICIAL, 3)):
        start_use = strict_integer(before.get(source), "scope count before")
        end_use = strict_integer(after.get(source), "scope count after")
        if start_use < 0 or end_use - start_use != step or current[source] < end_use:
            raise ValueError("mixed Base attempt disagrees with original source ledger")


def replay_mixed_snapshot(manifest: Path) -> MixedState:
    if (
        manifest.is_symlink()
        or not manifest.is_file()
        or manifest.stat().st_size > MAX_RESPONSE_BYTES
    ):
        raise ValueError("missing or unbounded mixed Base manifest")
    body = strict_json_object(manifest.read_bytes())
    if (
        body.get("schema") not in (SCHEMA_V1, SCHEMA)
        or body.get("status") != "complete"
        or body.get("execution_eligible") is not False
        or body.get("capture_mode") not in ("injected", "public-shadow")
    ):
        raise ValueError("unsupported or incomplete mixed Base manifest")
    strict_digest(body.get("roster_parent_sha256"), "frozen address manifest digest")
    start = strict_integer(body.get("start_monotonic_ns"), "mixed capture start")
    ready = strict_integer(body.get("state_ready_monotonic_ns"), "mixed state ready")
    rows = strict_object(body.get("roles"), "mixed Base exchanges")
    if rows.keys() != _ROLES.keys():
        raise ValueError("mixed Base missing required exchange")
    specs_raw = body.get("address_roster")
    tag = body.get("block_tag")
    if not isinstance(specs_raw, list) or not isinstance(tag, str):
        raise ValueError("invalid mixed Base roster or block tag")
    specs = address_roster(specs_raw)
    scope = manifest.parent
    if body["schema"] == SCHEMA:
        _check_replay_budget(scope, body)
    timings = {}

    def exchange(role: str, request: bytes) -> bytes:
        row = strict_object(rows[role], "mixed Base exchange")
        if (
            row.get("source") != _ROLES[role]
            or row.get("url") != SOURCES[_ROLES[role]]
            or _read(scope / f"{role}.request.json", row.get("request_sha256")) != request
        ):
            raise ValueError("retained mixed Base request or source differs")
        release = strict_integer(row.get("release_monotonic_ns"), "request release")
        received = strict_integer(row.get("received_monotonic_ns"), "response received")
        utc = strict_integer(row.get("received_unix_ns"), "response Unix time")
        if not 0 < start <= release <= received <= ready or utc <= 0:
            raise ValueError("mixed Base request timing is inconsistent")
        timings[role] = (release, received)
        return _read(scope / f"{role}.response.json", row.get("response_sha256"))

    state = _acquire(tag, specs, exchange)
    if (
        timings["primary_header"][1] > timings["secondary_header"][0]
        or timings["secondary_header"][1] > timings["manager_code"][0]
        or timings["manager_code"][1] > min(timings["address_state"][0], timings["v4_state"][0])
    ):
        raise ValueError("mixed Base state began before the agreed pin and manager identity")
    if _read(scope / "state.json", body.get("state_sha256")) != canonical_json(state.evidence()):
        raise ValueError("retained mixed Base decoded state differs")
    return state
