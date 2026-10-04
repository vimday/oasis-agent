import json
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.rpc_validation import restore_exchanges
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.account_gas_coverage import (
    AccountGasBlockInput,
    AccountGasCapture,
    AccountGasPlan,
    AccountGasRequest,
    replay_account_gas,
    retain_account_gas_block,
)
from oasis_agent.farming.price_evidence import persist_price_observation_evidence
from oasis_agent.farming.transaction_costs import TransactionCostCapture, persist_transaction_cost
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import ZERO_ADDRESS, BlockRef, canonical_json
from oasis_agent.rpc.transport import RpcExchange
from scripts.replay_account_gas import Request, run
from tests.unit.test_price_evidence import price
from tests.unit.test_transaction_costs import fixture

PROVIDERS = ("fixture-a", "fixture-b")


def exchange(method, params, result, index):
    return RpcExchange(
        method,
        index,
        canonical_json({"jsonrpc": "2.0", "id": index, "method": method, "params": params}),
        canonical_json({"jsonrpc": "2.0", "id": index, "result": result}),
        1,
    )


def inputs(store, chain=1, empty=False, template=None, tx_offset=0, providers=PROVIDERS):
    template = fixture(chain) if template is None else template
    payer = template.reconstruct().payer
    header = template.header
    opening = BlockSnapshot(
        BlockRef(chain, header.ref.number - 1, header.parent_hash, header.ref.timestamp - 12),
        "0x" + "01" * 32,
    )
    plan = AccountGasPlan(header, payer, providers, 2)
    costs, txs = [], []
    for index, status in enumerate([1, 0]):
        xs = list(template.exchanges)
        digest = "0x" + f"{33 + index + tx_offset:02x}" * 32
        for i, x in enumerate(xs):
            request, response = json.loads(x.request_payload), json.loads(x.response_payload)
            if x.method == "eth_getTransactionReceipt":
                request["params"] = [digest]
                response["result"].update(
                    transactionHash=digest,
                    transactionIndex=hex(index),
                    status=hex(status),
                    logs=[],
                    to="0x" + "33" * 20,
                )
            xs[i] = replace(
                x,
                request_payload=canonical_json(request),
                response_payload=canonical_json(response),
            )
        cost = replace(template, transaction_hash=digest, exchanges=tuple(xs))
        costs.append(cost)
        txs.append(
            {
                "hash": digest,
                "blockHash": header.ref.block_hash,
                "blockNumber": hex(header.ref.number),
                "transactionIndex": hex(index),
                "from": payer,
                "to": "0x" + "33" * 20,
                "type": "0x2",
                "input": "0x",
                "value": "0x0",
                "gas": "0x10000",
                "nonce": hex(index),
            }
        )
    if empty:
        costs, txs = [], []
    raw_header = json.loads(template.exchanges[1].response_payload)["result"]
    full = {**raw_header, "transactions": txs}
    close = {**raw_header, "transactions": [tx["hash"] for tx in txs]}
    results = [hex(chain), full, close]
    xs = tuple(
        exchange(m, p, r, i)
        for i, ((m, p), r) in enumerate(
            zip(plan.requests(), results, strict=True),
            1,
        )
    )
    capture = AccountGasCapture(plan, providers[0], xs)
    native = price(template, ZERO_ADDRESS, 2000 * 10**8)
    block = AccountGasBlockInput(
        plan,
        tuple(retain_account_gas_block(store, replace(capture, source=p)) for p in providers),
        tuple(
            tuple(persist_transaction_cost(store, replace(c, source=p)) for p in providers)
            for c in costs
        ),
        tuple(
            persist_price_observation_evidence(store, replace(native, source=p)) for p in providers
        ),
    )
    return AccountGasRequest(opening, header, payer, providers, native.policy, (block,)), capture


@pytest.mark.parametrize("chain,total", [(1, 400), (8453, 436), (42161, 400)])
def test_all_account_transactions_success_and_revert_are_priced_once(tmp_path, chain, total):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, _ = inputs(store, chain)
        result = replay_account_gas(store, request)
        assert result.total_native == total
        assert result.successful_native == result.reverted_native == total // 2
        assert result.total_usd_micros == 2  # ceil each tiny expense, never floor to zero
        assert [t["status"] for t in result.transactions] == [1, 0]
        assert result.net_pnl is None and not result.execution_eligible
        assert result.position_allocation == "unresolved"
    finally:
        store.close()


def test_empty_account_activity_requires_complete_block_and_prices(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, _ = inputs(store, empty=True)
        result = replay_account_gas(store, request)
        assert result.total_native == result.total_usd_micros == 0
        assert result.transactions == () and result.net_pnl is None
        bad = replace(request.blocks[0], native_prices=())
        with pytest.raises(ValueError, match="two-provider"):
            replay_account_gas(store, replace(request, blocks=(bad,)))
    finally:
        store.close()


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "extra",
        "reordered",
        "single",
        "foreign",
        "gap",
        "parent",
        "closing",
        "account",
        "price",
        "policy",
    ],
)
def test_incomplete_or_foreign_interval_never_becomes_zero(tmp_path, bad):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, _ = inputs(store)
        block = request.blocks[0]
        if bad == "missing":
            block = replace(block, costs=block.costs[:1])
        elif bad == "extra":
            block = replace(block, costs=(*block.costs, block.costs[0]))
        elif bad == "reordered":
            block = replace(block, costs=tuple(reversed(block.costs)))
        elif bad == "single":
            block = replace(block, blocks=block.blocks[:1])
        elif bad == "foreign":
            block = replace(block, costs=((block.costs[0][1], block.costs[0][0]), block.costs[1]))
        elif bad == "gap":
            request = replace(
                request,
                opening=replace(
                    request.opening,
                    ref=replace(request.opening.ref, number=request.opening.ref.number - 1),
                ),
            )
        elif bad == "parent":
            request = replace(
                request,
                opening=replace(
                    request.opening, ref=replace(request.opening.ref, block_hash="0x" + "ff" * 32)
                ),
            )
        elif bad == "closing":
            request = replace(request, closing=request.opening)
        elif bad == "account":
            request = replace(request, account="0x" + "44" * 20)
        elif bad == "price":
            block = replace(block, native_prices=block.native_prices[::-1])
        else:
            request = replace(
                request, native_policy=replace(request.native_policy, base_token="0x" + "55" * 20)
            )
        with pytest.raises(ValueError):
            replay_account_gas(store, replace(request, blocks=(block,)))
    finally:
        store.close()


@pytest.mark.parametrize("bad", ["sender", "index", "hash", "close", "numeric", "bound"])
def test_full_block_shape_and_enumeration_mutations_refused(tmp_path, bad):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        _, capture = inputs(store)
        xs = list(capture.exchanges)
        index = 2 if bad == "close" else 1
        response = json.loads(xs[index].response_payload)
        tx = response["result"]["transactions"][0]
        if bad == "sender":
            del tx["from"]
        elif bad == "index":
            tx["transactionIndex"] = "0x1"
        elif bad == "hash":
            tx["blockHash"] = "0x" + "ff" * 32
        elif bad == "close":
            response["result"]["transactions"].pop()
        elif bad == "numeric":
            x = xs[1]
            req = json.loads(x.request_payload)
            req.update(
                method="eth_getBlockByNumber", params=[hex(capture.plan.header.ref.number), True]
            )
            xs[1] = replace(x, method="eth_getBlockByNumber", request_payload=canonical_json(req))
        else:
            capture = replace(capture, plan=replace(capture.plan, max_account_transactions=1))
        xs[index] = replace(xs[index], response_payload=canonical_json(response))
        with pytest.raises(ValueError):
            replace(capture, exchanges=tuple(xs)).facts()
    finally:
        store.close()


def test_full_block_quorum_rejects_different_sender_selection(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, capture = inputs(store)
        xs = list(capture.exchanges)
        response = json.loads(xs[1].response_payload)
        response["result"]["transactions"][0]["from"] = "0x" + "66" * 20
        xs[1] = replace(xs[1], response_payload=canonical_json(response))
        changed = retain_account_gas_block(
            store, replace(capture, source=PROVIDERS[1], exchanges=tuple(xs))
        )
        block = replace(request.blocks[0], blocks=(request.blocks[0].blocks[0], changed))
        with pytest.raises(ValueError, match="disagree on full block"):
            replay_account_gas(store, replace(request, blocks=(block,)))
    finally:
        store.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("from", "0x" + "44" * 20),
        ("transactionIndex", "0x9"),
        ("to", "0x" + "77" * 20),
        ("type", "0x1"),
        ("gasUsed", "0x0"),
        ("gasUsed", "0x10001"),
        ("logs", [{}]),
    ],
)
def test_receipt_must_match_full_transaction_and_revert_logs(tmp_path, field, value):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, _ = inputs(store)
        block = request.blocks[0]
        pair = block.costs[1]
        forged = []
        for ref in pair:
            record = store.get_record(ref)
            xs = list(restore_exchanges(record.request_payload, record.raw_response_payload))
            response = json.loads(xs[2].response_payload)
            response["result"][field] = value
            xs[2] = replace(xs[2], response_payload=canonical_json(response))
            forged.append(
                persist_transaction_cost(
                    store,
                    TransactionCostCapture(
                        block.plan.header,
                        json.loads(record.payload)["cost"]["transaction_hash"],
                        ref.source,
                        tuple(xs),
                    ),
                )
            )
        block = replace(block, costs=(block.costs[0], tuple(forged)))
        with pytest.raises(ValueError, match="receipt sender/index/type/gas/logs"):
            replay_account_gas(store, replace(request, blocks=(block,)))
    finally:
        store.close()


def test_native_price_full_round_disagreement_is_not_hidden(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, _ = inputs(store)
        block = request.blocks[0]
        capture = price(fixture(), ZERO_ADDRESS, 2001 * 10**8)
        changed = persist_price_observation_evidence(store, replace(capture, source=PROVIDERS[1]))
        block = replace(block, native_prices=(block.native_prices[0], changed))
        with pytest.raises(ValueError, match="full round"):
            replay_account_gas(store, replace(request, blocks=(block,)))
    finally:
        store.close()


def test_two_contiguous_blocks_include_every_reverted_cost(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, _ = inputs(store)
        template = fixture()
        old = template.header
        header = BlockSnapshot(
            replace(
                old.ref,
                number=old.ref.number + 1,
                timestamp=old.ref.timestamp + 12,
                block_hash="0x" + "66" * 32,
            ),
            old.ref.block_hash,
        )
        xs = []
        for x in template.exchanges:
            req, resp = json.loads(x.request_payload), json.loads(x.response_payload)
            if x.method == "eth_getBlockByNumber":
                req["params"][0] = hex(header.ref.number)
                resp["result"].update(
                    number=hex(header.ref.number),
                    hash=header.ref.block_hash,
                    timestamp=hex(header.ref.timestamp),
                    parentHash=header.parent_hash,
                )
            elif x.method == "eth_getTransactionReceipt":
                resp["result"].update(
                    blockNumber=hex(header.ref.number), blockHash=header.ref.block_hash, logs=[]
                )
            xs.append(
                replace(
                    x, request_payload=canonical_json(req), response_payload=canonical_json(resp)
                )
            )
        later, _ = inputs(
            store, template=replace(template, header=header, exchanges=tuple(xs)), tx_offset=2
        )
        request = replace(request, closing=header, blocks=(*request.blocks, *later.blocks))
        result = replay_account_gas(store, request)
        assert len(result.transactions) == 4 and result.reverted_native == 400
        assert result.total_native == 800 and result.total_usd_micros == 4
        assert result.net_pnl is None
    finally:
        store.close()


@pytest.mark.parametrize("limit", [True, False, 0, 129, 1.5])
def test_scope_limit_must_be_bounded_strict_integer(tmp_path, limit):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, _ = inputs(store)
        with pytest.raises(ValueError):
            replace(request.blocks[0].plan, max_account_transactions=limit)
    finally:
        store.close()


def test_cli_exclusive_deterministic_reports_and_rejects_amount_override(tmp_path):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path)
    request, _ = inputs(store)
    store.close()
    source = tmp_path / "input.json"
    payload = {"schema": "oasis.account-gas-input.v1", "request": asdict(request)}
    source.write_bytes(canonical_json(payload))
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    run(source, path, first)
    run(source, path, second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(ValueError):
        run(source, path, first)
    payload["request"]["total_native"] = 0
    with pytest.raises(ValueError):
        Request.from_json(canonical_json(payload))


def test_forged_empty_account_facts_cannot_override_original_transactions(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request, _ = inputs(store)
        block = request.blocks[0]
        ref = block.blocks[0]
        record = store.get_record(ref)
        changed = json.loads(record.payload)
        changed["account_transactions"] = []
        forged = store.put(
            ref.kind,
            canonical_json(changed),
            ref.block,
            ref.source,
            request=json.loads(record.request_payload),
            raw_response=json.loads(record.raw_response_payload),
            interpretation=changed,
        )
        with pytest.raises(ValueError, match="facts differ"):
            replay_account_gas(
                store, replace(request, blocks=(replace(block, blocks=(forged, block.blocks[1])),))
            )
    finally:
        store.close()


def test_missing_target_is_not_assumed_contract_creation(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        _, capture = inputs(store)
        xs = list(capture.exchanges)
        response = json.loads(xs[1].response_payload)
        del response["result"]["transactions"][0]["to"]
        xs[1] = replace(xs[1], response_payload=canonical_json(response))
        with pytest.raises(ValueError, match="enumeration"):
            replace(capture, exchanges=tuple(xs)).facts()
    finally:
        store.close()
