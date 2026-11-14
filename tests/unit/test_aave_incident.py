"""Historical snapshots must agree with the receipt before attributing a cashflow."""

from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.arbitrage import aave_incident as incident
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import BlockRef
from tests.unit.test_aave_cashflows import transfer
from tests.unit.test_aave_history import ACCOUNT, ACTOR, POOL, receipt
from tests.unit.test_public_receipts import BLOCK, TX

STABLE = "0x" + "61" * 20
WETH = "0x" + "62" * 20
CONFIGURATOR = "0x" + "63" * 20
BUY = "0x" + "64" * 32
UNWRAP = "0x" + "65" * 32


def words(types, values):
    return "0x" + encode(types, values).hex()


def plan():
    return incident.Plan(
        schema="oasis-aave-incident-v1",
        chain_id=BLOCK.chain_id,
        account=ACCOUNT,
        pool=POOL,
        provider="0x" + "66" * 20,
        configurator=CONFIGURATOR,
        oracle="0x" + "67" * 20,
        oracle_runtime_hash="0x" + "68" * 32,
        weth=WETH,
        weth9_runtime_hash="0x" + "69" * 32,
        stable=STABLE,
        stable_decimals=6,
        freeze_tx=TX,
        borrow_tx="0x" + "70" * 32,
        purchase_tx=BUY,
        unwrap_tx=UNWRAP,
        max_weth_feed_age_seconds=1800,
    )


def reserve(decimals, frozen=False, paused=False):
    raw = 8000 | (8500 << 16) | (decimals << 48) | (1 << 56) | (1 << 58)
    raw |= int(frozen) << 57 | int(paused) << 60
    return incident.reserve(words(("uint256",), (raw,)), decimals)


def singleton(address, signature, indexed, types, values, index=1):
    return LogRecord(
        address=address,
        topics=(signature, words(("address",), (indexed,))),
        data=words(types, values),
        block_number=BLOCK.number,
        block_hash=BLOCK.block_hash,
        transaction_hash=TX,
        transaction_index=2,
        log_index=index,
    )


def test_configurator_event_must_agree_with_two_reserves_at_block_boundaries():
    sample = receipt(
        singleton(CONFIGURATOR, incident.FREEZE, STABLE, ("bool",), (True,)),
        singleton(CONFIGURATOR, incident.FREEZE, WETH, ("bool",), (True,), index=3),
    )
    before = {STABLE: reserve(6), WETH: reserve(18)}
    after = {STABLE: reserve(6, frozen=True), WETH: reserve(18, frozen=True, paused=True)}
    first, second = incident.freeze(plan(), sample, before, after)
    assert first["log_index"] == 1 and second["log_index"] == 3
    assert first["freeze_block_end"]["frozen"] and not first["freeze_block_end"]["paused"]
    assert second["freeze_block_end"]["paused"]
    with pytest.raises(ValueError, match="boundary_disagreement"):
        incident.freeze(plan(), sample, before, before)
    with pytest.raises(ValueError, match="boundary_disagreement"):
        incident.freeze(plan(), replace(sample, logs=sample.logs[:1]), before, after)


def test_paired_receipt_cashflows_keep_quantity_and_gas_separate():
    first = replace(
        receipt(
            transfer(STABLE, ACCOUNT, ACTOR, 1000000, 1),
            transfer(WETH, ACTOR, ACCOUNT, 10**18, 3),
        ),
        transaction_hash=BUY,
        sender=ACTOR,
    )
    fill = incident.purchase(plan(), first)
    assert fill["stable_paid_raw"] == 1000000
    assert fill["weth_received_raw"] == 10**18
    with pytest.raises(ValueError, match="transfer_ambiguous"):
        incident.purchase(plan(), replace(first, logs=first.logs + first.logs[:1]))
    second = replace(
        receipt(singleton(WETH, incident.WITHDRAWAL, ACCOUNT, ("uint256",), (10**18,))),
        transaction_hash=UNWRAP,
        sender=ACCOUNT,
        recipient=WETH,
        gas_used=40000,
        effective_gas_price=4,
    )
    assert incident.unwrap(plan(), second, fill["weth_received_raw"]) == {
        "weth_withdrawn_raw": 10**18,
        "withdrawal_log_index": 1,
        "gas_paid_native_raw": 160000,
    }
    with pytest.raises(ValueError, match="quantity"):
        incident.unwrap(plan(), second, 2 * 10**18)


@pytest.mark.parametrize("updated, answer", [(100, 200), (101, 200), (80, 200), (99, 199)])
def test_weth_source_round_checks_age_disagreement_and_lookahead(updated, answer):
    block = BlockRef(1, 3, BLOCK.block_hash, 100)
    raw = words(("uint80", "int256", "uint256", "uint256", "uint80"), (4, answer, 79, updated, 4))
    if updated == 100:
        result = incident.weth_round(raw, observed=block, market_price=200, age_limit=5)
        assert result["age_at_parent_seconds"] == 0
    else:
        with pytest.raises(ValueError, match="disagreement_or_stale"):
            incident.weth_round(raw, observed=block, market_price=200, age_limit=5)


def test_fill_comparison_applies_usdc_mark_and_uses_signed_difference():
    def compare(paid):
        return incident.fill_comparison(
            paid=paid,
            received=10**18,
            stable_usd_e8=99000000,
            weth_usd_e8=200000000,
            stable_decimals=6,
        )

    assert compare(2000000) == {
        "stable_per_weth_e8_floor": 200000000,
        "marked_usd_per_weth_e8_floor": 198000000,
        "marked_difference_bps_floor": -100,
    }
    assert compare(3000000)["marked_difference_bps_floor"] == 4850
