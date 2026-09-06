import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.farming.batch_settlement import (
    UnsupportedBatchAllocation,
    batch_owner_cash_payload,
    reconcile_batch_settlement,
    resolve_batch_owner_cash,
)
from oasis_agent.farming.owner_payments import TRANSFER, _log, _transfer
from oasis_agent.farming.position_settlement import MODIFY_SELECTOR, MODIFY_TYPES
from oasis_agent.farming.transaction_payments import (
    TransactionPaymentCapture,
    _scope,
    transaction_payment_requests,
)
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.models.v4 import PoolKey
from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.unit.test_position_dataset import CORE, OWNER, POSM
from tests.unit.test_position_settlement import mutate
from tests.unit.test_transaction_payments import fixture as payment_fixture

fixture = payment_fixture


def test_batch_core_cash_conserves_each_token():
    report = json.loads(reconcile_batch_settlement(fixture()))
    assert len(report["positions"]) == 1
    assert all(v == 0 for v in report["pool_manager_residual_by_token"].values())
    assert report["individual_owner_cash_allocation"] == "unresolved"


def test_core_claim_mismatch_cannot_pass_cash_reconciliation():
    capture = fixture()

    def change(r):
        frame = r["result"]["calls"][0]
        frame["output"] = (
            "0x" + b"".join(x.to_bytes(16, "big", signed=True) for x in (-99, -200, 0, 0)).hex()
        )

    core = mutate(capture.core, 3, change)
    with pytest.raises(ValueError, match="residual"):
        reconcile_batch_settlement(replace(capture, core=core))


def rebuild_payments(payments, core):
    """Rebuild the full payment witness after extending core receipt/trace."""
    tokens, accounts = _scope(core)
    transfers = [
        x
        for x in json.loads(core.exchanges[2].response_payload)["result"]["logs"]
        if x["topics"][0] == TRANSFER
    ]
    results = [json.loads(x.response_payload)["result"] for x in payments.exchanges[:3]]
    for token in tokens:
        results += ["0x6000", "0x6000", "0x" + encode(["uint8"], [18]).hex()]
        results += ["0x" + encode(["uint256"], [10000]).hex()] * 2
        for account in accounts:
            net = sum(
                (amount if recipient == account else 0) - (amount if sender == account else 0)
                for t, sender, recipient, amount in map(_transfer, map(_log, transfers))
                if t == token
            )
            results += ["0x" + encode(["uint256"], [v]).hex() for v in [1000, 1000 + net]]
    results += [transfers, results[2]]
    exchanges = tuple(
        RpcExchange(
            m,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": m, "params": p}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": r}),
            1,
        )
        for i, ((m, p), r) in enumerate(
            zip(transaction_payment_requests(core), results, strict=True), 1
        )
    )
    return TransactionPaymentCapture(core, exchanges)


def second_position(core, *, key, salt, params, amounts, paid):
    pool_b = pool_id_from_key(key)

    def extend_trace(r):
        r["result"]["calls"].append(
            {
                "type": "CALL",
                "from": POSM,
                "to": CORE,
                "input": "0x"
                + (
                    MODIFY_SELECTOR
                    + encode(
                        MODIFY_TYPES,
                        [
                            (key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks),
                            (params[0], params[1], params[2], salt.to_bytes(32, "big")),
                            b"",
                        ],
                    )
                ).hex(),
                "output": "0x" + b"".join(x.to_bytes(16, "big", signed=True) for x in amounts).hex(),
                "calls": [],
            }
        )

    def extend_receipt(r):
        template = r["result"]["logs"][0]
        r["result"]["logs"].append(
            dict(
                template,
                topics=[
                    MODIFY_LIQUIDITY_TOPIC,
                    pool_b,
                    "0x" + ("00" * 12 + POSM[2:]),
                ],
                data="0x"
                + b"".join(
                    x.to_bytes(32, "big", signed=True) for x in (params[0], params[1], params[2], salt)
                ).hex(),
                logIndex=hex(3),
            )
        )
        for i, (token, amount) in enumerate(zip((key.currency0, key.currency1), paid), 4):
            r["result"]["logs"].append(
                dict(
                    template,
                    address=token,
                    topics=[
                        TRANSFER,
                        "0x" + encode(["address"], [OWNER]).hex(),
                        "0x" + encode(["address"], [CORE]).hex(),
                    ],
                    data="0x" + encode(["uint256"], [amount]).hex(),
                    logIndex=hex(i),
                )
            )

    core = mutate(core, 3, extend_trace)
    return mutate(core, 2, extend_receipt)


def test_single_position_owner_cash_is_uniquely_attributed():
    allocations = resolve_batch_owner_cash(fixture(), OWNER)
    assert len(allocations) == 1
    (only,) = allocations
    assert (only.owner_amount0, only.owner_amount1) == (-100, -200)
    assert (only.fees_accrued0, only.fees_accrued1) == (0, 0)
    payload = json.loads(batch_owner_cash_payload(fixture(), OWNER))
    assert payload["physical_allocation"] == "unique_per_currency_per_pool"
    assert payload["execution_eligible"] is False


def test_two_pool_batch_attributes_each_position_physically():
    payments = payment_fixture()
    key_b = PoolKey("0x" + "88" * 20, "0x" + "99" * 20, 3000, 60, ZERO_ADDRESS)
    core = second_position(
        payments.core, key=key_b, salt=8, params=(-60, 60, 10), amounts=(-30, -40, 5, 6), paid=(30, 40)
    )
    allocations = resolve_batch_owner_cash(rebuild_payments(payments, core), OWNER)
    assert len(allocations) == 2
    by_id = {x.token_id: x for x in allocations}
    assert (by_id[7].owner_amount0, by_id[7].owner_amount1) == (-100, -200)
    assert (by_id[8].owner_amount0, by_id[8].owner_amount1) == (-30, -40)
    assert (by_id[8].fees_accrued0, by_id[8].fees_accrued1) == (5, 6)
    assert by_id[7].pool_id != by_id[8].pool_id
    payload = json.loads(batch_owner_cash_payload(rebuild_payments(payments, core), OWNER))
    assert len(payload["positions"]) == 2


def test_shared_token_pair_between_positions_fails_closed():
    payments = payment_fixture()
    core = second_position(
        payments.core,
        key=payments.core.pool_key,
        salt=8,
        params=(-60, 60, 10),
        amounts=(-30, -40, 5, 6),
        paid=(30, 40),
    )
    with pytest.raises(UnsupportedBatchAllocation, match="shared token pair"):
        resolve_batch_owner_cash(rebuild_payments(payments, core), OWNER)


def test_third_party_netting_fails_closed():
    payments = payment_fixture()
    third = "0x" + "aa" * 20
    template = json.loads(payments.core.exchanges[2].response_payload)["result"]["logs"][0]
    core = mutate(
        payments.core,
        2,
        lambda r: r["result"]["logs"].append(
            dict(
                template,
                topics=[
                    TRANSFER,
                    "0x" + encode(["address"], [CORE]).hex(),
                    "0x" + encode(["address"], [third]).hex(),
                ],
                data="0x" + encode(["uint256"], [50]).hex(),
                logIndex=hex(3),
            )
        ),
    )
    with pytest.raises(UnsupportedBatchAllocation, match="third-party"):
        resolve_batch_owner_cash(rebuild_payments(payments, core), OWNER)


def test_native_currency_batch_fails_closed():
    payments = payment_fixture()
    core = second_position(
        payments.core,
        key=PoolKey(ZERO_ADDRESS, "0x" + "99" * 20, 3000, 60, ZERO_ADDRESS),
        salt=8,
        params=(-60, 60, 10),
        amounts=(-30, -40, 5, 6),
        paid=(30, 40),
    )
    with pytest.raises(UnsupportedBatchAllocation, match="native currency"):
        resolve_batch_owner_cash(rebuild_payments(payments, core), OWNER)


def test_owner_flow_claim_residual_fails():
    payments = payment_fixture()

    def change(r):
        frame = r["result"]["calls"][0]
        frame["output"] = (
            "0x" + b"".join(x.to_bytes(16, "big", signed=True) for x in (-99, -200, 0, 0)).hex()
        )

    core = mutate(payments.core, 3, change)
    with pytest.raises(ValueError, match="residual"):
        resolve_batch_owner_cash(rebuild_payments(payments, core), OWNER)
