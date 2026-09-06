import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.owner_payments import _log, _transfer
from oasis_agent.farming.transaction_payments import (
    TransactionPaymentCapture,
    _scope,
    persist_transaction_payments,
    replay_transaction_payments,
    transaction_payment_requests,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import RpcExchange
from tests.unit.test_owner_payments import fixture as owner_fixture


def fixture():
    owner = owner_fixture()
    core = owner.core
    tokens, accounts = _scope(core)
    logs = json.loads(owner.exchanges[-2].response_payload)["result"]
    results = [json.loads(x.response_payload)["result"] for x in owner.exchanges[:3]]
    for token in tokens:
        results += ["0x6000", "0x6000", "0x" + encode(["uint8"], [18]).hex()]
        results += ["0x" + encode(["uint256"], [10000]).hex()] * 2
        for account in accounts:
            net = sum(
                (amount if recipient == account else 0) - (amount if sender == account else 0)
                for t, sender, recipient, amount in map(_transfer, map(_log, logs))
                if t == token
            )
            results += ["0x" + encode(["uint256"], [v]).hex() for v in [1000, 1000 + net]]
    results += [logs, results[2]]
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


def test_all_accounts_close_per_token_and_roundtrip(tmp_path):
    capture = fixture()
    xs = capture.reconstruct()
    for token in _scope(capture.core)[0]:
        assert sum(x.net for x in xs if x.token == token) == 0
        assert sum(x.paid for x in xs if x.token == token) == sum(
            x.received for x in xs if x.token == token
        )
    store = EvidenceStore(tmp_path / "evidence.db")
    ref = persist_transaction_payments(store, capture)
    assert replay_transaction_payments(store, ref, capture.core) == capture
    assert json.loads(capture.payload)["position_cash_allocation"] == "unresolved"
    store.close()


@pytest.mark.parametrize(
    "index,result",
    [
        (3, "0x6001"),
        (7, "0x" + encode(["uint256"], [10001]).hex()),
        (9, "0x" + encode(["uint256"], [999]).hex()),
        (-2, []),
    ],
)
def test_malformed_balance_supply_code_or_logs_fail(index, result):
    c = fixture()
    xs = list(c.exchanges)
    raw = json.loads(xs[index].response_payload)
    raw["result"] = result
    xs[index] = replace(xs[index], response_payload=canonical_json(raw))
    with pytest.raises(ValueError):
        replace(c, exchanges=tuple(xs))
