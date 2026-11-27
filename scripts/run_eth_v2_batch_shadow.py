"""One frozen, anonymous Ethereum V2 batch shadow window and byte replay."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Event

import httpx

from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.rpc_validation import (
    rpc_quantity,
    strict_digest,
    strict_json_object,
    strict_object,
)
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.fastpath.batch_shadow import (
    BatchShadowCash,
    BatchShadowHead,
    BatchShadowPhase,
    BatchShadowScope,
    BatchShadowSource,
    reduce_batch_shadow,
    summarize_batch_shadow,
)
from oasis_agent.fastpath.eth_v2_batch_probe import (
    SHADOW_CALLER,
    SHADOW_PROBE,
    WETH,
    BatchProbeResult,
    build_batch_calldata,
    decode_batch_result,
)
from oasis_agent.fastpath.multicall_universe import (
    _decode_aggregate3_results,
    _encode_aggregate3_calldata,
)
from oasis_agent.fastpath.v2_batch import V2BatchOrder, compare_v2_batch, search_v2_batches
from oasis_agent.fastpath.v2_sizing import V2Leg, V2Pool, V2Route
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope
from scripts import run_eth_v2_wide_shadow as wide
from scripts.capture_public_rpc import durable_new

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "publicnode": "https://ethereum-rpc.publicnode.com",
    "drpc": "https://eth.drpc.org",
}
POLICIES = (BudgetPolicy("publicnode", 24, 250), BudgetPolicy("drpc", 9, 250))
PUBLIC_READS = frozenset(
    {"eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getTransactionCount"}
)
TRACE_READS = PUBLIC_READS | {"debug_traceCall"}
MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"
CAPITAL = 10**19
GAS_LIMIT = 2_400_000
TIP = 50_000_000
FAILURE_GAS_LIMIT = GAS_LIMIT
OPPORTUNITY = 10**12
MIN_NET = 10**12
POLL_COUNT = 7
POLL_SECONDS = 2
WALL_SECONDS = 75
DEADLINE_NS = 4_000_000_000
BODY_CAP = 768 << 10
LEGACY_SCOPE_SCHEMA = "oasis.eth-v2-batch-shadow-window.v1"
SCOPE_SCHEMA = "oasis.eth-v2-batch-shadow-window.v2"
CHILD_CLOCK_BASIS = "post_quorum_publicnode_http_without_independent_child_witness"
CONTROL_SYMBOLS = ("WETH", "USDT", "LINK", "WETH")
CONTROL_POOLS = (
    "0x06da0fd433c1a5d7a4faa01111c044910a184553",
    "0x9db10c305c671153662119d453c4d2c123725566",
    "0xa2107fa5b38d9bbd2c461d6edf11b11a50f6b974",
)
ROSTER_SOURCE_SHA256 = "ec1b0d0de00ba90a97620e590c6c4dd19cd4dbefc57e92a3672f9cf1a8ae563c"


class ShadowStop(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Wire:
    source: str
    request_sha256: str
    response_sha256: str
    sent_ns: int
    received_ns: int
    wall_ms: int
    anonymous_tier: str | None
    body: dict


class ShadowReads:
    def __init__(self, root: Path, *, live: bool) -> None:
        self.root, self.live = root, live
        self.ledger = root / "rpc-budget.sqlite3"
        self.stop = Event()
        self.counts = dict.fromkeys(SOURCES, 0)
        self.deadline = time.monotonic() + WALL_SECONDS
        if live:
            with RpcBudget(self.ledger, POLICIES):
                pass

    def _admit(self) -> None:
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise ShadowStop("scope_stopped_or_expired")

    def _capture(self, source: str, raw: bytes, name: str) -> None:
        self._admit()
        with RpcBudget(self.ledger, POLICIES, require_existing=True) as budget:
            while True:
                now_ms = time.time_ns() // 1_000_000
                if not (wait := budget.reserve(source, now_ms=now_ms, admission=self._admit)):
                    break
                if self.stop.wait(wait / 1000):
                    raise ShadowStop("scope_stopped_or_expired")
        durable_new(self.root / f"{name}.request.json", raw)
        sent_ns = time.monotonic_ns()
        status = 0
        response = bytearray()
        outcome = "ok"
        tier = None
        try:
            self._admit()
            with (
                httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as client,
                client.stream(
                    "POST",
                    SOURCES[source],
                    content=raw,
                    headers={"Content-Type": "application/json"},
                ) as result,
            ):
                status = result.status_code
                if source == "drpc":
                    header = result.headers.get("x-drpc-owner-tier")
                    tier = "free" if header == "free" else "other" if header else None
                for chunk in result.iter_bytes(chunk_size=8192):
                    if len(response) + len(chunk) > BODY_CAP:
                        raise ShadowStop("response_exceeds_bound")
                    if time.monotonic() >= self.deadline:
                        raise ShadowStop("wall_deadline")
                    response.extend(chunk)
        except (httpx.HTTPError, OSError, ShadowStop) as exc:
            outcome = exc.args[0] if isinstance(exc, ShadowStop) else type(exc).__name__
        finally:
            received_ns = time.monotonic_ns()
            wall_ms = time.time_ns() // 1_000_000
            durable_new(self.root / f"{name}.response.json", bytes(response))
            durable_new(
                self.root / f"{name}.meta.json",
                canonical_json(
                    {
                        "source": source,
                        "http_status": status,
                        "outcome": outcome,
                        "reserved_ms": now_ms,
                        "sent_ns": sent_ns,
                        "received_ns": received_ns,
                        "anonymous_tier": tier,
                        "wall_ms": wall_ms,
                        "request_sha256": sha256_hex(raw),
                        "response_sha256": sha256_hex(bytes(response)),
                    }
                ),
            )

    def read(self, source: str, method: str, params: list, *, allow_revert: bool = False) -> Wire:
        allowed = TRACE_READS if source == "drpc" else PUBLIC_READS
        if source not in SOURCES or method not in allowed:
            raise ShadowStop("method_outside_read_scope")
        if self.live:
            self._admit()
        index = self.counts[source] + 1
        name = f"{source}-{index:02d}"
        raw = canonical_json({"jsonrpc": "2.0", "id": index, "method": method, "params": params})
        try:
            if self.live:
                self._capture(source, raw, name)
            meta = strict_json_object((self.root / f"{name}.meta.json").read_bytes())
            self.counts[source] = index
            response = (self.root / f"{name}.response.json").read_bytes()
            if (
                raw != (self.root / f"{name}.request.json").read_bytes()
                or meta["source"] != source
                or meta["request_sha256"] != sha256_hex(raw)
                or meta["response_sha256"] != sha256_hex(response)
                or not 0 < meta["sent_ns"] <= meta["received_ns"]
            ):
                raise ShadowStop("retained_exchange_mismatch")
            if meta["outcome"] != "ok" or meta["http_status"] != 200:
                raise ShadowStop(f"{source}:{method}:HTTP_{meta['http_status']}:{meta['outcome']}")
            body = response_envelope(response, index)
            if "error" in body:
                error = strict_object(body["error"], "rpc error")
                message = error.get("message")
                evm_revert = (
                    allow_revert
                    and error.get("code") in (3, -32000)
                    and isinstance(message, str)
                    and (
                        message == "execution reverted" or message.startswith("execution reverted:")
                    )
                )
                if not evm_revert:
                    raise ShadowStop(f"{source}:{method}:RPC_{error.get('code')}")
            return Wire(
                source,
                meta["request_sha256"],
                meta["response_sha256"],
                meta["sent_ns"],
                meta["received_ns"],
                meta["wall_ms"],
                meta["anonymous_tier"],
                dict(body),
            )
        except OSError, ValueError, TypeError, KeyError:
            self.stop.set()
            raise

    def pair(self, left: tuple, right: tuple) -> tuple[Wire, Wire]:
        if not self.live:
            results = []
            errors = []
            for source, task in zip(SOURCES, (left, right), strict=True):
                try:
                    results.append(self.read(source, task[0], task[1], allow_revert=task[2]))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append(exc)
            if errors:
                raise next((e for e in errors if isinstance(e, ShadowStop)), errors[0])
            return results[0], results[1]
        with ThreadPoolExecutor(max_workers=2) as workers:
            jobs = [
                workers.submit(self.read, source, task[0], task[1], allow_revert=task[2])
                for source, task in zip(SOURCES, (left, right), strict=True)
            ]
            left_result, right_result = [job.exception() for job in jobs]
            errors = [error for error in (left_result, right_result) if error is not None]
            if errors:
                raise next((e for e in errors if str(e) != "scope_stopped_or_expired"), errors[0])
            return jobs[0].result(), jobs[1].result()

    def wait_between_heads(self) -> None:
        if self.live:
            self._admit()
            time.sleep(POLL_SECONDS)


@dataclass(frozen=True, slots=True)
class Head:
    height: int
    block_hash: str
    parent_hash: str
    base_fee: int
    sent_ns: int
    observed_ns: int

    @property
    def pin(self) -> dict:
        return {"blockHash": self.block_hash, "requireCanonical": True}


def head_from(wire: Wire) -> Head:
    raw = strict_object(wire.body.get("result"), "Ethereum head")
    number = rpc_quantity(raw["number"])
    snapshot, _ = prague_header(raw, number, raw["hash"])
    if not 0 <= wire.wall_ms - snapshot.ref.timestamp * 1000 <= 40_000:
        raise ShadowStop("stale_or_future_Ethereum_head")
    return Head(
        number,
        snapshot.ref.block_hash,
        raw["parentHash"],
        rpc_quantity(raw["baseFeePerGas"]),
        wire.sent_ns,
        wire.received_ns,
    )


def latest(reads: ShadowReads) -> Head:
    return head_from(reads.read("publicnode", "eth_getBlockByNumber", ["latest", False]))


def observe_child(reads: ShadowReads, parent: Head, seen: Head | None = None) -> Head:
    for _ in range(POLL_COUNT):
        if seen is None or seen.height == parent.height:
            if seen is not None and seen.block_hash != parent.block_hash:
                raise ShadowStop("same_height_reorg")
            reads.wait_between_heads()
            seen = latest(reads)
        if seen.height > parent.height + 1:
            seen = head_from(
                reads.read("publicnode", "eth_getBlockByNumber", [hex(parent.height + 1), False])
            )
        if seen.height == parent.height + 1 and seen.parent_hash == parent.block_hash:
            return seen
        if seen.height != parent.height:
            raise ShadowStop("child_ancestry_conflict")
    raise ShadowStop("direct_child_poll_bound")


def snapshot(reads: ShadowReads, head: Head, roster: list) -> tuple[list, int]:
    reserve_data = bytes.fromhex("0902f1ac")
    call = {
        "to": MULTICALL,
        "data": _encode_aggregate3_calldata(
            [(entry["address"], False, reserve_data) for entry in roster]
        ),
    }
    task = ("eth_call", [call, head.pin], False)
    left, right = reads.pair(task, task)
    a, b = data_bytes(left.body["result"]), data_bytes(right.body["result"])
    if a != b:
        raise ShadowStop("dual_source_pool_state_mismatch")
    active = []
    for entry, (success, raw) in zip(
        roster, _decode_aggregate3_results(a, len(roster)), strict=True
    ):
        if not success or len(raw) != 96:
            raise ShadowStop("pool_state_missing")
        values = tuple(int.from_bytes(raw[i : i + 32]) for i in (0, 32, 64))
        if max(values[:2]) >= 1 << 112 or values[2] >= 1 << 32:
            raise ShadowStop("pool_state_noncanonical")
        if all(values[:2]):
            active.append({**entry, "reserves": values[:2]})
    return active, max(left.received_ns, right.received_ns)


def order_for(item: dict, by_pool: dict, *, name: str, leg_cost: int) -> V2BatchOrder:
    path = item["pools"]
    legs = []
    for i, address in enumerate(path):
        pool = by_pool[address]
        a, b = [wide.ASSETS[symbol][0] for symbol in pool["assets"]]
        legs.append(
            V2Leg(
                V2Pool(address, a, b, *pool["reserves"], 997, 1000),
                wide.ASSETS[item["assets"][i]][0],
            )
        )
    return V2BatchOrder(name, V2Route(tuple(legs)), item["input_wei"], leg_cost * len(legs), 0)


def rank_batches(active: list, base_fee: int) -> tuple[dict, tuple[V2BatchOrder, ...]]:
    floor, budget = wide.costs(base_fee)
    grid = wide.search(active, floor, budget)
    by_pool = {entry["address"]: entry for entry in active}
    routes = {}
    for entry in grid["top_routes"]:
        for row in (
            entry,
            {**entry, "pools": entry["pools"][::-1], "assets": entry["assets"][::-1]},
        ):
            routes.setdefault((tuple(row["pools"]), row["input_wei"]), row)
    # Discovery is a conservative cost scenario. The exact call obtains its own whole gas.
    fee = (base_fee * 9 + 7) // 8 + TIP
    fixed = (21_000 + FAILURE_GAS_LIMIT) * fee + OPPORTUNITY
    menu = tuple(
        order_for(row, by_pool, name=f"route-{i:02d}", leg_cost=75_000 * fee)
        for i, row in enumerate(routes.values())
    )
    search = search_v2_batches(
        menu,
        initial_capital=CAPITAL,
        batch_fixed_cost=fixed,
        anchor_token=WETH,
        max_batch_orders=3,
        beam_width=64,
        max_leg_evaluations=6_000,
    )
    eligible = tuple(row for row in search.shortlist if row.net_in_anchor >= MIN_NET)
    return (
        {
            "route_grid_count": grid["route_grid_count"],
            "grid_counts": grid["counts"],
            "menu_count": len(menu),
            "menu_orders": [
                {
                    "id": order.order_id,
                    "pools": [leg.pool.pool_id for leg in order.route.legs],
                    "input_wei": order.amount_in,
                }
                for order in menu
            ],
            "search_legs": search.search_leg_evaluations,
            "extensions": search.extensions_evaluated,
            "complete": search.complete,
            "best_model_net_wei": search.best_net,
            "eligible_shortlist": [asdict(row) for row in eligible],
        },
        tuple({order.order_id: order for order in menu}[key] for key in eligible[0].order_ids)
        if eligible
        else (),
    )


def control_orders(active: list) -> tuple[V2BatchOrder, ...]:
    by_pool = {entry["address"]: entry for entry in active}
    order = {"pools": CONTROL_POOLS, "assets": CONTROL_SYMBOLS, "input_wei": 10**14}
    # Both rounds intentionally use the same pools. The second must see the mutated reserves.
    return tuple(order_for(order, by_pool, name=f"control-{i}", leg_cost=0) for i in (1, 2))


def envelope(
    orders: tuple[V2BatchOrder, ...], active: list, head: Head, runtime: bytes, nonce: int
) -> dict:
    venues = {entry["address"]: entry["venue"] for entry in active}
    paths = []
    for order in orders:
        legs = order.route.legs
        pools = tuple(leg.pool.pool_id for leg in legs)
        assets = (legs[0].token_in, *(leg.token_out for leg in legs))
        mask = sum(1 << i for i, address in enumerate(pools) if venues[address] == "sushi")
        paths.append((assets, pools, mask, order.amount_in))
    fee_cap = head.base_fee
    for _ in range(3):
        fee_cap = (9 * fee_cap + 7) // 8
    value = CAPITAL if len(orders) and not orders[0].order_id.startswith("control-") else 10**15
    encoded = build_batch_calldata(paths)
    return {
        "call": {
            "from": SHADOW_CALLER,
            "to": SHADOW_PROBE,
            "chainId": "0x1",
            "type": "0x2",
            "nonce": hex(nonce),
            "accessList": [],
            "gas": hex(GAS_LIMIT),
            "maxFeePerGas": hex(fee_cap + TIP),
            "maxPriorityFeePerGas": hex(TIP),
            "value": hex(value),
            "data": "0x" + encoded.hex() if isinstance(encoded, bytes) else encoded,
        },
        "override": {
            SHADOW_CALLER: {"balance": hex(100 * 10**18), "code": "0x"},
            SHADOW_PROBE: {"balance": "0x0", "code": "0x" + runtime.hex()},
        },
    }


def match_cash(decoded: BatchProbeResult, orders: tuple[V2BatchOrder, ...], capital: int) -> int:
    expected_assets = tuple(
        dict.fromkeys(
            token
            for order in orders
            for leg in order.route.legs
            for token in (leg.token_in, leg.token_out)
        )
    )
    if (
        decoded.assets != expected_assets
        or decoded.start_balances[0] != capital
        or len(decoded.route_amounts) != len(orders)
        or any(
            len(amounts) != len(order.route.legs) + 1 or amounts[0] != order.amount_in
            for amounts, order in zip(decoded.route_amounts, orders, strict=True)
        )
        or decoded.function_body_gas >= GAS_LIMIT
    ):
        raise ShadowStop("ordered_batch_cash_or_menu_mismatch")
    return decoded.end_balances[0] - capital


def simulation(
    reads: ShadowReads, head: Head, payload: dict, orders: tuple[V2BatchOrder, ...], result: dict
) -> None:
    call, override = payload["call"], payload["override"]
    if head.base_fee > rpc_quantity(call["maxFeePerGas"]):
        raise ShadowStop("frozen_fee_cap_below_state_base_fee")
    task = ("eth_call", [call, head.pin, override], True)
    primary, secondary = reads.pair(task, task)
    price = min(rpc_quantity(call["maxFeePerGas"]), head.base_fee + TIP)
    result.update(
        {
            "head": asdict(head),
            "primary": {k: v for k, v in asdict(primary).items() if k != "body"},
            "secondary": {k: v for k, v in asdict(secondary).items() if k != "body"},
            "effective_gas_price_wei": price,
            "execution_fee_upper_wei": GAS_LIMIT * price,
            "l1_data_fee_wei": 0,
            "operator_fee_wei": 0,
            "additional_inclusion_wei": 0,
            "financing_wei": 0,
            "failure_reserve_wei": FAILURE_GAS_LIMIT * price,
            "opportunity_reserve_wei": OPPORTUNITY,
        }
    )
    if "error" in primary.body or "error" in secondary.body:
        if "error" not in primary.body or "error" not in secondary.body:
            raise ShadowStop("dual_source_simulation_status_mismatch")
        if data_bytes(primary.body["error"]["data"]) != data_bytes(secondary.body["error"]["data"]):
            raise ShadowStop("dual_source_revert_data_mismatch")
        result.update({"status": "reverted", "gross_weth_wei": 0, "modeled_full_net_wei": None})
        return
    raw = data_bytes(primary.body["result"])
    if raw != data_bytes(secondary.body["result"]):
        raise ShadowStop("dual_source_whole_output_mismatch")
    decoded = decode_batch_result("0x" + raw.hex())
    realized = match_cash(decoded, orders, rpc_quantity(call["value"]))
    result.update(
        {
            "status": "succeeded",
            "decoded": asdict(decoded),
            "gross_weth_wei": realized,
            "raw_output_sha256": sha256_hex(raw),
            "modeled_full_net_wei_lower": realized
            - result["execution_fee_upper_wei"]
            - result["failure_reserve_wei"]
            - OPPORTUNITY,
        }
    )


def trace_fee(reads: ShadowReads, head: Head, payload: dict, result: dict) -> dict:
    call, override = payload["call"], payload["override"]
    wire = reads.read(
        "drpc",
        "debug_traceCall",
        [
            call,
            head.pin,
            {
                "tracer": "callTracer",
                "timeout": "4s",
                "tracerConfig": {"onlyTopCall": True},
                "stateOverrides": override,
            },
        ],
    )
    if wire.anonymous_tier != "free":
        raise ShadowStop("trace_not_confirmed_anonymous_free")
    trace = strict_object(wire.body["result"], "ordered batch callTracer")
    if (
        any(str(trace.get(k)).lower() != call[k].lower() for k in ("from", "to", "value", "gas"))
        or trace.get("input") != call["data"]
    ):
        raise ShadowStop("trace_envelope_mismatch")
    charged_gas = rpc_quantity(trace["gasUsed"])
    if not 21_000 <= charged_gas <= GAS_LIMIT:
        raise ShadowStop("whole_gas_out_of_range")
    if (result["status"] == "reverted") != (trace.get("error") == "execution reverted"):
        raise ShadowStop("trace_status_disagrees_with_dual_call")
    if (
        result["status"] == "succeeded"
        and sha256_hex(data_bytes(trace["output"])) != result["raw_output_sha256"]
    ):
        raise ShadowStop("trace_output_disagrees_with_dual_call")
    fee = charged_gas * result["effective_gas_price_wei"]
    network_net = result["gross_weth_wei"] - fee
    return {
        "source": "drpc",
        "request_sha256": wire.request_sha256,
        "response_sha256": wire.response_sha256,
        "gas_used": charged_gas,
        "execution_fee_wei": fee,
        "network_fee_net_wei": network_net,
        "policy_net_wei": network_net - result["failure_reserve_wei"] - OPPORTUNITY,
    }


def reduce_outcomes(report: dict, discovered: Head, first_return: Head, child: Head) -> None:
    scope = BatchShadowScope(
        chain_id=1,
        plan_sha256=report["scope_sha256"],
        envelope_sha256=report["envelope_sha256"],
        settlement_asset=WETH,
        decimals=18,
        primary_source="publicnode",
        secondary_source="drpc",
        deadline_ns=DEADLINE_NS,
        minimum_net_base_units=MIN_NET,
    )

    def phase(label: str, head: Head) -> BatchShadowPhase:
        observed = BatchShadowHead(
            height=head.height,
            block_hash=head.block_hash,
            parent_hash=head.parent_hash,
            observed_ns=head.observed_ns,
        )
        row = report["phases"][label]
        batch = ()
        if "reused_phase" not in row:
            cash = BatchShadowCash(
                settlement_delta=row["gross_weth_wei"],
                gas=row["execution_fee_upper_wei"],
                l1_data=0,
                operator=0,
                inclusion=0,
                financing=0,
                failure_reserve=row["failure_reserve_wei"],
                opportunity=OPPORTUNITY,
                residual=0,
            )
            batch = tuple(
                BatchShadowSource(
                    source=wire["source"],
                    chain_id=1,
                    plan_sha256=scope.plan_sha256,
                    envelope_sha256=scope.envelope_sha256,
                    block_number=head.height,
                    block_hash=head.block_hash,
                    started_ns=wire["sent_ns"],
                    returned_ns=wire["received_ns"],
                    request_sha256=wire["request_sha256"],
                    response_sha256=wire["response_sha256"],
                    status=row["status"],
                    cash=cash,
                )
                for wire in (row["primary"], row["secondary"])
            )
        return BatchShadowPhase(
            head=observed, results=batch, witness_started_ns=head.sent_ns if label == "K" else None
        )

    outcome = reduce_batch_shadow(
        scope,
        kind=report["candidate_kind"],
        discovery=phase("H", discovered),
        first_return=phase("K", first_return),
        child=phase("C", child),
        independent_child_clock=False,
    )
    report["fee_basis"] = "dual_whole_output_with_frozen_gas_limit_upper_bound"
    report["child_clock_basis"] = CHILD_CLOCK_BASIS
    quorum = outcome.discovery.quorum_return_ns
    report["publicnode_post_quorum_latest_was_discovery"] = (
        first_return.block_hash == discovered.block_hash
        if quorum is not None and quorum <= first_return.sent_ns <= first_return.observed_ns
        else None
    )
    report["normalized_outcome"] = outcome.model_dump(mode="json")
    report["summary"] = summarize_batch_shadow((outcome,))


def run_window(reads: ShadowReads, roster: list, runtime: bytes, report: dict) -> None:
    chains = reads.pair(("eth_chainId", [], False), ("eth_chainId", [], False))
    if any(wire.body.get("result") != "0x1" for wire in chains):
        raise ShadowStop("chain_mismatch")
    anchor = latest(reads)
    report["anchor"] = asdict(anchor)
    discovered = observe_child(reads, anchor)
    report["discovery_head"] = asdict(discovered)
    active, state_ready = snapshot(reads, discovered, roster)
    scan, selected = rank_batches(active, discovered.base_fee)
    report.update(
        {
            "active_pools": len(active),
            "state_ready_ns": state_ready,
            "ranking": scan,
            "ranking_finished_ns": time.monotonic_ns() if reads.live else None,
        }
    )
    orders = selected or control_orders(active)
    report["candidate_kind"] = "natural_forward" if selected else "fixed_control"
    nonce = rpc_quantity(
        reads.read("publicnode", "eth_getTransactionCount", [SHADOW_CALLER, discovered.pin]).body[
            "result"
        ]
    )
    payload = envelope(orders, active, discovered, runtime, nonce)
    report["envelope"] = payload
    report["envelope_sha256"] = sha256_hex(canonical_json(payload))
    phases = report["phases"]
    phases["H"] = {}
    simulation(reads, discovered, payload, orders, phases["H"])
    if phases["H"]["status"] == "succeeded":
        model = compare_v2_batch(
            (
                replace(
                    order,
                    success_cost_in_anchor=0,
                    failure_cost_in_anchor=0,
                    min_net_in_anchor=-CAPITAL,
                )
                for order in orders
            ),
            initial_capital=rpc_quantity(payload["call"]["value"]),
            anchor_token=WETH,
        ).ordered
        if (
            model.success_count != len(orders)
            or tuple(entry.quote.amounts for entry in model.results if entry.quote is not None)
            != phases["H"]["decoded"]["route_amounts"]
        ):
            raise ShadowStop("ordered_H_integer_quote_mismatch")
    first_return = latest(reads)
    report["first_return_head"] = asdict(first_return)
    if first_return.height < discovered.height or (
        first_return.height == discovered.height
        and first_return.block_hash != discovered.block_hash
    ):
        raise ShadowStop("return_head_reorg_or_regression")
    phases["K"] = {}
    if first_return.block_hash == discovered.block_hash:
        phases["K"] = {**phases["H"], "reused_phase": "H"}
    else:
        require_nonce(reads, first_return, nonce)
        simulation(reads, first_return, payload, orders, phases["K"])
    child = observe_child(reads, discovered, first_return)
    report["direct_child_head"] = asdict(child)
    phases["C"] = {}
    if child.block_hash == first_return.block_hash:
        phases["C"] = phases["K"]
        phases["K"] = {**phases["C"], "reused_phase": "C"}
    else:
        require_nonce(reads, child, nonce)
        simulation(reads, child, payload, orders, phases["C"])
    reduce_outcomes(report, discovered, first_return, child)
    report["trace_fee_audit"] = {}
    if chains[1].anonymous_tier == "free":
        for label, head in (("H", discovered), ("K", first_return), ("C", child)):
            if "reused_phase" not in phases[label]:
                report["trace_fee_audit"][label] = trace_fee(reads, head, payload, phases[label])


def require_nonce(reads: ShadowReads, head: Head, expected: int) -> None:
    wire = reads.read("publicnode", "eth_getTransactionCount", [SHADOW_CALLER, head.pin])
    if rpc_quantity(wire.body["result"]) != expected:
        raise ShadowStop("frozen_research_nonce_changed")


def freeze(root: Path, roster_path: Path, artifact: Path) -> None:
    from_roster = roster_path.read_bytes()
    if sha256_hex(from_roster) != ROSTER_SOURCE_SHA256:
        raise ShadowStop("roster_source_commitment_mismatch")
    if (
        artifact.resolve()
        != ROOT / "contracts/out/EthV2OrderedBatchProbe.sol/EthV2OrderedBatchProbe.json"
    ):
        raise ShadowStop("artifact_must_be_local_ordered_batch_probe_build")
    roster = json.loads(from_roster)["pools"]
    runtime = data_bytes(json.loads(artifact.read_bytes())["deployedBytecode"]["object"])
    if len(roster) != 45 or not 1 <= len(runtime) <= 24_576:
        raise ShadowStop("unreviewed_roster_or_runtime_size")
    root.mkdir(exist_ok=False)
    durable_new(root / "runtime.bin", runtime)
    durable_new(root / "roster.json", canonical_json(roster))
    plan = plan_for(root, locked_ms=time.time_ns() // 1_000_000)
    durable_new(root / "scope.json", canonical_json(plan))


def plan_for(root: Path, *, locked_ms: int, legacy_strategy_sha256: str | None = None) -> dict:
    plan = {
        "schema": LEGACY_SCOPE_SCHEMA if legacy_strategy_sha256 is not None else SCOPE_SCHEMA,
        "locked_ms": locked_ms,
        "chain_id": 1,
        "sources": SOURCES,
        "source_limits": [asdict(policy) for policy in POLICIES],
        "max_usd": 0,
        "wall_seconds": WALL_SECONDS,
        "poll_seconds": POLL_SECONDS,
        "max_polls": POLL_COUNT,
        "target": "first direct child of a post-freeze freshest head",
        "fixed_control": CONTROL_POOLS,
        "fixed_control_hops": CONTROL_SYMBOLS,
        "fixed_control_wei": 10**14,
        "capital_wei": CAPITAL,
        "gas_limit": GAS_LIMIT,
        "priority_fee_wei": TIP,
        "failure_gas_limit": FAILURE_GAS_LIMIT,
        "opportunity_reserve_wei": OPPORTUNITY,
        "min_net_wei": MIN_NET,
        "grid": wide.GRID,
        "max_batch_routes": 3,
        "beam": 64,
        "search_legs": 6000,
        "candidate_rule": "best qualified batch or precommitted control",
        "deadline_ns": DEADLINE_NS,
        "trace_policy": "anonymous free tier only, after H/K/C; stop at first refusal",
        "roster_source_sha256": ROSTER_SOURCE_SHA256,
        "runtime_sha256": sha256_hex((root / "runtime.bin").read_bytes()),
        "roster_sha256": sha256_hex((root / "roster.json").read_bytes()),
        "strategy_sha256": legacy_strategy_sha256 or sha256_hex(Path(__file__).read_bytes()),
        "wide_grid_sha256": sha256_hex(Path(wide.__file__).read_bytes()),
        "kernel_sha256": sha256_hex((ROOT / "src/oasis_agent/fastpath/v2_batch.py").read_bytes()),
        "execution_eligible": False,
    }
    if legacy_strategy_sha256 is None:
        reducer = ROOT / "src/oasis_agent/fastpath/batch_shadow.py"
        plan["batch_shadow_reducer_sha256"] = sha256_hex(reducer.read_bytes())
        plan["child_clock_basis"] = CHILD_CLOCK_BASIS
    return plan


def legacy_control_replay(report: dict) -> dict:
    """Project a verified v2 outcome onto the completed v1 control's wire format."""
    outcome = report.get("normalized_outcome")
    if (
        report.get("candidate_kind") != "fixed_control"
        or not isinstance(outcome, dict)
        or outcome["timely_h_k_c_survivor"] is not False
        or outcome["quorum_before_direct_child"] is not None
    ):
        raise ShadowStop("legacy_replay_requires_completed_negative_control")
    quorum = outcome["discovery"]["quorum_return_ns"]
    child = outcome["child"]["head"]
    if not isinstance(quorum, int) or not isinstance(child, dict):
        raise ShadowStop("legacy_replay_missing_control_clocks")
    legacy = report.copy()
    legacy.pop("publicnode_post_quorum_latest_was_discovery")
    legacy["child_clock_basis"] = "observed_publicnode_http_head_not_stream"
    # The old value records only ordering of two local observations. It is never
    # consumed by the v2 reducer or any natural timing denominator.
    legacy["normalized_outcome"] = {
        **outcome,
        "quorum_before_direct_child": quorum < child["observed_ns"],
    }
    return legacy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "capture", "replay"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--roster-source", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--scope-sha256")
    args = parser.parse_args()
    root = args.root
    if args.mode == "freeze":
        if not args.roster_source or not args.artifact:
            parser.error("freeze requires --roster-source and --artifact")
        freeze(root, args.roster_source, args.artifact)
        print(sha256_hex((root / "scope.json").read_bytes()))
        return 0
    live = args.mode == "capture"
    plan_raw = (root / "scope.json").read_bytes()
    plan = strict_json_object(plan_raw)
    legacy = plan["schema"] == LEGACY_SCOPE_SCHEMA
    if legacy and live:
        raise ShadowStop("legacy_scope_is_replay_only")
    if (live or legacy or args.scope_sha256 is not None) and args.scope_sha256 != sha256_hex(
        plan_raw
    ):
        parser.error("capture and legacy replay require the precommitted --scope-sha256")
    legacy_hash = (
        strict_digest(plan["strategy_sha256"], "legacy strategy SHA-256") if legacy else None
    )
    if (
        canonical_json(
            plan_for(root, locked_ms=plan["locked_ms"], legacy_strategy_sha256=legacy_hash)
        )
        != plan_raw
    ):
        raise ShadowStop("preregistration_or_inputs_changed")
    if live:
        now = time.time_ns() // 1_000_000
        if not 0 < now - plan["locked_ms"] <= 300_000:
            raise ShadowStop("preregistration_not_current")
        durable_new(
            root / "ACTIVE",
            canonical_json({"scope_sha256": sha256_hex(plan_raw), "started_ms": now}),
        )
    roster = json.loads((root / "roster.json").read_bytes())
    runtime = (root / "runtime.bin").read_bytes()
    reads = ShadowReads(root, live=live)
    report = {
        "scope_sha256": sha256_hex(plan_raw),
        "phases": {},
        "execution_eligible": False,
        "release_permitted": False,
        "stop_reason": None,
    }
    previous = None if live else strict_json_object((root / "result.json").read_bytes())
    try:
        run_window(reads, roster, runtime, report)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        report["stop_reason"] = str(exc) if isinstance(exc, ShadowStop) else type(exc).__name__
    with RpcBudget(reads.ledger, POLICIES, read_only=True) as budget:
        report["rpc_counts"] = budget.counts()
    if report["rpc_counts"] != reads.counts:
        raise ShadowStop("unreconciled_rpc_reservations")
    if previous is not None and "ranking" in report:
        report["ranking_finished_ns"] = previous["ranking_finished_ns"]
    body = canonical_json(legacy_control_replay(report) if legacy else report)
    if live:
        durable_new(root / "result.json", body)
        durable_new(
            root / "STOP.json",
            canonical_json(
                {
                    "reason": report["stop_reason"] or "window_complete",
                    "rpc_counts": report["rpc_counts"],
                    "result_sha256": sha256_hex(body),
                }
            ),
        )
    elif body != (root / "result.json").read_bytes():
        raise ShadowStop("offline_replay_differs")
    print(
        canonical_json(
            {
                "stop_reason": report["stop_reason"],
                "rpc_counts": report["rpc_counts"],
                "result_sha256": sha256_hex(body),
            }
        ).decode()
    )
    return 3 if report["stop_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
