import json
from dataclasses import replace

import pytest

from oasis_agent.evidence.rpc_validation import decode_exchange
from oasis_agent.farming.owner_payments import persist_owner_payment
from oasis_agent.farming.position_action_costs import ActionCostProof, bind_position_action_costs
from oasis_agent.farming.transaction_costs import TransactionCostCapture, persist_transaction_cost
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import RpcExchange
from scripts.verify_lp_action_costs import Request, verify
from tests.unit.test_cash_window import retained
from tests.unit.test_owner_payments import fixture


@pytest.fixture
def inputs(tmp_path):
    generator = retained.__wrapped__(tmp_path)
    store, request = next(generator)
    try:
        payment = fixture()
        xs = list(payment.core.exchanges)
        envelope = json.loads(xs[2].response_payload)
        envelope["result"].update(type="0x2", gasUsedForL1="0x0")
        xs[2] = replace(xs[2], response_payload=canonical_json(envelope))
        core = replace(payment.core, exchanges=tuple(xs))
        peers = ("rpc-a", "rpc-b")
        payment_refs = tuple(
            persist_owner_payment(
                store, replace(payment, source=s, core=replace(core, source=s))
            ).settlement_evidence
            for s in peers
        )
        request = replace(request, payments=(payment_refs,))
        yield store, request, core
    finally:
        generator.close()


def costs(store, core, *, change=None):
    receipt = dict(decode_exchange(core.exchanges[2])[1]["result"])
    if change:
        receipt.update(change)
    specs = [
        ("eth_chainId", [], hex(core.header.ref.chain_id)),
        (
            "eth_getBlockByNumber",
            [hex(core.header.ref.number), False],
            decode_exchange(core.exchanges[1])[1]["result"],
        ),
        ("eth_getTransactionReceipt", [core.entry.transaction_hash], receipt),
        (
            "eth_getBlockByNumber",
            [hex(core.header.ref.number), False],
            decode_exchange(core.exchanges[1])[1]["result"],
        ),
    ]
    xs = tuple(
        RpcExchange(
            m,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": m, "params": p}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": r}),
            1,
        )
        for i, (m, p, r) in enumerate(specs, 1)
    )
    refs = tuple(
        persist_transaction_cost(
            store, TransactionCostCapture(core.header, core.entry.transaction_hash, s, xs)
        )
        for s in ("rpc-a", "rpc-b")
    )
    return ActionCostProof(core.entry.transaction_hash, refs)


def test_cost_bound_to_action_time_owner_after_nft_transfer(inputs):
    store, request, core = inputs
    proof = costs(store, core)
    result = bind_position_action_costs(store, request, (proof,))
    assert len(result.actions) == 1
    action = result.actions[0]
    assert action.native_units == 200
    assert action.owner == fixture().owner
    assert action.evidence == (*proof.evidence, *request.payments[0])
    assert not result.execution_eligible
    assert result.scope == "successful_exclusive_owner_paid_actions_only"


@pytest.mark.parametrize(
    "change",
    [
        {"logs": []},
        {"from": "0x" + "99" * 20},
        {"status": "0x0"},
        {"gasUsed": "0x65"},
    ],
)
def test_matching_transaction_id_never_substitutes_for_same_full_receipt(inputs, change):
    store, request, core = inputs
    proof = costs(store, core, change=change)
    with pytest.raises(ValueError, match="receipt differs"):
        bind_position_action_costs(store, request, (proof,))


@pytest.mark.parametrize("case", ["missing", "duplicate", "extra", "provider"])
def test_complete_action_and_provider_scope_required(inputs, case):
    store, request, core = inputs
    proof = costs(store, core)
    proofs = (proof,)
    if case == "missing":
        proofs = ()
    elif case == "duplicate":
        proofs = (proof, proof)
    elif case == "extra":
        proofs = (replace(proof, transaction_hash="0x" + "ff" * 32),)
    else:
        proofs = (replace(proof, evidence=(proof.evidence[0], proof.evidence[0])),)
    with pytest.raises(ValueError):
        bind_position_action_costs(store, request, proofs)


def test_offline_cli_outputs_partial_scope_and_refusal_writes_nothing(inputs, tmp_path):
    store, request, core = inputs
    proof = costs(store, core)
    source = tmp_path / "input.json"
    declared = Request(
        **{"schema": "oasis.lp-action-cost-input.v1", "cash_window": request, "proofs": (proof,)}
    )
    source.write_text(declared.model_dump_json(by_alias=True))
    output = tmp_path / "output.json"
    verify(source, tmp_path / "cash.db", output)
    report = json.loads(output.read_bytes())
    assert report["actions"][0]["native_units"] == 200
    assert "full_cost_and_net_returns" in report["unresolved"]
    assert report["execution_eligible"] is False
    with pytest.raises(ValueError, match="new output"):
        verify(source, tmp_path / "cash.db", output)
    declared = declared.model_copy(update={"proofs": ()})
    source.write_text(declared.model_dump_json(by_alias=True))
    denied = tmp_path / "denied.json"
    with pytest.raises(ValueError, match="cover every action"):
        verify(source, tmp_path / "cash.db", denied)
    assert not denied.exists()
