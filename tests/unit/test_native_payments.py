"""Synthetic native+WETH/ERC-20 exclusive-payment regressions, not live evidence."""

import json
from dataclasses import replace
from typing import cast

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.native_payments import (
    NATIVE_DECIMALS,
    NativeOwnerPaymentCapture,
    UnsupportedOwnerPayment,
    native_balance_requests,
    persist_native_owner_payment,
    resolve_exclusive_native_payment,
)
from oasis_agent.farming.owner_payments import MODIFY, TRANSFER
from oasis_agent.farming.position_ledger import PositionLedgerEntry
from oasis_agent.farming.position_settlement import (
    MODIFY_SELECTOR,
    MODIFY_TYPES,
    PositionSettlementCapture,
    _text,
)
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC, decode_modify_liquidity
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.unit.test_position_dataset import CORE, OWNER, POSM, address_topic, scenario
from tests.unit.test_position_settlement import mutate


def q(value: int) -> str:
    """Canonical RPC hex quantity (no leading zeroes, 0x0 for zero)."""
    if value == 0:
        return "0x0"
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return "0x" + raw.hex().lstrip("0")


def native_capture_fixture(
    chain_id=4663,
    amounts=(-100, -200, 0, 0),
    l1_fee=None,
    liquidity_delta=20,
    currency0=ZERO_ADDRESS,
    currency1=None,
):
    """Core settlement capture for a native(0) + ERC-20 pool."""
    opening, _, dataset, _ = scenario()
    header = replace(dataset.blocks[-1], ref=replace(dataset.blocks[-1].ref, chain_id=chain_id))
    if currency1 is None:
        currency1 = opening.pool_key.currency1
    key = replace(opening.pool_key, currency0=currency0, currency1=currency1)
    pool_id = pool_id_from_key(key)
    log = replace(
        dataset.logs[0],
        topics=(MODIFY_LIQUIDITY_TOPIC, pool_id, address_topic(POSM)),
        data="0x"
        + encode(
            ["int24", "int24", "int256", "bytes32"],
            [-120, 120, liquidity_delta, (7).to_bytes(32, "big")],
        ).hex(),
    )
    entry = PositionLedgerEntry(
        header.ref, log.transaction_hash, 0, 0, decode_modify_liquidity(log)
    )
    data = (
        "0x"
        + (
            MODIFY_SELECTOR
            + encode(
                MODIFY_TYPES,
                [
                    (key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks),
                    (-120, 120, liquidity_delta, (7).to_bytes(32, "big")),
                    b"",
                ],
            )
        ).hex()
    )
    packed = b"".join(x.to_bytes(16, "big", signed=True) for x in amounts)
    frame = {
        "type": "CALL",
        "from": POSM,
        "to": CORE,
        "input": data,
        "output": "0x" + packed.hex(),
        "calls": [],
    }
    trace = {
        "type": "CALL",
        "from": OWNER,
        "to": POSM,
        "input": "0x12345678",
        "output": "0x",
        "calls": [frame],
    }
    receipt = {
        "transactionHash": entry.transaction_hash,
        "blockHash": header.ref.block_hash,
        "blockNumber": hex(header.ref.number),
        "transactionIndex": "0x0",
        "status": "0x1",
        "from": OWNER,
        "to": POSM,
        "type": "0x2",
        "gasUsedForL1": "0x0",
        "gasUsed": "0x64",
        "effectiveGasPrice": "0x2",
        "logs": [
            {
                "address": CORE,
                "topics": list(log.topics),
                "data": log.data,
                "blockNumber": hex(log.block_number),
                "blockHash": log.block_hash,
                "transactionHash": log.transaction_hash,
                "transactionIndex": "0x0",
                "logIndex": "0x0",
                "removed": False,
            }
        ],
    }
    if l1_fee is not None:
        receipt["l1Fee"] = q(l1_fee)
    hdr = {
        "number": hex(header.ref.number),
        "hash": header.ref.block_hash,
        "parentHash": header.parent_hash,
        "timestamp": hex(header.ref.timestamp),
    }
    pin = {"blockHash": header.ref.block_hash, "requireCanonical": True}
    specs = [
        ("eth_chainId", [], hex(chain_id)),
        ("eth_getBlockByNumber", [hex(header.ref.number), False], hdr),
        ("eth_getTransactionReceipt", [entry.transaction_hash], receipt),
        (
            "debug_traceTransaction",
            [
                entry.transaction_hash,
                {
                    "tracer": "callTracer",
                    "timeout": "10s",
                    "tracerConfig": {"onlyTopCall": False},
                },
            ],
            trace,
        ),
        ("eth_getCode", [CORE, pin], "0x6000"),
        ("eth_getCode", [POSM, pin], "0x6001"),
        ("eth_getBlockByNumber", [hex(header.ref.number), False], hdr),
    ]
    exchanges = tuple(
        RpcExchange(
            method,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": method, "params": params}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": result}),
            1,
        )
        for i, (method, params, result) in enumerate(specs, 1)
    )
    return PositionSettlementCapture("synthetic-rpc", header, CORE, key, entry, exchanges)


def native_fixture(
    chain_id=4663,
    amounts=(-100, -200, 0, 0),
    native=(1000, -300),
    l1_fee=None,
    trace_hook=None,
    operator_fee=0,
):
    """Full native owner payment capture: owner pays ETH and ERC-20 debit.

    ``native=(before, raw_delta)`` pins the owner balance; the default -300
    matches paying 100 settlement plus 200 gas. ``trace_hook`` mutates the
    trace root after default wiring to inject failure cases.
    """
    liquidity_delta = -20 if amounts[0] > 0 else 20
    core = native_capture_fixture(chain_id, amounts, l1_fee=l1_fee, liquidity_delta=liquidity_delta)
    erc20 = core.pool_key.currency1
    settle = -amounts[0] if amounts[0] < 0 else 0
    root_value = settle
    take = max(0, amounts[0])
    before, raw_delta = native

    def wire_trace(r):
        root = r["result"]
        root["input"] = (
            "0x"
            + (
                MODIFY
                + encode(
                    ["bytes", "uint256"],
                    [
                        encode(
                            ["bytes", "bytes[]"],
                            [
                                b"\x00\x12\x12" if amounts[0] < 0 else b"\x01\x12\x12",
                                [
                                    encode(
                                        ["uint256", "uint256", "uint128", "uint128", "bytes"],
                                        [7, 20, 100, 200, b""],
                                    ),
                                    encode(["address"], [ZERO_ADDRESS]),
                                    encode(["address"], [erc20]),
                                ],
                            ],
                        ),
                        core.header.ref.timestamp + 1,
                    ],
                )
            ).hex()
        )
        if root_value:
            root["value"] = q(root_value)
            root["calls"].append(
                {
                    "type": "CALL",
                    "from": POSM,
                    "to": CORE,
                    "input": "0x",
                    "output": "0x",
                    "value": q(root_value),
                    "calls": [],
                }
            )
        if take:
            root["calls"].append(
                {
                    "type": "CALL",
                    "from": POSM,
                    "to": CORE,
                    "input": "0x",
                    "output": "0x",
                    "calls": [
                        {
                            "type": "CALL",
                            "from": CORE,
                            "to": OWNER,
                            "input": "0x",
                            "output": "0x",
                            "value": q(take),
                            "calls": [],
                        }
                    ],
                }
            )
        erc_amount = abs(amounts[1])
        if amounts[1] < 0:
            root["calls"].append(
                {
                    "type": "CALL",
                    "from": POSM,
                    "to": CORE,
                    "input": "0x",
                    "output": "0x",
                    "calls": [
                        {
                            "type": "CALL",
                            "from": CORE,
                            "to": erc20,
                            "input": "0x23b872dd"
                            + encode(
                                ["address", "address", "uint256"], [OWNER, CORE, erc_amount]
                            ).hex(),
                            "output": "0x" + encode(["bool"], [True]).hex(),
                            "calls": [],
                        }
                    ],
                }
            )
        else:
            root["calls"].append(
                {
                    "type": "CALL",
                    "from": POSM,
                    "to": CORE,
                    "input": "0x",
                    "output": "0x",
                    "calls": [
                        {
                            "type": "CALL",
                            "from": CORE,
                            "to": erc20,
                            "input": "0xa9059cbb"
                            + encode(["address", "uint256"], [OWNER, erc_amount]).hex(),
                            "output": "0x" + encode(["bool"], [True]).hex(),
                            "calls": [],
                        }
                    ],
                }
            )

    core = mutate(core, 3, wire_trace)
    if trace_hook is not None:
        core = mutate(core, 3, lambda r: trace_hook(r["result"]))

    erc_logs = []
    log_template = dict(json.loads(core.exchanges[2].response_payload)["result"]["logs"][0])
    erc_log = dict(log_template)
    if amounts[1] < 0:
        erc_sender, erc_recipient = OWNER, CORE
    else:
        erc_sender, erc_recipient = CORE, OWNER
    erc_log.update(
        address=erc20,
        topics=[
            TRANSFER,
            "0x" + encode(["address"], [erc_sender]).hex(),
            "0x" + encode(["address"], [erc_recipient]).hex(),
        ],
        data="0x" + encode(["uint256"], [abs(amounts[1])]).hex(),
        logIndex="0x1",
    )
    erc_logs.append(erc_log)
    core = mutate(core, 2, lambda r: r["result"]["logs"].extend(erc_logs))

    hdr = json.loads(core.exchanges[1].response_payload)["result"]
    previous = dict(
        hdr,
        number=hex(core.header.ref.number - 1),
        hash=core.header.parent_hash,
        timestamp=hex(core.header.ref.timestamp - 1),
    )
    results = [hex(chain_id), previous, hdr]
    for token in (core.pool_key.currency0, core.pool_key.currency1):
        if token == ZERO_ADDRESS:
            results += [
                q(before),
                q(before + raw_delta),
            ]
        else:
            results += [
                "0x6000",
                "0x6000",
                "0x" + encode(["uint8"], [18]).hex(),
                "0x" + encode(["uint256"], [1000]).hex(),
                "0x" + encode(["uint256"], [1000 + amounts[1]]).hex(),
            ]
    results.append(erc_logs)
    if chain_id in (8453, 84532):
        results += ["0x6000", "0x" + encode(["uint256"], [operator_fee]).hex()]
    results.append(hdr)
    exchanges = tuple(
        RpcExchange(
            method,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": method, "params": params}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": result}),
            1,
        )
        for i, ((method, params), result) in enumerate(
            zip(native_balance_requests(core, OWNER), results, strict=True), 1
        )
    )
    return NativeOwnerPaymentCapture(core, OWNER, core.source, exchanges)


def test_native_debit_reconciles_balance_gas_and_trace(tmp_path):
    capture = native_fixture(amounts=(-100, -200, 0, 0), native=(1000, -300))
    amounts = resolve_exclusive_native_payment(capture)
    assert (amounts.amount0, amounts.amount1) == (-100, -200)
    assert amounts.token0.token == ZERO_ADDRESS
    assert amounts.token0.decimals == NATIVE_DECIMALS
    entry = persist_native_owner_payment(EvidenceStore(tmp_path / "evidence.db"), capture)
    assert (entry.owner_amount0, entry.owner_amount1) == (-100, -200)
    assert entry.settlement_evidence is not None


def test_native_credit_reconciles_take_refund(tmp_path):
    capture = native_fixture(amounts=(50, 40, 0, 0), native=(1000, -150))
    amounts = resolve_exclusive_native_payment(capture)
    assert (amounts.amount0, amounts.amount1) == (50, 40)


def test_op_l1_fee_is_included_in_gas_debit(tmp_path):
    # OP Stack charges l1Fee on top of the L2 execution fee; the owner balance
    # delta must include both before native settlement is recovered.
    capture = native_fixture(
        chain_id=8453, amounts=(-100, -200, 0, 0), native=(1000, -350), l1_fee=50
    )
    amounts = resolve_exclusive_native_payment(capture)
    assert (amounts.amount0, amounts.amount1) == (-100, -200)


def test_unknown_chain_gas_policy_fails_closed():
    with pytest.raises(ValueError, match="unknown chain gas policy"):
        native_fixture(chain_id=56, amounts=(-100, -200, 0, 0), native=(1000, -300))


def test_native_module_requires_exactly_one_native_currency():
    core = native_capture_fixture(currency0="0x" + "05" * 20)
    with pytest.raises(UnsupportedOwnerPayment, match="exactly one native"):
        native_balance_requests(core, OWNER)


def test_weth_pool_stays_on_erc20_attribution_path():
    # A WETH pool (WETH is a plain ERC-20, no address(0) currency) is out of
    # scope for the native module and stays on the ERC-20 Transfer path, which
    # does not special-case WETH.
    from oasis_agent.farming.owner_payments import balance_requests

    weth = "0x4200000000000000000000000000000000000006"
    core = native_capture_fixture(currency0="0x" + "05" * 20, currency1=weth)
    with pytest.raises(UnsupportedOwnerPayment, match="exactly one native"):
        native_balance_requests(core, OWNER)
    # The ERC-20 path builds balanceOf/Transfer witness requests for any token,
    # including WETH; it needs no native-specific handling.
    requests = balance_requests(core, OWNER)
    methods = [method for method, _ in requests]
    assert methods.count("eth_getCode") == 4  # two pins for two tokens
    assert methods.count("eth_getLogs") == 1
    assert any(
        method == "eth_call"
        and params[0]["to"] == core.pool_key.currency1
        and params[0]["data"].startswith("0x70a08231")
        for method, params in requests
    )


def test_gas_not_deducted_from_owner_balance_fails():
    with pytest.raises(ValueError, match="residual"):
        native_fixture(amounts=(-100, -200, 0, 0), native=(1000, -100))


def test_excess_root_value_without_settle_frame_fails():
    def hook(root):
        root["value"] = q(50)

    with pytest.raises(ValueError, match="settle frame"):
        native_fixture(amounts=(50, 40, 0, 0), native=(1000, -150), trace_hook=hook)


def test_third_party_native_value_flow_fails():
    def hook(root):
        root["calls"].append(
            {
                "type": "CALL",
                "from": POSM,
                "to": "0x" + "99" * 20,
                "input": "0x",
                "output": "0x",
                "value": q(1),
                "calls": [],
            }
        )

    with pytest.raises(ValueError, match="third-party"):
        native_fixture(amounts=(-100, -200, 0, 0), native=(1000, -300), trace_hook=hook)


def test_settle_and_take_in_same_action_fails():
    def hook(root):
        root["calls"].append(
            {
                "type": "CALL",
                "from": POSM,
                "to": CORE,
                "input": "0x",
                "output": "0x",
                "calls": [
                    {
                        "type": "CALL",
                        "from": CORE,
                        "to": OWNER,
                        "input": "0x",
                        "output": "0x",
                        "value": q(50),
                        "calls": [],
                    }
                ],
            }
        )

    with pytest.raises(ValueError, match="settle and take"):
        native_fixture(amounts=(-100, -200, 0, 0), native=(1000, -300), trace_hook=hook)


@pytest.mark.parametrize(
    "index,result",
    [
        (5, "0x6002"),  # ERC-20 runtime changed
        (8, "0x" + encode(["uint256"], [901]).hex()),  # balance mismatch
        (10, []),  # omitted full-block Transfer logs
    ],
)
def test_changed_code_balance_or_omitted_transfers_fail(index, result):
    capture = native_fixture(amounts=(-100, -200, 0, 0), native=(1000, -300))
    xs = list(capture.exchanges)
    response = json.loads(xs[index].response_payload)
    response["result"] = result
    xs[index] = replace(xs[index], response_payload=canonical_json(response))
    with pytest.raises(ValueError):
        replace(capture, exchanges=tuple(xs))


def transfer_frame(root: dict, erc20: str) -> dict:
    """Locate the ERC-20 transfer CALL frame anywhere in the trace."""
    stack: list[dict] = [root]
    while stack:
        frame = stack.pop()
        stack.extend(cast(list[dict], frame.get("calls", [])))
        data = bytes.fromhex(_text(frame.get("input", "0x")).removeprefix("0x"))
        if frame.get("to") == erc20 and data[:4].hex() in {"a9059cbb", "23b872dd"}:
            return frame
    raise AssertionError("erc transfer frame missing")


def test_router_cannot_be_labelled_owner():
    capture = native_fixture(amounts=(-100, -200, 0, 0), native=(1000, -300))
    core = mutate(capture.core, 3, lambda r: r["result"].update(input="0x12345678"))
    with pytest.raises(UnsupportedOwnerPayment):
        resolve_exclusive_native_payment(replace(capture, core=core))


def test_erc20_false_return_rejected():
    capture = native_fixture(amounts=(-100, -200, 0, 0), native=(1000, -300))
    erc20 = capture.core.pool_key.currency1
    core = mutate(
        capture.core,
        3,
        lambda r: transfer_frame(r["result"], erc20).update(
            output="0x" + encode(["bool"], [False]).hex()
        ),
    )
    with pytest.raises(ValueError, match="return true"):
        resolve_exclusive_native_payment(replace(capture, core=core))


def test_duplicate_native_settle_frames_fail_closed():
    from oasis_agent.farming.native_payments import _native_value_frames

    settle = {"type": "CALL", "from": POSM, "to": CORE, "value": "0x64"}
    root = {
        "type": "CALL",
        "from": OWNER,
        "to": POSM,
        "value": "0x64",
        "calls": [settle, dict(settle)],
    }
    with pytest.raises(UnsupportedOwnerPayment, match="duplicate"):
        _native_value_frames(root, OWNER, CORE, POSM)


def test_native_blob_gas_is_not_silently_omitted():
    from oasis_agent.farming.native_payments import _gas_debit

    receipt = {
        "type": "0x3",
        "gasUsed": "0x64",
        "effectiveGasPrice": "0x2",
        "blobGasUsed": hex(131072),
        "blobGasPrice": "0x3",
    }
    assert _gas_debit(receipt, 1) == 200 + 131072 * 3


def test_op_native_requires_operator_fee_evidence():
    from oasis_agent.farming.native_payments import _gas_debit

    receipt = {"type": "0x2", "gasUsed": "0x64", "effectiveGasPrice": "0x2", "l1Fee": "0x32"}
    with pytest.raises(ValueError, match="operator"):
        _gas_debit(receipt, 8453)


def test_op_operator_fee_reconciles_owner_cash():
    capture = native_fixture(chain_id=8453, native=(1000, -367), l1_fee=50, operator_fee=17)
    amounts = resolve_exclusive_native_payment(capture)
    assert (amounts.amount0, amounts.amount1) == (-100, -200)


def test_missing_operator_oracle_runtime_rejected():
    capture = native_fixture(chain_id=8453, native=(1000, -367), l1_fee=50, operator_fee=17)
    exchanges = list(capture.exchanges)
    raw = json.loads(exchanges[-3].response_payload)
    raw["result"] = "0x"
    exchanges[-3] = replace(exchanges[-3], response_payload=canonical_json(raw))
    with pytest.raises(ValueError, match="oracle code missing"):
        replace(capture, exchanges=tuple(exchanges))


def test_robinhood_testnet_uses_explicit_nitro_cost_policy():
    capture = native_fixture(chain_id=46630, amounts=(-100, -200, 0, 0), native=(1000, -300))
    amounts = resolve_exclusive_native_payment(capture)
    assert (amounts.amount0, amounts.amount1) == (-100, -200)
