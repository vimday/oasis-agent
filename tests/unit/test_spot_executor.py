"""Live spot executor: gate, exactly-once submission, fills proven by balances and receipt."""

import json
from typing import Any

import pytest

from oasis_agent.launch.onchainos_source import BASE_USDC, SourceFailure
from oasis_agent.launch.spot_executor import (
    APPROVED_WALLET,
    CONFIRMATION,
    LiveExecutor,
    LiveGate,
    LiveRefused,
    LiveUnresolved,
    parse_balance,
    parse_native_balance,
    parse_tx_hash,
    preflight,
    rpc_receipt_reader,
    swap_argv,
)

TOKEN = "0x" + "ab" * 20
TX = "0x" + "cd" * 32
USD = 1_000_000


def gate(**overrides: Any) -> LiveGate:
    values: dict[str, Any] = {
        "wallet": APPROVED_WALLET,
        "max_trade_usd_micros": 100 * USD,
        "slippage_bps": 100,
        "confirmation": CONFIRMATION.format(wallet=APPROVED_WALLET),
    }
    return LiveGate(**(values | overrides))


def balance_reply(token: str, raw: int | None) -> bytes:
    rows = (
        []
        if raw is None
        else [{"tokenAddress": token, "chainIndex": "8453", "rawBalance": str(raw)}]
    )
    return json.dumps({"ok": True, "data": {"details": [{"tokenAssets": rows}]}}).encode()


SCRIPTED_REPLY = json.dumps({"ok": True, "data": [{"txHash": TX}]}).encode()


class FakeWallet:
    """Balances move only when `submit` runs; every call is recorded."""

    def __init__(self, usdc: int = 500 * USD, token: int = 0) -> None:
        self.balances = {BASE_USDC: usdc, TOKEN: token}
        self.reads: list[tuple[str, ...]] = []
        self.submits: list[tuple[str, ...]] = []
        self.fill_out = 7_000  # units received per swap
        self.reply: bytes = SCRIPTED_REPLY
        self.move = True
        self.submit_error: SourceFailure | None = None

    def read(self, argv: Any) -> bytes:
        argv = tuple(argv)
        self.reads.append(argv)
        token = argv[argv.index("--token-address") + 1]
        return balance_reply(token, self.balances[token])

    def submit(self, argv: Any) -> bytes:
        argv = tuple(argv)
        self.submits.append(argv)
        if self.submit_error is not None:
            raise self.submit_error
        src = argv[argv.index("--from") + 1]
        dst = argv[argv.index("--to") + 1]
        amount = int(argv[argv.index("--amount") + 1])
        if self.move:
            self.balances[src] -= amount
            self.balances[dst] += self.fill_out
        if self.reply == SCRIPTED_REPLY and len(self.submits) > 1:
            # A real wallet returns a new transaction per swap; only the first reuses TX.
            fresh = "0x" + f"{len(self.submits):064x}"
            return json.dumps({"ok": True, "data": [{"txHash": fresh}]}).encode()
        return self.reply


def executor(
    wallet: FakeWallet,
    receipt: Any = lambda _tx: (1, 123, APPROVED_WALLET),
    journal: list[tuple[str, dict[str, Any]]] | None = None,
) -> LiveExecutor:
    log = journal if journal is not None else []
    return LiveExecutor(
        gate(),
        read=wallet.read,
        submit=wallet.submit,
        receipt=receipt,
        emit=lambda kind, body: log.append((kind, body)),
        clock=lambda: 1_800_000_000,
        sleep=lambda _s: None,
        receipt_timeout=9.0,
        poll_seconds=3.0,
        settle_attempts=2,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"wallet": "0x" + "11" * 20},
        {"max_trade_usd_micros": 0},
        {"max_trade_usd_micros": 3_000 * USD + 1},
        {"slippage_bps": 9},
        {"slippage_bps": 301},
        {"confirmation": "yes"},
    ],
)
def test_gate_refuses_unapproved_wallet_caps_slippage_and_phrase(overrides: Any) -> None:
    with pytest.raises(ValueError):
        gate(**overrides)


def test_swap_argv_is_one_base_usdc_swap_without_force() -> None:
    argv = swap_argv(gate(), "buy", TOKEN, 5 * USD)
    assert argv[:2] == ("swap", "execute")
    options = dict(zip(argv[2::2], argv[3::2], strict=False))
    assert options["--chain"] == "base" and options["--wallet"] == APPROVED_WALLET
    assert options["--from"] == BASE_USDC and options["--to"] == TOKEN
    assert options["--slippage"] == "1.00"
    assert "--mev-protection" in argv and "--force" not in argv
    sell = swap_argv(gate(), "sell", TOKEN, 9)
    assert sell[sell.index("--from") + 1] == TOKEN and sell[sell.index("--to") + 1] == BASE_USDC
    with pytest.raises(LiveRefused):
        swap_argv(gate(), "buy", BASE_USDC, 1)
    with pytest.raises(LiveRefused):
        swap_argv(gate(), "buy", TOKEN, 0)


def test_balance_parser_fails_closed_on_ambiguity() -> None:
    assert parse_balance(balance_reply(TOKEN, None), TOKEN) == 0
    assert parse_balance(balance_reply(TOKEN, 42), TOKEN) == 42
    with pytest.raises(SourceFailure):
        parse_balance(balance_reply("0x" + "99" * 20, 42), TOKEN)
    with pytest.raises(SourceFailure):
        parse_balance(
            b'{"ok":true,"data":{"details":[{"tokenAssets":[{"tokenAddress":"'
            + TOKEN.encode()
            + b'","chainIndex":"8453","rawBalance":"1.5"}]}]}}',
            TOKEN,
        )
    with pytest.raises(SourceFailure):
        parse_balance(b'{"ok":false}', TOKEN)


def test_tx_hash_parser_requires_exactly_one_canonical_hash() -> None:
    def reply(*rows: Any) -> bytes:
        return json.dumps({"ok": True, "data": list(rows)}).encode()

    assert parse_tx_hash(reply({"txHash": TX})) == TX
    assert parse_tx_hash(reply({"txHash": TX}, {"swapTxHash": TX})) == TX
    approve = "0x" + "aa" * 32
    assert (
        parse_tx_hash(reply({"approveTxHash": approve, "txHash": approve, "swapTxHash": TX})) == TX
    )
    assert parse_tx_hash(reply({"swapTxHash": TX}, {"swapTxHash": approve})) is None
    assert parse_tx_hash(reply({"txHash": TX}, {"txHash": "0x" + "ee" * 32})) is None
    assert parse_tx_hash(reply({"txHash": "0x12"})) is None
    assert parse_tx_hash(reply({})) is None
    assert parse_tx_hash(b"not json") is None


def test_buy_fill_comes_from_balance_deltas_and_is_journaled_after_intent() -> None:
    wallet, log = FakeWallet(), []
    fill = executor(wallet, journal=log).buy(TOKEN, 10 * USD)
    assert (fill.usdc_delta, fill.token_delta, fill.tx_hash, fill.block_number) == (
        -10 * USD,
        7_000,
        TX,
        123,
    )
    assert [kind for kind, _ in log] == ["live_intent", "live_fill"]
    assert all(body["execution_eligible"] is False for _, body in log)
    assert len(wallet.submits) == 1


def test_refusals_happen_before_anything_is_submitted() -> None:
    wallet, log = FakeWallet(usdc=5 * USD), []
    bot = executor(wallet, journal=log)
    with pytest.raises(LiveRefused, match="above_trade_cap"):
        bot.buy(TOKEN, 100 * USD + 1)
    with pytest.raises(LiveRefused, match="insufficient_balance"):
        bot.buy(TOKEN, 6 * USD)
    with pytest.raises(LiveRefused, match="insufficient_balance"):
        bot.sell(TOKEN, 1)
    assert wallet.submits == [] and log == []


def test_unreadable_balance_refuses_without_submitting() -> None:
    wallet = FakeWallet()

    def broken(_argv: Any) -> bytes:
        return b'{"ok":false}'

    wallet.read = broken  # type: ignore[method-assign]
    with pytest.raises(LiveRefused, match="balance_unavailable"):
        executor(wallet).buy(TOKEN, USD)
    assert wallet.submits == []


@pytest.mark.parametrize(
    ("setup", "category"),
    [
        (lambda w: setattr(w, "submit_error", SourceFailure("timeout")), "submit_timeout"),
        (lambda w: setattr(w, "reply", b'{"ok":true,"data":[{}]}'), "no_tx_hash"),
        (lambda w: setattr(w, "move", False), "balance_not_settled"),
    ],
)
def test_unproven_outcomes_are_unresolved_and_never_retried(setup: Any, category: str) -> None:
    wallet, log = FakeWallet(), []
    setup(wallet)
    with pytest.raises(LiveUnresolved, match=category):
        executor(wallet, journal=log).buy(TOKEN, USD)
    assert len(wallet.submits) == 1
    assert [kind for kind, _ in log] == ["live_intent", "live_unresolved"]
    assert log[-1][1]["category"] == category


def test_missing_or_reverted_receipt_is_unresolved() -> None:
    wallet = FakeWallet()
    with pytest.raises(LiveUnresolved, match="receipt_unavailable"):
        executor(wallet, receipt=lambda _tx: None).buy(TOKEN, USD)
    with pytest.raises(LiveUnresolved, match="receipt_not_from_wallet"):
        executor(FakeWallet(), receipt=lambda _tx: (1, 9, "0x" + "22" * 20)).buy(TOKEN, USD)
    with pytest.raises(LiveUnresolved, match="reverted"):
        executor(FakeWallet(), receipt=lambda _tx: (0, 9, APPROVED_WALLET)).buy(TOKEN, USD)

    def flaky(_tx: str) -> tuple[int, int]:
        raise OSError("rpc down")

    with pytest.raises(LiveUnresolved, match="receipt_unavailable"):
        executor(FakeWallet(), receipt=flaky).buy(TOKEN, USD)
    assert len(wallet.submits) == 1


def test_sell_requires_exact_units_spent_and_usdc_received() -> None:
    wallet = FakeWallet(token=50_000)
    wallet.fill_out = 3 * USD
    fill = executor(wallet).sell(TOKEN, 50_000)
    assert (fill.token_delta, fill.usdc_delta) == (-50_000, 3 * USD)


def test_balance_parser_accepts_the_observed_cli_shape() -> None:
    # Shape captured from `onchainos wallet balance --chain base --token-address <USDC> --force`
    # on the unfunded Agentic Wallet (2026-11-07); values other than shape are irrelevant.
    raw = (
        b'{"ok":true,"data":{"details":[{"tokenAssets":[{"balance":"12.5","chainIndex":"8453",'
        b'"decimal":"6","rawBalance":"12500000","symbol":"USDC","tokenAddress":"'
        + BASE_USDC.encode()
        + b'","tokenName":"USD Coin","tokenPrice":"1","usdValue":"12.5"}]}]}}'
    )
    assert parse_balance(raw, BASE_USDC) == 12_500_000


def addresses_reply(address: str) -> bytes:
    return json.dumps(
        {"ok": True, "data": {"evm": [{"address": address, "chainIndex": "8453"}]}}
    ).encode()


def native_reply(wei: int | None) -> bytes:
    rows = (
        [] if wei is None else [{"tokenAddress": "", "chainIndex": "8453", "rawBalance": str(wei)}]
    )
    return json.dumps({"ok": True, "data": {"details": [{"tokenAssets": rows}]}}).encode()


def preflight_reader(address: str, usdc: int, wei: int | None) -> Any:
    def read(argv: Any) -> bytes:
        argv = tuple(argv)
        if argv[:2] == ("wallet", "addresses"):
            return addresses_reply(address)
        if "--token-address" in argv:
            return balance_reply(BASE_USDC, usdc)
        return native_reply(wei)

    return read


def test_preflight_binds_active_account_and_requires_usdc_and_gas() -> None:
    ok = preflight(gate(), preflight_reader(APPROVED_WALLET, 5 * USD, 10**15))
    assert ok == {
        "wallet": APPROVED_WALLET,
        "usdc_base_units": "5000000",
        "native_wei": str(10**15),
    }
    cases = [
        (preflight_reader("0x" + "11" * 20, 5 * USD, 10**15), "active_account_not_gated_wallet"),
        (preflight_reader(APPROVED_WALLET, USD - 1, 10**15), "wallet_usdc_below_1_usd"),
        (preflight_reader(APPROVED_WALLET, 5 * USD, None), "wallet_gas_below_minimum"),
        (lambda _argv: b'{"ok":false}', "preflight_addresses_shape"),
    ]
    for read, category in cases:
        with pytest.raises(LiveRefused, match=category):
            preflight(gate(), read)


def test_observed_empty_native_shape_is_zero() -> None:
    # Observed on the unfunded wallet: `wallet balance --chain base --force`.
    raw = b'{"ok":true,"data":{"details":[{"tokenAssets":[]}],"totalValueUsd":"0.00"}}'
    assert parse_native_balance(raw) == 0


def test_receipt_fields_come_from_one_raw_response() -> None:
    calls: list[tuple[str, Any]] = []

    class Transport:
        def __init__(self, raw: Any) -> None:
            self.raw = raw

        def request(self, method: str, params: Any) -> Any:
            calls.append((method, params))
            return self.raw

    upper = "0x" + "95A9" * 10
    raw = {
        "transactionHash": TX.upper().replace("0X", "0x"),
        "status": "0x1",
        "blockNumber": "0x1f",
        "from": upper,
    }
    assert rpc_receipt_reader(Transport(raw))(TX) == (1, 31, upper.lower())
    assert calls == [("eth_getTransactionReceipt", [TX])]  # exactly one request
    assert rpc_receipt_reader(Transport(None))(TX) is None
    for bad in (
        raw | {"transactionHash": "0x" + "ee" * 32},
        raw | {"status": "0x2"},
        raw | {"blockNumber": "0x01"},
        {k: v for k, v in raw.items() if k != "from"},
        ["not", "an", "object"],
    ):
        with pytest.raises(ValueError):
            rpc_receipt_reader(Transport(bad))(TX)
    with pytest.raises(ValueError):
        rpc_receipt_reader(Transport(raw))("0x12")


def test_fill_record_states_its_settlement_boundary() -> None:
    fill = executor(FakeWallet()).buy(TOKEN, USD)
    assert fill.record()["settlement"] == "unverified_balance_delta_plus_sender_receipt"
