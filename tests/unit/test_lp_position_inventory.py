"""Decision-block position inventory as two-provider agreed evidence.

The fixtures below are synthetic wire exchanges that reconstruct a canonical v4
PositionManager/StateView read. They assert mechanism, not live profitability: the
amounts are cross-checked against the Uniswap integer formulas written out here, so
the suite fails if the rounding or the fee inclusion changes.
"""

import json
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_position_inventory import (
    DERIVED_INVENTORY_KIND,
    INVENTORY_SCHEMA,
    agreed_position_inventory,
)
from oasis_agent.farming.position_capture import (
    PositionSnapshotCapture,
    persist_position_snapshot_evidence,
)
from oasis_agent.farming.positions import Q128, PositionSnapshot, position_id
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import canonical_json
from oasis_agent.models.position_scope import LpPositionScope
from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.shadow.features import agreed_value_payload
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.unit.test_live_shadow_producer import CHAIN, blocks
from tests.unit.test_lp_cycle_assembly import (
    DECISION_BLOCK,
    RETRIEVED_AT,
    assemble,
    declaration,
    evidence_rows,
    market,
)
from tests.unit.test_lp_features import Q96
from tests.unit.test_market_window import POOL, POOL_ID, VIEW

HEADERS = blocks(100, 3)
MANAGER = "0x" + "4" * 40
OWNER = "0x" + "5" * 40
TOKEN_ID = 7
LIQUIDITY = 5_000_000
TICK_LOWER, TICK_UPPER = -120, 120
Q192 = 1 << 192
SELECTORS = {
    name: "0x" + keccak(name)[:4].hex()
    for name in (
        b"ownerOf(uint256)",
        b"getPoolAndPositionInfo(uint256)",
        b"getPositionLiquidity(uint256)",
        b"getPositionInfo(bytes32,bytes32)",
        b"getFeeGrowthInside(bytes32,int24,int24)",
    )
}


def scope(**overrides: object) -> LpPositionScope:
    values: dict[str, object] = {
        "chain_id": CHAIN,
        "position_manager": MANAGER,
        "token_id": TOKEN_ID,
        "pool_id": POOL_ID,
        "owner": OWNER,
    }
    values.update(overrides)
    return LpPositionScope(**values)  # type: ignore[arg-type]


def snapshot(
    *,
    decision: BlockSnapshot = HEADERS[2],
    liquidity: int = LIQUIDITY,
    pool: object = POOL,
    pool_id: str = POOL_ID,
) -> PositionSnapshot:
    """The position the wire fixture reconstructs; growth makes fee0 equal liquidity."""
    return PositionSnapshot(
        decision.ref,
        MANAGER,
        TOKEN_ID,
        OWNER,
        MANAGER,
        pool,  # type: ignore[arg-type]
        pool_id,
        position_id(MANAGER, TICK_LOWER, TICK_UPPER, TOKEN_ID),
        TICK_LOWER,
        TICK_UPPER,
        False,
        liquidity,
        0,
        0,
        Q128,
        0,
        liquidity,
        0,
    )


def _envelope(exchange_id: int, method: str, params: list[object], result: object) -> RpcExchange:
    return RpcExchange(
        method,
        exchange_id,
        canonical_json({"jsonrpc": "2.0", "id": exchange_id, "method": method, "params": params}),
        canonical_json({"jsonrpc": "2.0", "id": exchange_id, "result": result}),
        1,
    )


def _call(
    exchange_id: int,
    to: str,
    signature: bytes,
    args: bytes,
    result: bytes,
    *,
    decision: BlockSnapshot,
) -> RpcExchange:
    return _envelope(
        exchange_id,
        "eth_call",
        [
            {"to": to, "data": SELECTORS[signature] + args.hex()},
            {"blockHash": decision.ref.block_hash, "requireCanonical": True},
        ],
        "0x" + result.hex(),
    )


def _header(decision: BlockSnapshot) -> dict[str, object]:
    return {
        "number": hex(decision.ref.number),
        "hash": decision.ref.block_hash,
        "parentHash": decision.parent_hash,
        "timestamp": hex(decision.ref.timestamp),
    }


def capture(source: str, value: PositionSnapshot, *, decision: BlockSnapshot = HEADERS[2]):
    """A capture whose retained exchanges reconstruct ``value`` exactly."""
    key = value.pool_key
    packed = (
        (int(value.pool_id[:52], 16) << 56)
        | ((value.tick_lower & 0xFFFFFF) << 8)
        | ((value.tick_upper & 0xFFFFFF) << 32)
    )
    token = encode(["uint256"], [value.token_id])
    core = encode(
        ["bytes32", "bytes32"],
        [bytes.fromhex(value.pool_id[2:]), bytes.fromhex(value.core_position_id[2:])],
    )
    growth = encode(
        ["bytes32", "int24", "int24"],
        [bytes.fromhex(value.pool_id[2:]), value.tick_lower, value.tick_upper],
    )
    exchanges = (
        _envelope(1, "eth_chainId", [], hex(value.block.chain_id)),
        _envelope(2, "eth_getBlockByNumber", [hex(value.block.number), False], _header(decision)),
        _call(
            3,
            value.position_manager,
            b"ownerOf(uint256)",
            token,
            encode(["address"], [value.nft_owner]),
            decision=decision,
        ),
        _call(
            4,
            value.position_manager,
            b"getPoolAndPositionInfo(uint256)",
            token,
            encode(
                ["(address,address,uint24,int24,address)", "uint256"],
                [(key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks), packed],
            ),
            decision=decision,
        ),
        _call(
            5,
            value.position_manager,
            b"getPositionLiquidity(uint256)",
            token,
            encode(["uint128"], [value.liquidity]),
            decision=decision,
        ),
        _call(
            6,
            VIEW,
            b"getPositionInfo(bytes32,bytes32)",
            core,
            encode(
                ["uint128", "uint256", "uint256"],
                [value.liquidity, value.fee_growth_inside0_last_x128, 0],
            ),
            decision=decision,
        ),
        _call(
            7,
            VIEW,
            b"getFeeGrowthInside(bytes32,int24,int24)",
            growth,
            encode(["uint256", "uint256"], [Q128, 0]),
            decision=decision,
        ),
        _envelope(8, "eth_getBlockByNumber", [hex(value.block.number), False], _header(decision)),
    )
    return PositionSnapshotCapture(source, decision, VIEW, value, exchanges)


def retained(
    store: EvidenceStore,
    *,
    value: PositionSnapshot | None = None,
    sources: tuple[str, ...] = ("a", "b"),
    decision: BlockSnapshot = HEADERS[2],
) -> tuple[object, ...]:
    position = snapshot() if value is None else value
    return tuple(
        persist_position_snapshot_evidence(
            store, capture(source, position, decision=decision), retrieved_at=RETRIEVED_AT
        )
        for source in sources
    )


def expected_amounts(liquidity: int, sqrt_price: int) -> tuple[int, int]:
    """Uniswap integer rounding written out independently of the implementation."""
    lower = sqrt_price_at_tick(TICK_LOWER)
    upper = sqrt_price_at_tick(TICK_UPPER)
    assert lower < sqrt_price < upper
    return ((liquidity << 96) * (upper - sqrt_price) // upper) // sqrt_price, (
        liquidity * (sqrt_price - lower)
    ) // Q96


def expected_skew(total0: int, total1: int) -> Decimal:
    """The same expression ``produce_pool_state`` evaluates, at its own precision."""
    with localcontext() as context:
        context.prec = 80
        value0 = Decimal(total0) * Decimal(Q96**2) / Decimal(Q192)
        value1 = Decimal(total1)
        return (value0 - value1) / (value0 + value1)


def test_derived_inventory_is_bound_to_the_window_and_its_own_providers(tmp_path: Path) -> None:
    store, _refs, window = market(tmp_path)
    try:
        references = retained(store)
        inventory0, inventory1 = agreed_position_inventory(
            store,
            window,
            references,  # type: ignore[arg-type]
            scope=scope(),
        )
        principal0, principal1 = expected_amounts(LIQUIDITY, Q96)
        assert inventory0.value == principal0 + LIQUIDITY  # the uncollected fee is included
        assert inventory1.value == principal1
        assert (inventory0.name, inventory1.name) == ("inventory0", "inventory1")
        expected_interpretation = {
            "schema": INVENTORY_SCHEMA,
            "execution_eligible": False,
            "bytes_kind": "position_snapshot_wire_envelope_that_pinned_this_value",
            "pool_id": POOL_ID,
            "name": inventory0.name,
            "value": inventory0.value,
            "block": asdict(DECISION_BLOCK),
            "position_manager": MANAGER,
            "token_id": TOKEN_ID,
            "owner": OWNER,
            "parents": [
                asdict(item)
                for item in sorted(references, key=lambda item: item.source)  # type: ignore[union-attr]
            ],
        }
        for agreed in (inventory0, inventory1):
            assert agreed.block == DECISION_BLOCK
            assert frozenset(item.source for item in agreed.evidence) == {"a", "b"}
            expected_interpretation["name"] = agreed.name
            expected_interpretation["value"] = agreed.value
            for reference in agreed.evidence:
                record = store.get_record(reference)
                assert reference.kind == DERIVED_INVENTORY_KIND
                assert record.payload == agreed_value_payload(
                    agreed.name, agreed.value, DECISION_BLOCK
                )
                assert json.loads(record.interpretation_payload) == expected_interpretation
                assert record.request_payload is not None
                position = store.get_record(
                    next(item for item in references if item.source == reference.source)  # type: ignore[union-attr]
                )
                assert record.request_payload == position.request_payload
                assert record.raw_response_payload == position.raw_response_payload
    finally:
        store.close()


def test_derived_inventory_drives_the_assembled_cycle_state(tmp_path: Path) -> None:
    store, refs, window = market(tmp_path)
    try:
        inventory0, inventory1 = agreed_position_inventory(
            store,
            window,
            retained(store),  # type: ignore[arg-type]
            scope=scope(),
        )
        assembled = assemble(
            store,
            refs,
            window,
            declaration(),
            position_scope=scope(),
            inventory0=inventory0,
            inventory1=inventory1,
        )
        expected = expected_skew(inventory0.value, inventory1.value)
        assert assembled.cycle.state.inventory_imbalance == expected
        assert expected != Decimal(3000 - 1000) / Decimal(3000 + 1000)
        assert assembled.cycle.position_scope == scope()
        assert assembled.cycle.input_evidence.kind == "shadow_lp_cycle_input"
    finally:
        store.close()


@pytest.mark.parametrize(
    "name,value",
    [
        ("token_id", TOKEN_ID + 1),
        ("owner", "0x" + "6" * 40),
        ("position_manager", "0x" + "7" * 40),
        ("pool_id", "0x" + "8" * 64),
        ("chain_id", CHAIN + 1),
    ],
)
def test_scope_mismatch_is_refused(tmp_path: Path, name: str, value: object) -> None:
    store, _refs, window = market(tmp_path)
    try:
        with pytest.raises(ValueError, match="declared scope"):
            agreed_position_inventory(
                store,
                window,
                retained(store),  # type: ignore[arg-type]
                scope=scope(**{name: value}),
            )
    finally:
        store.close()


def test_wrong_block_pool_and_providers_are_refused(tmp_path: Path) -> None:
    store, _refs, window = market(tmp_path)
    try:
        earlier = retained(store, value=snapshot(decision=HEADERS[1]), decision=HEADERS[1])
        with pytest.raises(ValueError, match="decision block"):
            agreed_position_inventory(store, window, earlier, scope=scope())  # type: ignore[arg-type]
        other = replace(POOL, fee=3000)
        foreign_pool = retained(store, value=snapshot(pool=other, pool_id=pool_id_from_key(other)))
        with pytest.raises(ValueError, match="market window's pool"):
            agreed_position_inventory(store, window, foreign_pool, scope=scope())  # type: ignore[arg-type]
        foreign_source = retained(store, sources=("a", "c"))
        with pytest.raises(ValueError, match="own two providers"):
            agreed_position_inventory(store, window, foreign_source, scope=scope())  # type: ignore[arg-type]
    finally:
        store.close()


def test_disagreeing_providers_and_no_partial_retention(tmp_path: Path) -> None:
    store, _refs, window = market(tmp_path)
    try:
        disagreeing = (
            *retained(store, sources=("a",)),
            persist_position_snapshot_evidence(
                store, capture("b", snapshot(liquidity=LIQUIDITY + 1))
            ),
        )
        with pytest.raises(ValueError, match="disagree"):
            agreed_position_inventory(store, window, disagreeing, scope=scope())  # type: ignore[arg-type]
        foreign_source = retained(store, sources=("a", "c"))
        references = retained(store)
        database = tmp_path / "evidence.db"
        before = evidence_rows(database)
        with pytest.raises(ValueError, match="own two providers"):
            agreed_position_inventory(store, window, foreign_source, scope=scope())  # type: ignore[arg-type]
        assert evidence_rows(database) == before
        agreed_position_inventory(store, window, references, scope=scope())  # type: ignore[arg-type]
        # Two agreed names, one record per provider: nothing else is written.
        assert evidence_rows(database) == before + 4
    finally:
        store.close()


@pytest.mark.parametrize(
    "references",
    [
        pytest.param(("only",), id="one-reference"),
        pytest.param("not-a-tuple", id="untyped"),
    ],
)
def test_reference_shape_is_validated(tmp_path: Path, references: object) -> None:
    store, _refs, window = market(tmp_path)
    try:
        with pytest.raises(ValueError, match="two distinct position providers"):
            agreed_position_inventory(store, window, references, scope=scope())  # type: ignore[arg-type]
    finally:
        store.close()
