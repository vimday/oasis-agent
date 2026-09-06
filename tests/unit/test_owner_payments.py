"""Synthetic exclusive-payment regressions, not live economic evidence."""

import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.owner_payments import (
    MODIFY,
    TRANSFER,
    OwnerBalanceCapture,
    UnsupportedOwnerPayment,
    balance_requests,
    persist_owner_payment,
    resolve_exclusive_owner_payment,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import RpcExchange
from tests.unit.test_position_dataset import CORE, OWNER, POSM
from tests.unit.test_position_settlement import capture_fixture, mutate


def fixture():
    core = capture_fixture(amounts=(-100, -200, 0, 0))
    tokens = (core.pool_key.currency0, core.pool_key.currency1)
    params = [encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [7, 20, 100, 200, b""])]
    params += [encode(["address"], [t]) for t in tokens]
    data = (
        "0x"
        + (
            MODIFY
            + encode(
                ["bytes", "uint256"],
                [
                    encode(["bytes", "bytes[]"], [b"\x00\x12\x12", params]),
                    core.header.ref.timestamp + 1,
                ],
            )
        ).hex()
    )
    logs = []
    for i, (token, amount) in enumerate(zip(tokens, [100, 200]), 1):
        log = dict(json.loads(core.exchanges[2].response_payload)["result"]["logs"][0])
        log.update(
            address=token,
            topics=[
                TRANSFER,
                "0x" + encode(["address"], [OWNER]).hex(),
                "0x" + encode(["address"], [CORE]).hex(),
            ],
            data="0x" + encode(["uint256"], [amount]).hex(),
            logIndex=hex(i),
        )
        logs.append(log)
    core = mutate(core, 2, lambda r: r["result"]["logs"].extend(logs))

    def trace(r):
        root = r["result"]
        root["input"] = data
        for token, amount in zip(tokens, [100, 200]):
            root["calls"].append(
                {
                    "type": "CALL",
                    "from": POSM,
                    "to": token,
                    "input": "0x23b872dd"
                    + encode(["address", "address", "uint256"], [OWNER, CORE, amount]).hex(),
                    "output": "0x" + encode(["bool"], [True]).hex(),
                }
            )

    core = mutate(core, 3, trace)
    hdr = json.loads(core.exchanges[1].response_payload)["result"]
    previous = dict(
        hdr,
        number=hex(core.header.ref.number - 1),
        hash=core.header.parent_hash,
        timestamp=hex(core.header.ref.timestamp - 1),
    )
    results = [hex(core.header.ref.chain_id), previous, hdr]
    for amount in (100, 200):
        results += [
            "0x6000",
            "0x6000",
            "0x" + encode(["uint8"], [18]).hex(),
            "0x" + encode(["uint256"], [1000]).hex(),
            "0x" + encode(["uint256"], [1000 - amount]).hex(),
        ]
    results += [logs, hdr]
    exchanges = tuple(
        RpcExchange(
            method,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": method, "params": params}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": result}),
            1,
        )
        for i, ((method, params), result) in enumerate(
            zip(balance_requests(core, OWNER), results), 1
        )
    )
    return OwnerBalanceCapture(core, OWNER, core.source, exchanges)


def test_actual_owner_debits_reconcile_all_three_sources(tmp_path):
    capture = fixture()
    amounts = resolve_exclusive_owner_payment(capture)
    assert (amounts.amount0, amounts.amount1) == (-100, -200)
    entry = persist_owner_payment(EvidenceStore(tmp_path / "evidence.db"), capture)
    assert (entry.owner_amount0, entry.owner_amount1) == (-100, -200)
    assert entry.settlement_evidence is not None


@pytest.mark.parametrize(
    "index,result", [(3, "0x6002"), (7, "0x" + encode(["uint256"], [901]).hex()), (13, [])]
)
def test_changed_code_balance_or_omitted_transfers_fail(index, result):
    capture = fixture()
    xs = list(capture.exchanges)
    response = json.loads(xs[index].response_payload)
    response["result"] = result
    xs[index] = replace(xs[index], response_payload=canonical_json(response))
    with pytest.raises(ValueError):
        replace(capture, exchanges=tuple(xs))


def test_router_cannot_be_labelled_owner():
    capture = fixture()
    core = mutate(capture.core, 3, lambda r: r["result"].update(input="0x12345678"))
    with pytest.raises(UnsupportedOwnerPayment):
        resolve_exclusive_owner_payment(replace(capture, core=core))


def test_token_false_return_rejected():
    capture = fixture()
    core = mutate(
        capture.core,
        3,
        lambda r: r["result"]["calls"][1].update(output="0x" + encode(["bool"], [False]).hex()),
    )
    with pytest.raises(ValueError, match="return true"):
        resolve_exclusive_owner_payment(replace(capture, core=core))


def test_pipeline_uses_owner_at_action_and_rejects_duplicate_payment(tmp_path):
    from oasis_agent.evidence.ingestion import persist_and_verify_block_range
    from oasis_agent.farming.position_pipeline import replay_captured_position_datasets
    from tests.rpc_evidence_helpers import range_exchanges
    from tests.unit.test_position_capture import snapshot_capture
    from tests.unit.test_position_dataset import scenario

    store = EvidenceStore(tmp_path / "pipeline.db")
    _, _, core, nft = scenario()
    verified = [
        persist_and_verify_block_range(store, d, range_exchanges(d), research_single_provider=True)
        for d in (core, nft)
    ]
    kwargs = {
        "opening": snapshot_capture(),
        "closing": snapshot_capture(closing=True),
        "pool_manager": CORE,
        "core": verified[0],
        "ownership": verified[1],
    }
    payment = fixture()
    report = replay_captured_position_datasets(store, **kwargs, payments=(payment,))
    assert report.position.ledger.settlement_complete
    assert report.position.ledger.cumulative_owner_amount0 == -100
    assert report.position.ledger.cumulative_owner_amount1 == -200
    # NFT moves after modification. Endpoint ownership must not rewrite the payer.
    assert report.position.ledger.closing.nft_owner != payment.owner
    with pytest.raises(ValueError, match="uniquely"):
        replay_captured_position_datasets(store, **kwargs, payments=(payment, payment))
    store.close()
