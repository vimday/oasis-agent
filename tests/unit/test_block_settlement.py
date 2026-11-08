"""One account's block settlement: exact pinned reads, complete Transfer logs, replay.

Synthetic RPC answers only, built from the module's own request sequence. No network.
"""

import json
from dataclasses import replace
from typing import Any

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.block_settlement import (
    BlockSettlementCapture,
    collect_block_settlement_quorum,
    persist_block_settlement,
    replay_block_settlement,
    settlement_requests,
    transaction_settlement,
)
from oasis_agent.farming.owner_payments import TRANSFER
from oasis_agent.launch.spot_cost_capture import FROZEN_SCHEMA, frozen_sessions
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef, canonical_json
from oasis_agent.rpc.transport import RpcExchange

BLOCK = "0x" + "b1" * 32
PARENT = "0x" + "a0" * 32
GRANDPARENT = "0x" + "9f" * 32
HEADER = BlockSnapshot(BlockRef(8453, 123, BLOCK, 1_800_000_000), PARENT)
WALLET = "0x" + "e6" * 20
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOKEN = "0x" + "ab" * 20
ROUTER = "0x" + "70" * 20
OTHER = "0x" + "51" * 20
TX = "0x" + "cd" * 32
TX2 = "0x" + "ce" * 32
ZERO = "0x" + "00" * 20
TOKENS = (USDC, TOKEN)  # sorted: 0x8335... < 0xabab...


def word(value: int) -> str:
    return "0x" + encode(["uint256"], [value]).hex()


def topic(address: str) -> str:
    return "0x" + "00" * 12 + address.removeprefix("0x")


def log(
    token: str, sender: str, recipient: str, amount: int, index: int, *, tx: str = TX, pos: int = 7
) -> dict[str, Any]:
    return {
        "address": token,
        "topics": [TRANSFER, topic(sender), topic(recipient)],
        "data": word(amount),
        "blockNumber": hex(123),
        "blockHash": BLOCK,
        "transactionHash": tx,
        "transactionIndex": hex(pos),
        "logIndex": hex(index),
        "removed": False,
    }


def swap_logs() -> list[dict[str, Any]]:
    """A buy: the wallet pays 100 USDC units and receives 2_000 token units in TX."""
    return [log(USDC, WALLET, ROUTER, 100, 3), log(TOKEN, ROUTER, WALLET, 2_000, 4)]


def header_json(number: int, block: str, parent: str, timestamp: int) -> dict[str, Any]:
    return {"number": hex(number), "hash": block, "parentHash": parent, "timestamp": hex(timestamp)}


class Chain:
    """Answers for every request in the exact sequence, with explicit balances."""

    def __init__(
        self,
        logs: list[dict[str, Any]] | None = None,
        *,
        account: str = WALLET,
        tokens: tuple[str, ...] = TOKENS,
        header: BlockSnapshot = HEADER,
        **change: Any,
    ) -> None:
        self.account, self.tokens, self.header = account, tokens, header
        self.logs = swap_logs() if logs is None else logs
        self.native = change.pop("native", (10**18, 10**18 - 5_000))
        self.balances = change.pop("balances", {USDC: (1_000, 900), TOKEN: (0, 2_000)})
        self.supply = change.pop("supply", {USDC: (10**12, 10**12), TOKEN: (10**9, 10**9)})
        self.code = change.pop("code", {USDC: ("0x6001", "0x6001"), TOKEN: ("0x6002", "0x6002")})
        self.decimals = change.pop("decimals", {USDC: 6, TOKEN: 18})
        self.override: dict[int, object] = change.pop("override", {})
        assert not change

    def results(self) -> list[object]:
        out: list[object] = [
            hex(8453),
            header_json(122, PARENT, GRANDPARENT, 1_799_999_998),
            header_json(123, BLOCK, PARENT, 1_800_000_000),
            hex(self.native[0]),
            hex(self.native[1]),
        ]
        for token in self.tokens:
            out += list(self.code[token])
            out.append(word(self.decimals[token]))
            out += [word(v) for v in self.supply[token]]
            out += [word(v) for v in self.balances[token]]
        out += [self.logs, header_json(123, BLOCK, PARENT, 1_800_000_000)]
        for index, value in self.override.items():
            out[index] = value
        return out

    def exchanges(self) -> tuple[RpcExchange, ...]:
        specs = settlement_requests(self.header, self.account, self.tokens)
        return tuple(
            RpcExchange(
                m,
                i,
                canonical_json({"jsonrpc": "2.0", "id": i, "method": m, "params": p}),
                canonical_json({"jsonrpc": "2.0", "id": i, "result": r}),
                1,
            )
            for i, ((m, p), r) in enumerate(zip(specs, self.results(), strict=True), 1)
        )

    def capture(self, source: str = "alchemy") -> BlockSettlementCapture:
        return BlockSettlementCapture(
            source, self.header, self.account, self.tokens, self.exchanges()
        )


def test_requests_pin_every_state_read_to_the_parent_or_the_block() -> None:
    specs = settlement_requests(HEADER, WALLET, TOKENS)
    pins = [p[-1] for m, p in specs if m in ("eth_getBalance", "eth_getCode", "eth_call")]
    assert {json.dumps(p, sort_keys=True) for p in pins} == {
        json.dumps({"blockHash": h, "requireCanonical": True}, sort_keys=True)
        for h in (PARENT, BLOCK)
    }
    (logs,) = [p for m, p in specs if m == "eth_getLogs"]
    assert logs == [{"address": list(TOKENS), "blockHash": BLOCK, "topics": [TRANSFER]}]
    assert specs[-1] == specs[2] == ("eth_getBlockByNumber", [hex(123), False])


def test_a_standard_swap_reconciles_with_zero_residual_and_round_trips(tmp_path: Any) -> None:
    capture = Chain().capture()
    view = transaction_settlement(capture.reconstruct(), TX)
    usdc, token = view.tokens
    assert (usdc.balance_delta, usdc.block_transfer_net, usdc.residual) == (-100, -100, 0)
    assert (token.balance_delta, token.transaction_net, token.residual) == (2_000, 2_000, 0)
    assert (usdc.supply_residual, token.supply_residual) == (0, 0)
    assert (usdc.other_transaction_net, token.other_transaction_net) == (0, 0)
    assert view.transaction_index == 7 and view.native_delta == -5_000
    assert (usdc.decimals, token.decimals) == (6, 18)
    store = EvidenceStore(tmp_path / "e.db")
    ref = persist_block_settlement(store, capture)
    again = replay_block_settlement(store, ref, header=HEADER, account=WALLET, tokens=TOKENS)
    assert again.reconstruct() == capture.reconstruct()
    store.close()


def test_balance_moved_without_events_is_a_stated_residual_not_a_refusal() -> None:
    # Reflection/rebase: the token balance rose 3 units more than its Transfer logs say.
    chain = Chain(balances={USDC: (1_000, 900), TOKEN: (0, 2_003)})
    _, token = transaction_settlement(chain.capture().reconstruct(), TX).tokens
    assert (token.balance_delta, token.block_transfer_net, token.residual) == (2_003, 2_000, 3)


def test_other_transactions_in_the_block_are_split_out_and_supply_is_checked() -> None:
    logs = [
        log(USDC, OTHER, WALLET, 40, 1, tx=TX2, pos=2),  # someone pays the wallet earlier
        *swap_logs(),
        log(TOKEN, ZERO, OTHER, 5, 9, tx=TX2, pos=9) | {"transactionHash": "0x" + "cf" * 32},
    ]
    chain = Chain(
        logs,
        balances={USDC: (1_000, 940), TOKEN: (0, 2_000)},
        supply={USDC: (10**12, 10**12), TOKEN: (10**9, 10**9 + 5)},
    )
    usdc, token = transaction_settlement(chain.capture().reconstruct(), TX).tokens
    assert (usdc.transaction_net, usdc.other_transaction_net, usdc.residual) == (-100, 40, 0)
    assert (token.block_mint_minus_burn, token.supply_residual) == (5, 0)


def test_a_transaction_with_no_scoped_log_has_no_position() -> None:
    view = transaction_settlement(Chain().capture().reconstruct(), "0x" + "99" * 32)
    assert view.transaction_index is None
    assert all(t.transaction_logs == 0 and t.transaction_net == 0 for t in view.tokens)


def results_with(index: int, value: object) -> Chain:
    return Chain(override={index: value})


# Result positions in the exact sequence (two tokens, 7 reads each, after 5 head reads).
USDC_CODE_AFTER, TOKEN_DECIMALS, TOKEN_BALANCE_AFTER = 6, 14, 18
LOGS, CLOSING = 19, 20


@pytest.mark.parametrize(
    "chain",
    [
        # Chain, parent or block identity is wrong, or the block moved before the close.
        results_with(0, hex(84532)),
        results_with(1, header_json(122, "0x" + "77" * 32, GRANDPARENT, 1_799_999_998)),
        results_with(CLOSING, header_json(123, "0x" + "b2" * 32, PARENT, 1_800_000_000)),
        # Runtime absent or changed within the block; a codeless address answers "0x".
        results_with(USDC_CODE_AFTER, "0x6009"),
        Chain(code={USDC: ("0x", "0x"), TOKEN: ("0x6002", "0x6002")}),
        Chain(code={USDC: ("0x60 01", "0x60 01"), TOKEN: ("0x6002", "0x6002")}),
        results_with(TOKEN_BALANCE_AFTER, "0x"),
        results_with(TOKEN_DECIMALS, word(256)),
        results_with(3, "0x00"),  # noncanonical native quantity
        # Logs: missing, unordered, duplicated, removed, foreign block/token/topic.
        results_with(LOGS, None),
        Chain(list(reversed(swap_logs()))),
        Chain([*swap_logs(), swap_logs()[1]]),
        Chain([swap_logs()[0], swap_logs()[1] | {"removed": True}]),
        Chain([swap_logs()[0], swap_logs()[1] | {"blockHash": "0x" + "b2" * 32}]),
        Chain([swap_logs()[0], swap_logs()[1] | {"address": OTHER}]),
        Chain([swap_logs()[0], swap_logs()[1] | {"topics": ["0x" + "ee" * 32]}]),
        # Positions: missing, contradicting log order, one tx at two places, two txs at one.
        Chain(
            [swap_logs()[0], {k: v for k, v in swap_logs()[1].items() if k != "transactionIndex"}]
        ),
        # A later log index at an earlier position (two distinct transactions).
        Chain([log(USDC, OTHER, WALLET, 1, 3, tx=TX2, pos=8), *swap_logs()[1:]]),
        Chain([swap_logs()[0], log(TOKEN, ROUTER, WALLET, 2_000, 4, pos=8)]),
        Chain([swap_logs()[0], log(TOKEN, ROUTER, WALLET, 2_000, 4, tx=TX2)]),
        # A Transfer-topic log of a scoped token that is not a standard ERC-20 Transfer.
        Chain([swap_logs()[0], swap_logs()[1] | {"data": "0x01"}]),
        Chain(
            [swap_logs()[0], swap_logs()[1] | {"topics": [*swap_logs()[1]["topics"], topic(OTHER)]}]
        ),
    ],
)
def test_incoherent_or_incomplete_answers_are_refused(chain: Chain) -> None:
    with pytest.raises((ValueError, TypeError)):
        chain.capture()


def test_scope_and_wire_shape_are_refused_before_any_value() -> None:
    exchanges = Chain().exchanges()
    with pytest.raises(ValueError):
        BlockSettlementCapture("alchemy", HEADER, WALLET, (TOKEN, USDC), exchanges)  # unsorted
    with pytest.raises(ValueError):
        BlockSettlementCapture("alchemy", HEADER, ZERO, TOKENS, exchanges)
    with pytest.raises(ValueError):
        BlockSettlementCapture("alchemy", HEADER, WALLET, TOKENS, exchanges[:-1])
    swapped = (exchanges[1], exchanges[0], *exchanges[2:])
    with pytest.raises(ValueError):
        BlockSettlementCapture("alchemy", HEADER, WALLET, TOKENS, swapped)
    genesis = BlockSnapshot(BlockRef(8453, 0, BLOCK, 1), PARENT)
    with pytest.raises(ValueError):
        settlement_requests(genesis, WALLET, TOKENS)


def frozen(source: str, chain: Chain) -> dict[str, Any]:
    specs = settlement_requests(chain.header, chain.account, chain.tokens)
    seen: dict[str, object] = {}
    for (method, params), result in zip(specs, chain.results(), strict=True):
        seen.setdefault(json.dumps([method, params]), result)  # the header read repeats
    return {
        "schema": FROZEN_SCHEMA,
        "source": source,
        "chain_id": 8453,
        "responses": [
            {"method": json.loads(k)[0], "params": json.loads(k)[1], "result": r}
            for k, r in seen.items()
        ],
    }


def quorum(first: Chain, second: Chain) -> Any:
    sessions = frozen_sessions(
        {"alchemy": frozen("alchemy", first), "drpc": frozen("drpc", second)}, chain_id=8453
    )
    return collect_block_settlement_quorum(
        HEADER, WALLET, TOKENS, sources=("alchemy", "drpc"), sessions=sessions
    )


def test_two_providers_must_agree_on_every_value_and_every_log_row() -> None:
    a, b = quorum(Chain(), Chain())
    assert (a.source, b.source) == ("alchemy", "drpc") and a.reconstruct() == b.reconstruct()
    usdc, token = swap_logs()
    split = [usdc, log(TOKEN, ROUTER, WALLET, 1_999, 4), log(TOKEN, ROUTER, WALLET, 1, 5)]
    for other in (
        Chain(split),  # same net, different rows
        # Same rows, both legs at another (self-consistent) position in the block.
        Chain([usdc | {"transactionIndex": hex(6)}, token | {"transactionIndex": hex(6)}]),
        Chain(balances={USDC: (1_000, 901), TOKEN: (0, 2_000)}),
        Chain(native=(10**18, 10**18 - 5_001)),
    ):
        other.capture()  # each answer is valid on its own: only the pair disagrees
        with pytest.raises(ValueError, match="providers disagree on the block settlement"):
            quorum(Chain(), other)


def test_replay_refuses_a_tampered_record_or_another_scope(tmp_path: Any) -> None:
    store = EvidenceStore(tmp_path / "e.db")
    capture = Chain().capture()
    ref = persist_block_settlement(store, capture)
    with pytest.raises(ValueError):
        replay_block_settlement(store, ref, header=HEADER, account=OTHER, tokens=TOKENS)
    with pytest.raises(ValueError):
        replay_block_settlement(store, ref, header=HEADER, account=WALLET, tokens=(USDC,))
    other = BlockSnapshot(BlockRef(8453, 123, "0x" + "b2" * 32, 1_800_000_000), PARENT)
    with pytest.raises(ValueError):
        replay_block_settlement(store, ref, header=other, account=WALLET, tokens=TOKENS)
    with pytest.raises((KeyError, ValueError)):
        replay_block_settlement(
            store, replace(ref, content_hash="0" * 64), header=HEADER, account=WALLET, tokens=TOKENS
        )
    store.close()
