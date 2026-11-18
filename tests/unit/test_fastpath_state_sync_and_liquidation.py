"""Unit tests for incremental EVM pool state sync, reorg rollback, and flash liquidation."""

import os
import subprocess
from pathlib import Path

import pytest
from eth_abi.abi import encode

from oasis_agent.fastpath import (
    AERO_SYNC_TOPIC,
    V2_SYNC_TOPIC,
    V3_BURN_TOPIC,
    V3_MINT_TOPIC,
    V3_SWAP_TOPIC,
    V4_MODIFY_LIQUIDITY_TOPIC,
    V4_SWAP_TOPIC,
    BorrowerPositionSnapshot,
    EvmPoolLogEvent,
    FastpathBridge,
    FlashLiquidationConfig,
    FlashLiquidationScanner,
    IncrementalPoolStateSynchronizer,
    LiquidationCandidate,
    PoolKind,
    PoolNode,
    ReorgDetectedError,
    StateSyncError,
    TickLiquidity,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _addr(byte_hex: str) -> str:
    return "0x" + byte_hex * 20


def _hash32(byte_hex: str) -> str:
    return "0x" + byte_hex * 32


def _word_uint(value: int) -> str:
    return f"{value:064x}"


def _word_int(value: int) -> str:
    return f"{value & ((1 << 256) - 1):064x}"


def _topic_addr(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def _topic_int24(tick: int) -> str:
    return "0x" + _word_int(tick)


def _ensure_release_cdylib() -> Path:
    crates_dir = REPO_ROOT / "crates"
    lib_name = "liboasis_core.dylib" if os.uname().sysname == "Darwin" else "liboasis_core.so"
    release_lib = crates_dir / "target" / "release" / lib_name
    rust_sources = [
        crates_dir / "Cargo.toml",
        crates_dir / "oasis-core" / "Cargo.toml",
        *(crates_dir / "oasis-core" / "src").rglob("*.rs"),
    ]
    latest_src_mtime = max(p.stat().st_mtime for p in rust_sources)
    if not release_lib.is_file() or release_lib.stat().st_mtime < latest_src_mtime:
        subprocess.run(
            [
                "cargo",
                "build",
                "--offline",
                "--manifest-path",
                str(crates_dir / "Cargo.toml"),
                "--release",
            ],
            cwd=REPO_ROOT,
            check=True,
        )
    return release_lib


@pytest.fixture(scope="module")
def bridge() -> FastpathBridge:
    return FastpathBridge(lib_path=_ensure_release_cdylib())


def _sample_pools(bridge: FastpathBridge) -> dict[str, PoolNode]:
    weth = _addr("11")
    usdc = _addr("22")
    usdt = _addr("33")
    aero = _addr("44")

    t_v3 = -196_258
    t_v4 = 283_255
    return {
        "v2": PoolNode(
            chain_id=8453,
            pool_id=_addr("a1"),
            kind=PoolKind.V2,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fee_pips=3000,
            reserve0=100 * 10**18,
            reserve1=300_000 * 10**6,
            block_number=100,
        ),
        "aero_stable": PoolNode(
            chain_id=8453,
            pool_id=_addr("a2"),
            kind=PoolKind.AERODROME_STABLE,
            token0=usdc,
            token1=usdt,
            decimals0=6,
            decimals1=6,
            fee_pips=100,
            reserve0=5_000_000 * 10**6,
            reserve1=5_000_000 * 10**6,
            block_number=100,
        ),
        "v3": PoolNode(
            chain_id=8453,
            pool_id=_addr("a3"),
            kind=PoolKind.V3,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fee_pips=500,
            sqrt_price_x96=bridge.sqrt_price_at_tick(t_v3),
            tick=t_v3,
            liquidity=20_000_000_000_000_000,
            ticks=(
                TickLiquidity(tick=-196_800, liquidity_net=10_000_000_000_000_000),
                TickLiquidity(tick=-195_800, liquidity_net=-10_000_000_000_000_000),
            ),
            block_number=100,
        ),
        "v4": PoolNode(
            chain_id=8453,
            pool_id=_hash32("a4"),
            kind=PoolKind.V4,
            token0=usdc,
            token1=aero,
            decimals0=6,
            decimals1=18,
            fee_pips=500,
            sqrt_price_x96=bridge.sqrt_price_at_tick(t_v4),
            tick=t_v4,
            liquidity=1_000_000_000_000_000_000,
            ticks=(),
            block_number=100,
        ),
    }


def test_sync_swap_mint_burn_and_v4_events_in_tx_log_order(bridge: FastpathBridge) -> None:
    pools = _sample_pools(bridge)
    h100 = _hash32("b0")
    h101 = _hash32("b1")
    h102 = _hash32("b2")
    sync = IncrementalPoolStateSynchronizer(
        list(pools.values()),
        head_block_number=100,
        head_block_hash=h100,
    )

    # Block 101: out-of-order (tx_index, log_index) events for V2, AerodromeStable, V3, V4
    new_v3_tick = -196_000
    new_v3_sqrt = bridge.sqrt_price_at_tick(new_v3_tick) + 42
    new_v4_tick = 283_300
    new_v4_sqrt = bridge.sqrt_price_at_tick(new_v4_tick) + 17
    sender = _addr("d1")
    recipient = _addr("d2")
    v4_manager = _addr("ee")

    v2_sync_late = EvmPoolLogEvent(
        chain_id=8453,
        block_number=101,
        block_hash=h101,
        parent_hash=h100,
        tx_index=2,
        log_index=0,
        pool_id=pools["v2"].pool_id,
        topic0=V2_SYNC_TOPIC,
        topics=(V2_SYNC_TOPIC,),
        data_hex="0x" + encode(["uint112", "uint112"], [120 * 10**18, 250_000 * 10**6]).hex(),
    )
    v2_sync_early = EvmPoolLogEvent(
        chain_id=8453,
        block_number=101,
        block_hash=h101,
        parent_hash=h100,
        tx_index=0,
        log_index=3,
        pool_id=pools["v2"].pool_id,
        topic0=V2_SYNC_TOPIC,
        topics=(V2_SYNC_TOPIC,),
        data_hex="0x" + encode(["uint112", "uint112"], [110 * 10**18, 270_000 * 10**6]).hex(),
    )
    aero_reserve0 = (1 << 113) + 12345  # exceeds uint112, valid uint256
    aero_reserve1 = (1 << 114) + 67890
    aero_sync = EvmPoolLogEvent(
        chain_id=8453,
        block_number=101,
        block_hash=h101,
        parent_hash=h100,
        tx_index=1,
        log_index=0,
        pool_id=pools["aero_stable"].pool_id,
        topic0=AERO_SYNC_TOPIC,
        topics=(AERO_SYNC_TOPIC,),
        data_hex="0x" + encode(["uint256", "uint256"], [aero_reserve0, aero_reserve1]).hex(),
    )
    v3_swap = EvmPoolLogEvent(
        chain_id=8453,
        block_number=101,
        block_hash=h101,
        parent_hash=h100,
        tx_index=1,
        log_index=2,
        pool_id=pools["v3"].pool_id,
        topic0=V3_SWAP_TOPIC,
        topics=(V3_SWAP_TOPIC, _topic_addr(sender), _topic_addr(recipient)),
        data_hex="0x"
        + encode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            [-5 * 10**18, 15_000 * 10**6, new_v3_sqrt, 25_000_000_000_000_000, new_v3_tick],
        ).hex(),
    )
    v4_swap = EvmPoolLogEvent(
        chain_id=8453,
        block_number=101,
        block_hash=h101,
        parent_hash=h100,
        tx_index=1,
        log_index=4,
        pool_id=v4_manager,
        topic0=V4_SWAP_TOPIC,
        topics=(V4_SWAP_TOPIC, pools["v4"].pool_id, _topic_addr(sender)),
        data_hex="0x"
        + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [-100 * 10**6, 200 * 10**18, new_v4_sqrt, 1_500_000_000_000_000_000, new_v4_tick, 250],
        ).hex(),
    )

    # Pass events intentionally scrambled
    applied = sync.apply_block_logs(
        101,
        h101,
        h100,
        [v2_sync_late, v4_swap, v3_swap, v2_sync_early, aero_sync],
    )
    assert applied == 5
    assert sync.head_block_number == 101
    assert sync.head_block_hash == h101

    v2_after = sync.get_pool(8453, pools["v2"].pool_id)
    assert v2_after is not None
    assert (v2_after.reserve0, v2_after.reserve1) == (120 * 10**18, 250_000 * 10**6)

    aero_after = sync.get_pool(8453, pools["aero_stable"].pool_id)
    assert aero_after is not None
    assert (aero_after.reserve0, aero_after.reserve1) == (aero_reserve0, aero_reserve1)

    v3_after = sync.get_pool(8453, pools["v3"].pool_id)
    assert v3_after is not None
    assert (v3_after.sqrt_price_x96, v3_after.liquidity, v3_after.tick) == (
        new_v3_sqrt,
        25_000_000_000_000_000,
        new_v3_tick,
    )

    v4_after = sync.get_pool(8453, pools["v4"].pool_id)
    assert v4_after is not None
    assert (v4_after.sqrt_price_x96, v4_after.liquidity, v4_after.tick, v4_after.fee_pips) == (
        new_v4_sqrt,
        1_500_000_000_000_000_000,
        new_v4_tick,
        250,
    )

    # Block 102: V3 Mint (in-range + out-of-range), V3 Burn, V4 ModifyLiquidity
    mint_in_range = EvmPoolLogEvent(
        chain_id=8453,
        block_number=102,
        block_hash=h102,
        parent_hash=h101,
        tx_index=0,
        log_index=0,
        pool_id=pools["v3"].pool_id,
        topic0=V3_MINT_TOPIC,
        topics=(
            V3_MINT_TOPIC,
            _topic_addr(sender),
            _topic_int24(-196_500),
            _topic_int24(-195_500),
        ),
        data_hex="0x"
        + encode(
            ["address", "uint128", "uint256", "uint256"], [sender, 5_000_000_000_000_000, 1, 2]
        ).hex(),
    )
    mint_out_of_range = EvmPoolLogEvent(
        chain_id=8453,
        block_number=102,
        block_hash=h102,
        parent_hash=h101,
        tx_index=0,
        log_index=1,
        pool_id=pools["v3"].pool_id,
        topic0=V3_MINT_TOPIC,
        topics=(
            V3_MINT_TOPIC,
            _topic_addr(sender),
            _topic_int24(-196_000),  # equal to active tick -> tickUpper check; wait tick == lower!
            _topic_int24(-195_900),
        ),
        data_hex="0x"
        + encode(
            ["address", "uint128", "uint256", "uint256"], [sender, 1_000_000_000_000_000, 1, 2]
        ).hex(),
    )
    # Wait: tickLower=-196_000 <= new_v3_tick=-196_000 < -195_900 IS in range.
    # Burn the exact same range [-196_000, -195_900) and test out-of-range V4 ModifyLiquidity.
    burn_v3 = EvmPoolLogEvent(
        chain_id=8453,
        block_number=102,
        block_hash=h102,
        parent_hash=h101,
        tx_index=0,
        log_index=2,
        pool_id=pools["v3"].pool_id,
        topic0=V3_BURN_TOPIC,
        topics=(
            V3_BURN_TOPIC,
            _topic_addr(sender),
            _topic_int24(-196_000),
            _topic_int24(-195_900),
        ),
        data_hex="0x"
        + encode(["uint128", "uint256", "uint256"], [1_000_000_000_000_000, 0, 0]).hex(),
    )
    v4_mod_in = EvmPoolLogEvent(
        chain_id=8453,
        block_number=102,
        block_hash=h102,
        parent_hash=h101,
        tx_index=1,
        log_index=0,
        pool_id=pools["v4"].pool_id,
        topic0=V4_MODIFY_LIQUIDITY_TOPIC,
        topics=(V4_MODIFY_LIQUIDITY_TOPIC, pools["v4"].pool_id, _topic_addr(sender)),
        data_hex="0x"
        + encode(
            ["int24", "int24", "int256", "bytes32"],
            [283_000, 283_500, -250_000_000_000_000_000, bytes(32)],
        ).hex(),
    )
    v4_mod_out = EvmPoolLogEvent(
        chain_id=8453,
        block_number=102,
        block_hash=h102,
        parent_hash=h101,
        tx_index=1,
        log_index=1,
        pool_id=pools["v4"].pool_id,
        topic0=V4_MODIFY_LIQUIDITY_TOPIC,
        topics=(V4_MODIFY_LIQUIDITY_TOPIC, pools["v4"].pool_id, _topic_addr(sender)),
        data_hex="0x"
        + encode(
            ["int24", "int24", "int256", "bytes32"],
            [283_500, 284_000, 700_000_000_000_000_000, bytes(32)],
        ).hex(),
    )

    applied_102 = sync.apply_block_logs(
        102,
        h102,
        h101,
        [mint_in_range, mint_out_of_range, burn_v3, v4_mod_in, v4_mod_out],
    )
    assert applied_102 == 5

    v3_102 = sync.get_pool(8453, pools["v3"].pool_id)
    assert v3_102 is not None
    assert v3_102.liquidity == 30_000_000_000_000_000
    assert (-196_500, 5_000_000_000_000_000) in [(t.tick, t.liquidity_net) for t in v3_102.ticks]
    assert (-195_500, -5_000_000_000_000_000) in [(t.tick, t.liquidity_net) for t in v3_102.ticks]
    # Mint + Burn cancelled tick -196_000 / -195_900 cleanly
    assert -196_000 not in [t.tick for t in v3_102.ticks]

    v4_102 = sync.get_pool(8453, pools["v4"].pool_id)
    assert v4_102 is not None
    assert v4_102.liquidity == 1_250_000_000_000_000_000
    assert [(t.tick, t.liquidity_net) for t in v4_102.ticks] == [
        (283_000, -250_000_000_000_000_000),
        (283_500, 950_000_000_000_000_000),
        (284_000, -700_000_000_000_000_000),
    ]

    # Verify exported FastpathPoolGraph quotes cleanly in Rust
    graph = sync.to_fastpath_graph(bridge=bridge)
    q = graph.quote_pool(8453, pools["v3"].pool_id, pools["v3"].token0, 10**17)
    assert q.amount_out > 0


def test_reorg_detection_rollback_retention_and_fail_closed_quarantine(
    bridge: FastpathBridge,
) -> None:
    pools = _sample_pools(bridge)
    v2 = pools["v2"]
    v3 = pools["v3"]
    h99 = _hash32("09")
    h100 = _hash32("10")
    h101 = _hash32("11")
    h102 = _hash32("12")
    h103 = _hash32("13")
    h101_alt = _hash32("91")

    sync = IncrementalPoolStateSynchronizer(
        [v2, v3],
        head_block_number=99,
        head_block_hash=h99,
        max_rollback_blocks=2,
    )

    def _v2_ev(block_num: int, bh: str, ph: str, r0: int, r1: int) -> EvmPoolLogEvent:
        return EvmPoolLogEvent(
            chain_id=8453,
            block_number=block_num,
            block_hash=bh,
            parent_hash=ph,
            tx_index=0,
            log_index=0,
            pool_id=v2.pool_id,
            topic0=V2_SYNC_TOPIC,
            topics=(V2_SYNC_TOPIC,),
            data_hex="0x" + encode(["uint112", "uint112"], [r0, r1]).hex(),
        )

    assert (
        sync.apply_block_logs(
            100, h100, h99, [_v2_ev(100, h100, h99, 101 * 10**18, 300_000 * 10**6)]
        )
        == 1
    )
    assert (
        sync.apply_block_logs(
            101, h101, h100, [_v2_ev(101, h101, h100, 102 * 10**18, 300_000 * 10**6)]
        )
        == 1
    )

    # Gap rejected
    with pytest.raises(ReorgDetectedError, match="non-contiguous"):
        sync.apply_block_logs(103, h103, h101, [])

    # Parent mismatch rejected until rollback
    with pytest.raises(ReorgDetectedError, match="parent_hash mismatch"):
        sync.apply_block_logs(102, h102, _hash32("ff"), [])

    # Duplicate block number rejected as non-contiguous reorg
    with pytest.raises(ReorgDetectedError, match="non-contiguous"):
        sync.apply_block_logs(
            101, h101_alt, h100, [_v2_ev(101, h101_alt, h100, 105 * 10**18, 300_000 * 10**6)]
        )

    # State remained intact at block 101
    assert sync.head_block_number == 101
    p101 = sync.get_pool(8453, v2.pool_id)
    assert p101 is not None and p101.reserve0 == 102 * 10**18

    # Rollback to 100 with wrong hash fails
    with pytest.raises(StateSyncError, match="rollback hash mismatch"):
        sync.rollback_to_block(100, _hash32("aa"))

    # Rollback to 100 with exact hash restores block 100 state and accepts alt chain
    sync.rollback_to_block(100, h100)
    assert sync.head_block_number == 100
    assert sync.head_block_hash == h100
    p100 = sync.get_pool(8453, v2.pool_id)
    assert p100 is not None and p100.reserve0 == 101 * 10**18

    assert (
        sync.apply_block_logs(
            101, h101_alt, h100, [_v2_ev(101, h101_alt, h100, 105 * 10**18, 300_000 * 10**6)]
        )
        == 1
    )
    assert sync.apply_block_logs(102, h102, h101_alt, []) == 0
    assert sync.apply_block_logs(103, h103, h102, []) == 0

    # Retention window max_rollback_blocks=2 keeps blocks {101, 102, 103}; 100 was evicted
    with pytest.raises(StateSyncError, match="retention window"):
        sync.rollback_to_block(100, h100)
    sync.rollback_to_block(101, h101_alt)
    assert sync.head_block_number == 101

    # Fail-closed on malformed hex / non-canonical sign extension / overflow keeps block 101 intact
    valid_v2_102 = _v2_ev(102, h102, h101_alt, 109 * 10**18, 300_000 * 10**6)
    bad_v3_noncanonical_tick = EvmPoolLogEvent(
        chain_id=8453,
        block_number=102,
        block_hash=h102,
        parent_hash=h101_alt,
        tx_index=1,
        log_index=0,
        pool_id=v3.pool_id,
        topic0=V3_SWAP_TOPIC,
        topics=(V3_SWAP_TOPIC, _topic_addr(_addr("d1")), _topic_addr(_addr("d2"))),
        data_hex="0x"
        + _word_int(-1)
        + _word_int(1)
        + _word_uint(bridge.sqrt_price_at_tick(0))
        + _word_uint(10**15)
        + "0000000000000000000000000000000000000000000000000000000000ffffff",  # unextended -1!
    )
    with pytest.raises(StateSyncError, match="sign-extended"):
        sync.apply_block_logs(102, h102, h101_alt, [valid_v2_102, bad_v3_noncanonical_tick])

    # Atomic rollback: valid_v2_102 inside the failed block did NOT mutate head state
    assert sync.head_block_number == 101
    p_still_101 = sync.get_pool(8453, v2.pool_id)
    assert p_still_101 is not None and p_still_101.reserve0 == 105 * 10**18

    # With quarantine_on_event_error=True, malformed V3 pool is quarantined while V2 updates
    applied = sync.apply_block_logs(
        102,
        h102,
        h101_alt,
        [valid_v2_102, bad_v3_noncanonical_tick],
        quarantine_on_event_error=True,
    )
    assert applied == 1
    assert v3.pool_id.lower() in sync.quarantined_pools
    graph = sync.to_fastpath_graph(bridge=bridge)
    assert graph.quote_pool(8453, v2.pool_id, v2.token0, 10**18).amount_out > 0
    with pytest.raises(KeyError):
        graph.quote_pool(8453, v3.pool_id, v3.token0, 10**18)

    # Rolling back to 101 unquarantines V3 cleanly
    sync.rollback_to_block(101, h101_alt)
    assert sync.quarantined_pools == {}


def test_flash_liquidation_scanner_fraction_pool_selection_and_filters(
    bridge: FastpathBridge,
) -> None:
    weth = _addr("11")
    usdc = _addr("22")
    cbeth = _addr("55")
    block_hash = _hash32("c1")

    # Sync two WETH/USDC pools via state synchronizer:
    # Pool A (shallow V2: good spot price 3200 USDC/WETH, low depth -> 100% slips badly)
    # Pool B (deep V2: slightly lower price 3100 USDC/WETH, huge depth -> 100% beats shallow pool)
    pool_shallow = PoolNode(
        chain_id=8453,
        pool_id=_addr("a1"),
        kind=PoolKind.V2,
        token0=weth,
        token1=usdc,
        decimals0=18,
        decimals1=6,
        fee_pips=3000,
        reserve0=5 * 10**18,
        reserve1=16_000 * 10**6,
        block_number=200,
    )
    pool_deep = PoolNode(
        chain_id=8453,
        pool_id=_addr("a2"),
        kind=PoolKind.V2,
        token0=weth,
        token1=usdc,
        decimals0=18,
        decimals1=6,
        fee_pips=500,
        reserve0=1_000 * 10**18,
        reserve1=3_100_000 * 10**6,
        block_number=200,
    )
    # Pool C: cbETH/USDC medium-depth pool where partial (25%) repay beats 100% repay
    pool_cbeth = PoolNode(
        chain_id=8453,
        pool_id=_addr("a3"),
        kind=PoolKind.V2,
        token0=cbeth,
        token1=usdc,
        decimals0=18,
        decimals1=6,
        fee_pips=3000,
        reserve0=12 * 10**18,
        reserve1=40_800 * 10**6,
        block_number=200,
    )

    sync = IncrementalPoolStateSynchronizer(
        [pool_shallow, pool_deep, pool_cbeth],
        head_block_number=200,
        head_block_hash=block_hash,
    )
    graph = sync.to_fastpath_graph(bridge=bridge)

    # Oracle price: 1 WETH (1e18 raw) = 3000 USDC (3000e6 raw) -> oracle_wad = 3000e6.
    # With 5% bonus (500 bps), max_repay_by_collateral = 4 * 3000e6 * 10000 // 10500,
    # while debt_amount_raw=20_000e6 with close_factor=5000 caps max_repay_by_debt=10_000e6.
    pos_morpho = BorrowerPositionSnapshot(
        protocol="morpho_blue",
        market_id=_hash32("d1"),
        borrower=_addr("f1"),
        debt_token=usdc,
        collateral_token=weth,
        debt_amount_raw=20_000 * 10**6,
        collateral_amount_raw=4 * 10**18,
        health_factor_wad=95 * 10**16,  # 0.95
        close_factor_bps=5000,
        liquidation_bonus_bps=500,
        oracle_price_collateral_in_debt_wad=3000 * 10**6,
        block_number=200,
        block_hash=block_hash,
    )
    # Position 2 on Moonwell (cbETH/USDC) on shallow pool_cbeth (spot 3400 vs oracle 3000):
    # Full 100% repay (~3.33 cbETH) against 4 cbETH reserve suffers >50% slippage,
    # whereas 25% or 50% repay stays profitable.
    pos_moonwell = BorrowerPositionSnapshot(
        protocol="moonwell",
        market_id=_addr("d2"),
        borrower=_addr("f2"),
        debt_token=usdc,
        collateral_token=cbeth,
        debt_amount_raw=10_000 * 10**6,
        collateral_amount_raw=5 * 10**18,
        health_factor_wad=90 * 10**16,
        close_factor_bps=10_000,
        liquidation_bonus_bps=800,
        oracle_price_collateral_in_debt_wad=3000 * 10**6,
        block_number=200,
        block_hash=block_hash,
    )
    # Filtered positions (must all be skipped)
    healthy = BorrowerPositionSnapshot(
        protocol="aave_v3",
        market_id="aave_base_weth_usdc",
        borrower=_addr("f3"),
        debt_token=usdc,
        collateral_token=weth,
        debt_amount_raw=10_000 * 10**6,
        collateral_amount_raw=5 * 10**18,
        health_factor_wad=10**18,  # >= 1.0 WAD -> healthy!
        close_factor_bps=5000,
        liquidation_bonus_bps=500,
        oracle_price_collateral_in_debt_wad=3000 * 10**6,
        block_number=200,
        block_hash=block_hash,
    )
    zero_hf = BorrowerPositionSnapshot(
        protocol="aave_v3",
        market_id="aave_base_weth_usdc",
        borrower=_addr("f4"),
        debt_token=usdc,
        collateral_token=weth,
        debt_amount_raw=10_000 * 10**6,
        collateral_amount_raw=5 * 10**18,
        health_factor_wad=0,
        close_factor_bps=5000,
        liquidation_bonus_bps=500,
        oracle_price_collateral_in_debt_wad=3000 * 10**6,
        block_number=200,
        block_hash=block_hash,
    )
    bad_close_factor = BorrowerPositionSnapshot(
        protocol="aave_v3",
        market_id="aave_base_weth_usdc",
        borrower=_addr("f5"),
        debt_token=usdc,
        collateral_token=weth,
        debt_amount_raw=10_000 * 10**6,
        collateral_amount_raw=5 * 10**18,
        health_factor_wad=9 * 10**17,
        close_factor_bps=10_001,
        liquidation_bonus_bps=500,
        oracle_price_collateral_in_debt_wad=3000 * 10**6,
        block_number=200,
        block_hash=block_hash,
    )

    cfg = FlashLiquidationConfig(
        flash_fee_bps=9,  # 9 bps (Aave-style) with ceiling rounding
        l2_gas_units=380_000,
        gas_cost_in_debt_raw=500_000,  # 0.50 USDC
        min_net_profit_debt_raw=1_000_000,  # 1.00 USDC
        max_repay_debt_raw=0,
    )
    candidates = FlashLiquidationScanner.scan(
        [pos_moonwell, pos_morpho, healthy, zero_hf, bad_close_factor],
        graph,
        cfg,
    )
    assert len(candidates) == 2
    assert candidates[0].net_profit_debt_raw > candidates[1].net_profit_debt_raw

    morpho_cand = next(c for c in candidates if c.protocol == "morpho_blue")
    assert morpho_cand.swap_pool_id == pool_deep.pool_id.lower()
    assert morpho_cand.repay_debt_raw == 10_000 * 10**6
    assert morpho_cand.flash_fee_debt_raw == (morpho_cand.repay_debt_raw * 9 + 9_999) // 10_000
    assert morpho_cand.gas_cost_in_debt_raw == 500_000
    assert (
        morpho_cand.net_profit_debt_raw
        == morpho_cand.swap_out_debt_raw
        - morpho_cand.repay_debt_raw
        - morpho_cand.flash_fee_debt_raw
        - cfg.gas_cost_in_debt_raw
    )
    assert morpho_cand.execution_eligible is False

    moonwell_cand = next(c for c in candidates if c.protocol == "moonwell")
    assert moonwell_cand.swap_pool_id == pool_cbeth.pool_id.lower()
    # Optimal fraction is strictly partial (< 100% of max repay) because 100% destroys pool price
    max_repay_moonwell = min(
        (pos_moonwell.debt_amount_raw * pos_moonwell.close_factor_bps) // 10_000,
        (
            pos_moonwell.collateral_amount_raw
            * pos_moonwell.oracle_price_collateral_in_debt_wad
            * 10_000
        )
        // ((10_000 + pos_moonwell.liquidation_bonus_bps) * 10**18),
    )
    assert moonwell_cand.repay_debt_raw < max_repay_moonwell
    assert moonwell_cand.execution_eligible is False

    # Capping max_repay_debt_raw restricts repay size cleanly
    capped_cfg = FlashLiquidationConfig(
        flash_fee_bps=9,
        gas_cost_in_debt_raw=100_000,
        min_net_profit_debt_raw=1,
        max_repay_debt_raw=2_000 * 10**6,
    )
    capped = FlashLiquidationScanner.scan([pos_morpho], graph, capped_cfg)
    assert len(capped) == 1
    assert capped[0].repay_debt_raw == 2_000 * 10**6

    # Prohibitive gas cost rejects all candidates
    unprofitable_cfg = FlashLiquidationConfig(
        gas_cost_in_debt_raw=10_000 * 10**6,
        min_net_profit_debt_raw=1,
    )
    assert FlashLiquidationScanner.scan([pos_morpho, pos_moonwell], graph, unprofitable_cfg) == []

    # Execution eligibility cannot be forced True
    with pytest.raises(ValueError, match="execution_eligible=False"):
        LiquidationCandidate(
            protocol="morpho_blue",
            market_id=_hash32("d1"),
            borrower=_addr("f1"),
            debt_token=usdc,
            collateral_token=weth,
            repay_debt_raw=1,
            seized_collateral_raw=1,
            swap_pool_id=_addr("a1"),
            swap_out_debt_raw=2,
            flash_fee_debt_raw=0,
            gas_cost_in_debt_raw=0,
            net_profit_debt_raw=1,
            health_factor_wad=9 * 10**17,
            block_number=200,
            block_hash=block_hash,
            execution_eligible=True,  # type: ignore[arg-type]
        )


def test_state_sync_edge_cases_and_malformed_events(bridge: FastpathBridge) -> None:
    pools = _sample_pools(bridge)
    v2 = pools["v2"]
    v3 = pools["v3"]
    v4 = pools["v4"]
    h99 = _hash32("09")
    h100 = _hash32("10")
    h101 = _hash32("11")
    sender = _addr("d1")

    # Cold-start synchronizer (head=None) allows rollback to pre-first-block parent hash
    sync = IncrementalPoolStateSynchronizer([v2, v3, v4])
    assert sync.head_block_number is None
    untracked_ev = EvmPoolLogEvent(
        chain_id=8453,
        block_number=100,
        block_hash=h100,
        parent_hash=h99,
        tx_index=0,
        log_index=0,
        pool_id=_addr("fe"),
        topic0=V2_SYNC_TOPIC,
        topics=(V2_SYNC_TOPIC,),
        data_hex="0x" + encode(["uint112", "uint112"], [10, 20]).hex(),
    )
    assert sync.apply_block_logs(100, h100, h99, [untracked_ev]) == 0
    assert sync.head_block_number == 100
    sync.rollback_to_block(99, h99)
    assert sync.head_block_number == 99

    # Malformed data_hex, wrong pool kind, V2 uint112 overflow, V4 fee>=1e6,
    # V3 sqrtPrice/tick mismatch, Burn underflow, duplicate log index, and batch header mismatch
    assert sync.apply_block_logs(100, h100, h99, []) == 0

    cases: list[tuple[EvmPoolLogEvent, type[Exception], str]] = [
        (
            EvmPoolLogEvent(
                chain_id=8453,
                block_number=101,
                block_hash=h101,
                parent_hash=h100,
                tx_index=0,
                log_index=0,
                pool_id=v2.pool_id,
                topic0=V2_SYNC_TOPIC,
                topics=(V2_SYNC_TOPIC,),
                data_hex="0x1234",  # not 32-byte aligned
            ),
            StateSyncError,
            "word aligned",
        ),
        (
            EvmPoolLogEvent(
                chain_id=8453,
                block_number=101,
                block_hash=h101,
                parent_hash=h100,
                tx_index=0,
                log_index=0,
                pool_id=v2.pool_id,
                topic0=V2_SYNC_TOPIC,
                topics=(V2_SYNC_TOPIC,),
                data_hex="0x" + _word_uint(1 << 112) + _word_uint(100),
            ),
            StateSyncError,
            "out of bounds",
        ),
        (
            EvmPoolLogEvent(
                chain_id=8453,
                block_number=101,
                block_hash=h101,
                parent_hash=h100,
                tx_index=0,
                log_index=0,
                pool_id=v3.pool_id,
                topic0=V2_SYNC_TOPIC,
                topics=(V2_SYNC_TOPIC,),
                data_hex="0x" + _word_uint(100) + _word_uint(100),
            ),
            StateSyncError,
            "incompatible",
        ),
        (
            EvmPoolLogEvent(
                chain_id=8453,
                block_number=101,
                block_hash=h101,
                parent_hash=h100,
                tx_index=0,
                log_index=0,
                pool_id=v4.pool_id,
                topic0=V4_SWAP_TOPIC,
                topics=(V4_SWAP_TOPIC, v4.pool_id, _topic_addr(sender)),
                data_hex="0x"
                + encode(
                    ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                    [-10, 10, bridge.sqrt_price_at_tick(0), 10**12, 0, 1_000_000],
                ).hex(),
            ),
            StateSyncError,
            "out of bounds",
        ),
        (
            EvmPoolLogEvent(
                chain_id=8453,
                block_number=101,
                block_hash=h101,
                parent_hash=h100,
                tx_index=0,
                log_index=0,
                pool_id=v3.pool_id,
                topic0=V3_SWAP_TOPIC,
                topics=(V3_SWAP_TOPIC, _topic_addr(sender), _topic_addr(sender)),
                data_hex="0x"
                + encode(
                    ["int256", "int256", "uint160", "uint128", "int24"],
                    [-10, 10, bridge.sqrt_price_at_tick(100), 10**12, 0],
                ).hex(),
            ),
            StateSyncError,
            "sqrt_price_at_tick",
        ),
        (
            EvmPoolLogEvent(
                chain_id=8453,
                block_number=101,
                block_hash=h101,
                parent_hash=h100,
                tx_index=0,
                log_index=0,
                pool_id=v3.pool_id,
                topic0=V3_BURN_TOPIC,
                topics=(
                    V3_BURN_TOPIC,
                    _topic_addr(sender),
                    _topic_int24(-196_800),
                    _topic_int24(-195_800),
                ),
                data_hex="0x"
                + encode(
                    ["uint128", "uint256", "uint256"],
                    [(v3.liquidity or 0) + 1, 0, 0],
                ).hex(),
            ),
            StateSyncError,
            "underflows",
        ),
        (
            EvmPoolLogEvent(
                chain_id=8453,
                block_number=102,  # mismatch against batch 101
                block_hash=h101,
                parent_hash=h100,
                tx_index=0,
                log_index=0,
                pool_id=v2.pool_id,
                topic0=V2_SYNC_TOPIC,
                topics=(V2_SYNC_TOPIC,),
                data_hex="0x" + _word_uint(100) + _word_uint(100),
            ),
            ReorgDetectedError,
            "batch block_number",
        ),
    ]

    for bad_event, exc_cls, pattern in cases:
        with pytest.raises(exc_cls, match=pattern):
            sync.apply_block_logs(101, h101, h100, [bad_event])
        assert sync.head_block_number == 100

    # Duplicate (tx_index, log_index) rejected
    dup_ev = EvmPoolLogEvent(
        chain_id=8453,
        block_number=101,
        block_hash=h101,
        parent_hash=h100,
        tx_index=1,
        log_index=1,
        pool_id=v2.pool_id,
        topic0=V2_SYNC_TOPIC,
        topics=(V2_SYNC_TOPIC,),
        data_hex="0x" + _word_uint(10**18) + _word_uint(3000 * 10**6),
    )
    with pytest.raises(StateSyncError, match="duplicate"):
        sync.apply_block_logs(101, h101, h100, [dup_ev, dup_ev])


def test_liquidation_handles_cl_exhaustion_gracefully_with_partial_repay(
    bridge: FastpathBridge,
) -> None:
    weth = _addr("11")
    usdc = _addr("22")
    block_hash = _hash32("e5")
    tick_v3 = -196_258

    # V3 pool with finite liquidity bounded at tick -196_800 so selling 4 WETH exhausts ticks,
    # while selling 1 WETH (25% repay) succeeds cleanly!
    shallow_v3 = PoolNode(
        chain_id=8453,
        pool_id=_addr("c7"),
        kind=PoolKind.V3,
        token0=weth,
        token1=usdc,
        decimals0=18,
        decimals1=6,
        fee_pips=500,
        sqrt_price_x96=bridge.sqrt_price_at_tick(tick_v3),
        tick=tick_v3,
        liquidity=20_000_000_000_000_000,
        ticks=(TickLiquidity(tick=-196_800, liquidity_net=20_000_000_000_000_000),),
        block_number=300,
    )
    sync = IncrementalPoolStateSynchronizer(
        [shallow_v3],
        head_block_number=300,
        head_block_hash=block_hash,
    )
    graph = sync.to_fastpath_graph(bridge=bridge)

    # Aave V3 position with 4 WETH collateral and 12_000 USDC debt at oracle 2800 USDC/WETH:
    # 100% / 75% / 50% repay exhausts shallow_v3 liquidity -> FastpathCallError caught per attempt;
    # 25% (~1.07 WETH) fits inside tick range and succeeds profitably!
    pos_aave = BorrowerPositionSnapshot(
        protocol="aave_v3",
        market_id="aave_v3_base_weth_usdc",
        borrower=_addr("f9"),
        debt_token=usdc,
        collateral_token=weth,
        debt_amount_raw=12_000 * 10**6,
        collateral_amount_raw=4 * 10**18,
        health_factor_wad=88 * 10**16,
        close_factor_bps=10_000,
        liquidation_bonus_bps=800,
        oracle_price_collateral_in_debt_wad=2_700 * 10**6,
        block_number=300,
        block_hash=block_hash,
    )
    cfg = FlashLiquidationConfig(
        flash_fee_bps=5,
        gas_cost_in_debt_raw=100_000,
        min_net_profit_debt_raw=1_000_000,
    )
    cands = FlashLiquidationScanner.scan([pos_aave], graph, cfg)
    assert len(cands) == 1
    assert cands[0].protocol == "aave_v3"
    assert cands[0].swap_pool_id == shallow_v3.pool_id.lower()
    assert cands[0].net_profit_debt_raw >= cfg.min_net_profit_debt_raw
    assert cands[0].execution_eligible is False
