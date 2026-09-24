"""Movement reconstruction and two-provider ERC-20 flow attribution.

The risk ledger only accepts a frame whose every asset delta is either observed
and explained or explicitly counted as unresolved. These tests pin the
conservatism: an unexplained delta raises the unresolved count, a withdrawal is
credited at a floored value, and no provider disagreement can be averaged away.
"""

import json
from dataclasses import dataclass, field, replace

import pytest

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.account_flows import (
    TRANSFER_TOPIC,
    FlowScope,
    TokenFlow,
    TransferLog,
    collect_token_flow_quorum,
    persist_token_flow_evidence,
    reconstruct_movements,
    replay_token_flow_evidence,
)
from oasis_agent.farming.price_evidence import value_usd_micros
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport

CHAIN_ID = 4663
ACCOUNT = "0x" + "e6" * 20
OTHER_ACCOUNT = "0x" + "e7" * 20
THIRD_PARTY = "0x" + "e8" * 20
TOKEN = "0x" + "11" * 20
POSITION_MANAGER = "0x" + "21" * 20
POSITION_ID = 7
TOKEN_DECIMALS = 6
NATIVE_ANSWER = 240_000_000_000
USDC_ANSWER = 100_000_000
NATIVE_PRICE = (NATIVE_ANSWER * 1_000_000, 10**8, 18)
USDC_PRICE = (USDC_ANSWER * 1_000_000, 10**8, TOKEN_DECIMALS)
PRICES = {"native": NATIVE_PRICE, "usdc": USDC_PRICE}
PREVIOUS_UNITS = {"native": 5 * 10**18, "usdc": 1_000_000_000}

START_BLOCK = 41
END_BLOCK = 43


def _word(address: str) -> str:
    return "0x" + address[2:].rjust(64, "0")


def transfer_log(
    *,
    from_address: str = THIRD_PARTY,
    to_address: str = ACCOUNT,
    units: int = 1_000_000,
    block_number: int = END_BLOCK - 1,
    log_index: int = 0,
    token: str = TOKEN,
    transaction_hash: str = "0x" + "7a" * 32,
    removed: bool = False,
) -> dict[str, object]:
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, _word(from_address), _word(to_address)],
        "data": "0x" + units.to_bytes(32, "big").hex(),
        "blockNumber": hex(block_number),
        "logIndex": hex(log_index),
        "transactionHash": transaction_hash,
        "removed": removed,
    }


def erc721_transfer_log(
    *,
    from_address: str = ZERO_ADDRESS,
    to_address: str = ACCOUNT,
    block_number: int = END_BLOCK - 1,
    log_index: int = 0,
    token: str = POSITION_MANAGER,
    token_id: int = POSITION_ID,
    transaction_hash: str = "0x" + "7b" * 32,
    removed: bool = False,
) -> dict[str, object]:
    return {
        "address": token,
        "topics": [
            TRANSFER_TOPIC,
            _word(from_address),
            _word(to_address),
            "0x" + f"{token_id:064x}",
        ],
        "data": "0x",
        "blockNumber": hex(block_number),
        "logIndex": hex(log_index),
        "transactionHash": transaction_hash,
        "removed": removed,
    }


@dataclass
class FlowFixture:
    """One deterministic provider for one movement window."""

    chain_id: int = CHAIN_ID
    incoming: list[dict[str, object]] = field(default_factory=list)
    outgoing: list[dict[str, object]] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        self.methods.append(method)
        result = self._result(method, params)
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def _result(self, method: str, params: list[object]) -> object:
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getLogs":
            query = params[0]
            assert isinstance(query, dict)
            topics = query["topics"]
            assert isinstance(topics, list)
            return list(self.incoming if topics[1] is None else self.outgoing)
        raise AssertionError(method)


def session(source: str, fixture: FlowFixture | None = None) -> tuple:
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport(
            "https://fixture.invalid", http=fixture or FlowFixture(), recorder=recorder
        ),
        recorder,
        source,
    )


def scope(*, end_block: int = END_BLOCK, decimals: int = TOKEN_DECIMALS) -> FlowScope:
    return FlowScope(CHAIN_ID, ACCOUNT, "usdc", TOKEN, decimals, START_BLOCK, end_block)


def position_scope(*, token_id: int = POSITION_ID, end_block: int = END_BLOCK) -> FlowScope:
    return FlowScope(
        CHAIN_ID, ACCOUNT, "lp-position", POSITION_MANAGER, 0, START_BLOCK, end_block, token_id
    )


def flow_quorum(
    left: FlowFixture | None = None,
    right: FlowFixture | None = None,
    *,
    window: FlowScope | None = None,
):
    return collect_token_flow_quorum(
        session("rpc_a", left), session("rpc_b", right), scope=window or scope()
    )


def usdc_flow(net_units: int, external_net_units: int | None = None) -> TokenFlow:
    external = net_units if external_net_units is None else external_net_units
    return TokenFlow(
        "usdc", TOKEN_DECIMALS, net_units, external, max(external, 0), max(-external, 0), ()
    )


def test_external_withdrawal_is_credited_and_never_becomes_a_loss() -> None:
    current = {"native": 5 * 10**18, "usdc": 700_000_000}
    result = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units=current,
        prices=PRICES,
        flows={"usdc": usdc_flow(-300_000_000)},
        native_asset_id="native",
        native_gas_units=0,
    )
    assert result.movements == {"native": 0, "usdc": -300_000_000}
    assert result.non_trading_flow_usd_micros == -300_000_000
    assert result.unresolved_operations == 0


def test_a_transfer_between_monitored_accounts_is_credited_not_a_loss() -> None:
    """Both legs of an internal move are explained: only `net_units` may be credited.

    Crediting `external_net_units` here would report a zero-credit NAV drop of the
    same size on the sending account, which the ledger would latch as a loss.
    """
    sent = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units={"native": PREVIOUS_UNITS["native"], "usdc": 700_000_000},
        prices=PRICES,
        flows={"usdc": usdc_flow(-300_000_000, external_net_units=0)},
        native_asset_id="native",
        native_gas_units=0,
    )
    assert sent.movements == {"native": 0, "usdc": -300_000_000}
    assert sent.non_trading_flow_usd_micros == -300_000_000
    assert sent.unresolved_operations == 0
    received = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units={"native": PREVIOUS_UNITS["native"], "usdc": 1_300_000_000},
        prices=PRICES,
        flows={"usdc": usdc_flow(300_000_000, external_net_units=0)},
        native_asset_id="native",
        native_gas_units=0,
    )
    assert received.movements == {"native": 0, "usdc": 300_000_000}
    assert received.non_trading_flow_usd_micros == 300_000_000
    assert received.unresolved_operations == 0


def test_native_gas_spend_is_explained() -> None:
    gas = 21_000 * 10**9
    result = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units={"native": PREVIOUS_UNITS["native"] - gas, "usdc": PREVIOUS_UNITS["usdc"]},
        prices=PRICES,
        flows={},
        native_asset_id="native",
        native_gas_units=gas,
    )
    assert result.movements["native"] == -gas
    assert result.non_trading_flow_usd_micros == 0
    assert result.unresolved_operations == 0


def test_declared_native_deposit_is_explained_at_its_floored_value() -> None:
    # 2.4e9 micros per wei-scaled ether: one wei is worth 2.4e-9 micro and must
    # never be rounded up into credit the owner did not receive.
    result = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units=dict(PREVIOUS_UNITS),
        prices=PRICES,
        flows={},
        native_asset_id="native",
        native_gas_units=1,
        non_trading_native_units=1,
    )
    assert result.movements["native"] == 0
    assert result.non_trading_flow_usd_micros == 0
    assert result.unresolved_operations == 0
    assert (
        value_usd_micros(
            1,
            numerator=NATIVE_PRICE[0],
            denominator=NATIVE_PRICE[1],
            decimals=NATIVE_PRICE[2],
            round_up=True,
        )
        == 1
    )


def test_unexplained_native_spend_is_unresolved() -> None:
    result = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units={"native": PREVIOUS_UNITS["native"] - 1, "usdc": PREVIOUS_UNITS["usdc"]},
        prices=PRICES,
        flows={},
        native_asset_id="native",
        native_gas_units=0,
    )
    assert result.movements["native"] == -1
    assert result.unresolved_operations == 1


def test_unexplained_token_decrease_is_unresolved() -> None:
    result = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units={"native": PREVIOUS_UNITS["native"], "usdc": 900_000_000},
        prices=PRICES,
        flows={},
        native_asset_id="native",
        native_gas_units=0,
    )
    assert result.unresolved_operations == 1
    assert result.non_trading_flow_usd_micros == 0


def test_flow_that_contradicts_the_observed_delta_is_unresolved() -> None:
    result = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units={"native": PREVIOUS_UNITS["native"], "usdc": 900_000_000},
        prices=PRICES,
        flows={"usdc": usdc_flow(-50_000_000)},
        native_asset_id="native",
        native_gas_units=0,
    )
    assert result.unresolved_operations == 1


def test_flow_decimals_mismatch_is_unresolved() -> None:
    wrong = TokenFlow("usdc", 18, -100_000_000, -100_000_000, 0, 100_000_000, ())
    result = reconstruct_movements(
        previous_units=PREVIOUS_UNITS,
        current_units={"native": PREVIOUS_UNITS["native"], "usdc": 900_000_000},
        prices=PRICES,
        flows={"usdc": wrong},
        native_asset_id="native",
        native_gas_units=0,
    )
    assert result.unresolved_operations == 1


def test_native_flow_cannot_come_from_transfer_logs() -> None:
    with pytest.raises(ValueError, match="native gas flow cannot be reconstructed"):
        reconstruct_movements(
            previous_units=PREVIOUS_UNITS,
            current_units=dict(PREVIOUS_UNITS),
            prices=PRICES,
            flows={"native": TokenFlow("native", 18, 0, 0, 0, 0, ())},
            native_asset_id="native",
            native_gas_units=0,
        )


def test_movement_reconstruction_requires_one_complete_asset_set() -> None:
    with pytest.raises(ValueError, match="one complete asset set"):
        reconstruct_movements(
            previous_units=PREVIOUS_UNITS,
            current_units={"native": 1},
            prices=PRICES,
            flows={},
            native_asset_id="native",
            native_gas_units=0,
        )
    with pytest.raises(ValueError, match="one complete asset set"):
        reconstruct_movements(
            previous_units=PREVIOUS_UNITS,
            current_units=dict(PREVIOUS_UNITS),
            prices={"usdc": USDC_PRICE},
            flows={},
            native_asset_id="native",
            native_gas_units=0,
        )


def test_movement_reconstruction_requires_the_native_asset_price() -> None:
    with pytest.raises(ValueError, match="requires the native gas asset"):
        reconstruct_movements(
            previous_units={"usdc": 1_000_000},
            current_units={"usdc": 1_000_000},
            prices={"usdc": USDC_PRICE},
            flows={},
            native_asset_id="native",
            native_gas_units=0,
        )


def test_gas_units_must_be_a_nonnegative_integer() -> None:
    with pytest.raises(TypeError, match="native_gas_units must be an integer"):
        reconstruct_movements(
            previous_units=PREVIOUS_UNITS,
            current_units=dict(PREVIOUS_UNITS),
            prices=PRICES,
            flows={},
            native_asset_id="native",
            native_gas_units=1.0,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="native gas units must be nonnegative"):
        reconstruct_movements(
            previous_units=PREVIOUS_UNITS,
            current_units=dict(PREVIOUS_UNITS),
            prices=PRICES,
            flows={},
            native_asset_id="native",
            native_gas_units=-1,
        )


def test_two_provider_flow_persists_and_replays_exactly(tmp_path) -> None:
    collected = flow_quorum(
        FlowFixture(incoming=[transfer_log(units=2_000_000, log_index=1)]),
        FlowFixture(incoming=[transfer_log(units=2_000_000, log_index=1)]),
    )
    assert [log.units for log in collected.flow(tracked_accounts=frozenset({ACCOUNT})).logs] == [
        2_000_000
    ]
    store = EvidenceStore(tmp_path / "flow.db")
    try:
        references = persist_token_flow_evidence(store, collected)
        assert len({reference.source for reference in references}) == 2
        for reference in references:
            assert reference.kind == "account_token_flow"
            assert reference.block is None
            replayed = replay_token_flow_evidence(
                store, reference, scope=scope(), tracked_accounts=frozenset({ACCOUNT})
            )
            assert replayed.logs == collected.left.logs
            assert replayed.net_units == 2_000_000
            assert replayed.external_net_units == 2_000_000
        interpretation = store.get_record(references[0]).interpretation_payload
        assert json.loads(interpretation)["execution_eligible"] is False
    finally:
        store.close()


def test_internal_transfers_between_owned_accounts_are_not_external() -> None:
    collected = flow_quorum(
        FlowFixture(incoming=[transfer_log(from_address=OTHER_ACCOUNT, units=7)]),
        FlowFixture(incoming=[transfer_log(from_address=OTHER_ACCOUNT, units=7)]),
    )
    internal = collected.flow(tracked_accounts=frozenset({ACCOUNT, OTHER_ACCOUNT}))
    assert internal.net_units == 7
    assert internal.external_net_units == 0
    external = collected.flow(tracked_accounts=frozenset({ACCOUNT}))
    assert external.external_net_units == 7


def test_both_transfer_directions_are_combined_into_one_net_flow() -> None:
    incoming = [transfer_log(units=500, log_index=1)]
    outgoing = [transfer_log(from_address=ACCOUNT, to_address=THIRD_PARTY, units=200, log_index=2)]
    collected = flow_quorum(
        FlowFixture(incoming=incoming, outgoing=outgoing),
        FlowFixture(incoming=incoming, outgoing=outgoing),
    )
    flow = collected.flow(tracked_accounts=frozenset({ACCOUNT}))
    assert (flow.net_units, flow.external_in_units, flow.external_out_units) == (300, 500, 200)
    assert flow.external_net_units == 300
    assert [log.order_key for log in flow.logs] == sorted(log.order_key for log in flow.logs)


def test_providers_must_agree_on_the_transfer_set() -> None:
    with pytest.raises(ValueError, match="disagree on the transfer set"):
        flow_quorum(
            FlowFixture(incoming=[transfer_log(units=1)]),
            FlowFixture(incoming=[transfer_log(units=2)]),
        )


def test_a_log_outside_the_declared_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside its declared window"):
        flow_quorum(FlowFixture(incoming=[transfer_log(block_number=END_BLOCK + 1)]))
    with pytest.raises(ValueError, match="outside its declared window"):
        flow_quorum(FlowFixture(incoming=[transfer_log(block_number=START_BLOCK)]))


def test_a_duplicate_log_is_rejected() -> None:
    duplicated = [
        transfer_log(units=5, log_index=1),
        transfer_log(units=5, log_index=1),
    ]
    with pytest.raises(ValueError, match="duplicate log"):
        flow_quorum(FlowFixture(incoming=duplicated))


def test_noncanonical_or_unfinalized_logs_are_rejected() -> None:
    wrong_topic = transfer_log() | {
        "topics": ["0x" + "00" * 32, _word(THIRD_PARTY), _word(ACCOUNT)]
    }
    with pytest.raises(ValueError, match="not a canonical ERC-20 Transfer"):
        flow_quorum(FlowFixture(incoming=[wrong_topic]))
    with pytest.raises(ValueError, match="one ABI word"):
        flow_quorum(FlowFixture(incoming=[transfer_log() | {"data": "0x01"}]))
    with pytest.raises(ValueError, match="token address mismatch"):
        flow_quorum(FlowFixture(incoming=[transfer_log() | {"address": THIRD_PARTY}]))
    with pytest.raises(ValueError, match="removed transfer log is not final"):
        flow_quorum(FlowFixture(incoming=[transfer_log(removed=True)]))
    with pytest.raises(ValueError, match="not a canonical address word"):
        flow_quorum(
            FlowFixture(
                incoming=[transfer_log() | {"topics": [TRANSFER_TOPIC, "0x01", _word(ACCOUNT)]}]
            )
        )


def test_the_same_session_cannot_form_a_flow_quorum() -> None:
    shared = session("rpc_a")
    with pytest.raises(ValueError, match="distinct fresh provider sessions required"):
        collect_token_flow_quorum(shared, shared, scope=scope())


def test_replay_requires_the_declared_scope(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "scope.db")
    try:
        reference = persist_token_flow_evidence(store, flow_quorum())[0]
        with pytest.raises(ValueError, match="does not match the requested scope"):
            replay_token_flow_evidence(
                store,
                reference,
                scope=scope(end_block=END_BLOCK + 1),
                tracked_accounts=frozenset({ACCOUNT}),
            )
        with pytest.raises(ValueError, match="unsupported token flow evidence"):
            replay_token_flow_evidence(
                store,
                replace(reference, kind="account_inventory"),
                scope=scope(),
                tracked_accounts=frozenset({ACCOUNT}),
            )
    finally:
        store.close()


def test_replay_rejects_a_forged_flow_even_with_a_recomputed_hash(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "forged.db")
    try:
        reference = persist_token_flow_evidence(
            store,
            flow_quorum(
                FlowFixture(incoming=[transfer_log(units=9, log_index=1)]),
                FlowFixture(incoming=[transfer_log(units=9, log_index=1)]),
            ),
        )[0]
        record = store.get_record(reference)
        data = json.loads(record.payload)
        data["logs"][0]["units"] = 10**9
        forged = store.put(
            reference.kind,
            canonical_json(data),
            reference.block,
            reference.source,
            request=record.request_payload,
            raw_response=record.raw_response_payload,
            retrieved_at=reference.retrieved_at,
        )
        with pytest.raises(ValueError, match="differs from raw reconstruction"):
            replay_token_flow_evidence(
                store, forged, scope=scope(), tracked_accounts=frozenset({ACCOUNT})
            )
    finally:
        store.close()


def test_a_provider_on_another_chain_is_rejected() -> None:
    with pytest.raises(ValueError, match="provider chain mismatch"):
        flow_quorum(FlowFixture(chain_id=8453))


def test_flow_scope_and_transfer_log_validation() -> None:
    with pytest.raises(ValueError, match="window must span at least one block"):
        scope(end_block=START_BLOCK)
    with pytest.raises(ValueError, match="self transfers are not attributable"):
        flow_quorum(FlowFixture(incoming=[transfer_log(from_address=ACCOUNT, to_address=ACCOUNT)]))


def test_a_position_scope_queries_logs_by_its_own_token_id() -> None:
    collected = flow_quorum(
        FlowFixture(incoming=[erc721_transfer_log()]),
        FlowFixture(incoming=[erc721_transfer_log()]),
        window=position_scope(),
    )
    queries = [
        json.loads(exchange.request_payload)["params"][0]
        for exchange in collected.left.exchanges
        if json.loads(exchange.request_payload)["method"] == "eth_getLogs"
    ]
    assert queries[0]["topics"] == [TRANSFER_TOPIC, None, _word(ACCOUNT), f"0x{POSITION_ID:064x}"]
    assert queries[1]["topics"] == [TRANSFER_TOPIC, _word(ACCOUNT), None, f"0x{POSITION_ID:064x}"]


def test_a_position_mint_and_burn_move_exactly_one_unit(tmp_path) -> None:
    minted = flow_quorum(
        FlowFixture(incoming=[erc721_transfer_log()]),
        FlowFixture(incoming=[erc721_transfer_log()]),
        window=position_scope(),
    )
    flow = minted.flow(tracked_accounts=frozenset({ACCOUNT}))
    assert (flow.asset_id, flow.decimals, flow.net_units) == ("lp-position", 0, 1)
    assert flow.logs[0].token_id == POSITION_ID
    store = EvidenceStore(tmp_path / "position-flow.db")
    try:
        references = persist_token_flow_evidence(store, minted)
        for reference in references:
            replayed = replay_token_flow_evidence(
                store, reference, scope=position_scope(), tracked_accounts=frozenset({ACCOUNT})
            )
            assert replayed == flow
        payload = json.loads(store.get_record(references[0]).payload)
        assert payload["scope"]["token_id"] == POSITION_ID
        assert payload["logs"][0]["token_id"] == POSITION_ID
    finally:
        store.close()
    burned = flow_quorum(
        FlowFixture(outgoing=[erc721_transfer_log(from_address=ACCOUNT, to_address=ZERO_ADDRESS)]),
        FlowFixture(outgoing=[erc721_transfer_log(from_address=ACCOUNT, to_address=ZERO_ADDRESS)]),
        window=position_scope(),
    )
    assert burned.flow(tracked_accounts=frozenset({ACCOUNT})).net_units == -1


def test_the_erc20_evidence_shape_is_unchanged(tmp_path) -> None:
    """A position adds keys to its own payload; ERC-20 evidence bytes must not move."""
    collected = flow_quorum(
        FlowFixture(incoming=[transfer_log(units=4, log_index=1)]),
        FlowFixture(incoming=[transfer_log(units=4, log_index=1)]),
    )
    store = EvidenceStore(tmp_path / "shape.db")
    try:
        reference = persist_token_flow_evidence(store, collected)[0]
        payload = json.loads(store.get_record(reference).payload)
        assert set(payload["scope"]) == {
            "chain_id",
            "account",
            "asset_id",
            "token",
            "decimals",
            "start_block",
            "end_block",
        }
        assert set(payload["logs"][0]) == {
            "token",
            "from_address",
            "to_address",
            "units",
            "block_number",
            "log_index",
            "transaction_hash",
        }
    finally:
        store.close()


def test_a_position_log_with_another_token_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="token id differs from the declared position"):
        flow_quorum(
            FlowFixture(incoming=[erc721_transfer_log(token_id=POSITION_ID + 1)]),
            window=position_scope(),
        )


def test_an_erc721_log_must_carry_no_data() -> None:
    with pytest.raises(ValueError, match="must carry no data"):
        flow_quorum(
            FlowFixture(incoming=[erc721_transfer_log() | {"data": "0x" + "00" * 32}]),
            window=position_scope(),
        )


def test_a_position_and_an_erc20_scope_do_not_mix() -> None:
    with pytest.raises(ValueError, match="must carry exactly 4 topics"):
        flow_quorum(FlowFixture(incoming=[transfer_log()]), window=position_scope())
    with pytest.raises(ValueError, match="must carry exactly 3 topics"):
        flow_quorum(FlowFixture(incoming=[erc721_transfer_log()]), window=scope())


def test_a_position_scope_requires_zero_decimals_and_a_bounded_token_id() -> None:
    with pytest.raises(ValueError, match="requires zero decimals"):
        FlowScope(
            CHAIN_ID,
            ACCOUNT,
            "lp-position",
            POSITION_MANAGER,
            6,
            START_BLOCK,
            END_BLOCK,
            POSITION_ID,
        )
    for invalid in (0, -1, 2**256, True):
        with pytest.raises(ValueError, match="bounded token id"):
            position_scope(token_id=invalid)


def test_a_position_transfer_moves_exactly_one_unit() -> None:
    minted = TransferLog(
        POSITION_MANAGER,
        ZERO_ADDRESS,
        ACCOUNT,
        1,
        END_BLOCK - 1,
        0,
        "0x" + "7b" * 32,
        POSITION_ID,
    )
    assert minted.from_address == ZERO_ADDRESS
    with pytest.raises(ValueError, match="exactly one position unit"):
        TransferLog(
            POSITION_MANAGER,
            ZERO_ADDRESS,
            ACCOUNT,
            2,
            END_BLOCK - 1,
            0,
            "0x" + "7b" * 32,
            POSITION_ID,
        )
    with pytest.raises(ValueError, match="must be nonzero"):
        TransferLog(POSITION_MANAGER, ZERO_ADDRESS, ACCOUNT, 1, END_BLOCK - 1, 0, "0x" + "7b" * 32)
