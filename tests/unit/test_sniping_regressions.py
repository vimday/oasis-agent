"""Independent protocol vectors for bugs found in the live execution review."""

import json
import time
from threading import Barrier
from unittest.mock import Mock

import pytest
from eth_abi import decode, encode
from eth_account import Account as EthAccount
from eth_utils import to_checksum_address

from oasis_agent.sniping.calldata import (
    EXECUTE_SELECTOR,
    buy_calldata,
    buy_calldata_usdc,
    encode_v3_path,
    sell_calldata,
    sell_calldata_usdc,
)
from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.monitor import LivePool, Monitor
from oasis_agent.sniping.rpc import DualValue, JsonRpc, ProviderDisagreement, RpcError, _selector


def test_feed_scales_eight_decimals_to_six() -> None:
    monitor = object.__new__(Monitor)
    monitor.config = ExecutionConfig()
    monitor.rpc = Mock()
    now = int(time.time())
    raw = (
        "0x"
        + encode(
            ["uint80", "int256", "uint256", "uint256", "uint80"], [1, 3000 * 10**8, now, now, 1]
        ).hex()
    )
    monitor.rpc.dual_eth_call.return_value = DualValue(raw, raw)
    monitor.rpc.eth_call.return_value = "0x" + encode(["uint8"], [8]).hex()
    monitor.rpc.block_number.return_value = 100
    assert monitor._eth_usd_feed()[0] == 3000 * 10**6


def test_quoter_static_tuple_and_four_outputs() -> None:
    cfg = ExecutionConfig()
    monitor = object.__new__(Monitor)
    monitor.config = cfg
    monitor.rpc = Mock()
    raw = (
        "0x"
        + encode(
            ["uint256", "uint160", "uint32", "uint256"], [1000 * 10**18, 2**96, 1, 100000]
        ).hex()
    )
    monitor.rpc.dual_eth_call.return_value = DualValue(raw, raw)
    monitor.rpc.block_number.return_value = 100
    amount, _ = monitor._quote_buy(LivePool(3000, "0x" + "1" * 40), 10**17)
    assert amount == 1000 * 10**18
    actual = monitor.rpc.dual_eth_call.call_args.args[1]
    expected = (
        _selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
        + encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(cfg.weth, cfg.target_ca, 10**17, 3000, 0)],
        ).hex()
    )
    assert actual == expected


def test_simulation_preserves_wallet_and_native_value() -> None:
    rpc = object.__new__(JsonRpc)
    rpc.call = Mock(return_value="0x")
    wallet = "0x" + "2" * 40
    rpc.simulate(
        from_address=wallet, to=ExecutionConfig().universal_router, data="0x1234", value=10**17
    )
    tx = rpc.call.call_args.args[1][0]
    assert tx["from"] == wallet
    assert tx["value"] == hex(10**17)


def test_native_buy_wraps_before_swap_and_sell_uses_actual_unwrap_opcode() -> None:
    cfg = ExecutionConfig()
    data, _ = buy_calldata(
        cfg, amount_in_wei=10**17, amount_out_min_laptop=1, fee=3000, recipient=cfg.weth
    )
    commands, inputs = decode(["bytes", "bytes[]"], bytes.fromhex(data[len(EXECUTE_SELECTOR) :]))
    assert commands == bytes([0x0B, 0x00])
    assert len(inputs) == 2
    data = sell_calldata(
        cfg, amount_in_laptop=10**18, amount_out_min_weth=1, fee=3000, wallet=cfg.weth
    )
    commands, _ = decode(["bytes", "bytes[]"], bytes.fromhex(data[len(EXECUTE_SELECTOR) :]))
    assert commands == bytes([0x00, 0x0C])


def test_nonempty_execution_journal_reopens(tmp_path) -> None:
    path = tmp_path / "journal.db"
    policy = {"schema": "oasis.launch-execution-policy.v1"}
    with ExecutionJournal(path, policy) as journal:
        first = journal.append("test", {"at": 1})
    with ExecutionJournal(path, policy) as journal:
        assert journal.read() == [first]


def test_live_requires_signer_and_dry_run_never_signs(tmp_path) -> None:
    cfg = ExecutionConfig()
    # Without a signer, a live run is refused before any network or journal work.
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        monitor = Monitor(cfg, Mock(), cfg.weth, None, journal, tmp_path / "stop", dry_run=False)
        with pytest.raises(RpcError, match="requires a signing account"):
            monitor.run()
        with pytest.raises(RpcError, match="no signing key"):
            monitor._broadcast({"nonce": 1, "chainId": 8453})
    # Dry-run refuses to sign even when a signer object is present.
    account = Mock()
    with ExecutionJournal(tmp_path / "j2.db", {"test": True}) as journal:
        monitor = Monitor(cfg, Mock(), cfg.weth, account, journal, tmp_path / "stop", dry_run=True)
        with pytest.raises(RpcError, match="dry-run mode cannot sign"):
            monitor._broadcast({"nonce": 1, "chainId": 8453})
        account.sign_transaction.assert_not_called()


def test_unresolved_sent_intent_halts_on_recovery(tmp_path) -> None:
    cfg = ExecutionConfig()
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        journal.append("broadcast_sent", {"nonce": 7, "tx_hash": "0x" + "a" * 64, "chain_id": 8453})
        rpc = Mock()
        rpc.eth_call.return_value = "0x" + "0" * 64
        rpc.get_transaction_receipt.return_value = None  # still not mined
        monitor = Monitor(cfg, rpc, cfg.weth, None, journal, tmp_path / "stop", dry_run=True)
        monitor._recover_position()
        assert monitor.submission_unknown
        assert monitor.halt_buys


def test_resolved_sent_intent_reconstructs_position(tmp_path) -> None:
    cfg = ExecutionConfig()
    tx_hash = "0x" + "b" * 64
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        journal.append("broadcast_sent", {"nonce": 7, "tx_hash": tx_hash, "chain_id": 8453})
        journal.append(
            "buy_confirmed",
            {
                "tx_hash": tx_hash,
                "amount_wei": 10**17,
                "received_base": 10**18,
                "price_usd_micros": 3_000_000_000,
                "observed_at": 123,
            },
        )
        rpc = Mock()
        rpc.eth_call.return_value = "0x" + "0" * 64
        monitor = Monitor(cfg, rpc, cfg.weth, None, journal, tmp_path / "stop", dry_run=True)
        monitor._recover_position()
        assert not monitor.submission_unknown
        assert not monitor.halt_buys
        assert monitor.tranches_done == 1
        assert monitor.position.received_base == 10**18


def test_halt_marker_stops_buys_without_unknown_tx(tmp_path) -> None:
    cfg = ExecutionConfig()
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        journal.append("halt_buys", {"reason": "rpc_fault"})
        rpc = Mock()
        rpc.eth_call.return_value = "0x" + "0" * 64
        monitor = Monitor(cfg, rpc, cfg.weth, None, journal, tmp_path / "stop", dry_run=True)
        monitor._recover_position()
        assert monitor.halt_buys
        assert not monitor.submission_unknown


def test_unknown_broadcast_halts_immediately_and_persists(tmp_path) -> None:
    cfg = ExecutionConfig()
    account = EthAccount.create()
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        rpc = Mock()
        rpc.send_raw_transaction.side_effect = RpcError("network down after broadcast")
        monitor = Monitor(
            cfg, rpc, account.address, account, journal, tmp_path / "stop", dry_run=False
        )
        with pytest.raises(RpcError, match="broadcast outcome unknown"):
            monitor._broadcast(
                {
                    "nonce": 0,
                    "to": cfg.weth,
                    "value": 0,
                    "gas": 21000,
                    "gasPrice": 1,
                    "chainId": 8453,
                    "data": "0x",
                }
            )
        assert monitor.submission_unknown
        assert any(r["kind"] == "halt_buys" for r in journal.read())


def test_reads_use_one_hash_pinned_snapshot() -> None:
    rpc = object.__new__(JsonRpc)
    rpc.snapshot = {"blockHash": "0x" + "1" * 64, "requireCanonical": True}
    rpc.call = Mock(return_value="0x")
    rpc.eth_call(ExecutionConfig().target_ca, "0x1234")
    assert rpc.call.call_args.args[1][-1] == rpc.snapshot


def test_simulation_uses_hash_pinned_wallet_context() -> None:
    rpc = object.__new__(JsonRpc)
    rpc.snapshot = {"blockHash": "0x" + "1" * 64, "requireCanonical": True}
    rpc.call = Mock(return_value="0x")
    wallet = ExecutionConfig().weth
    rpc.simulate(from_address=wallet, to=ExecutionConfig().universal_router, data="0x1234", value=7)
    assert rpc.call.call_args.args[1] == [
        {
            "from": wallet,
            "to": to_checksum_address(ExecutionConfig().universal_router),
            "data": "0x1234",
            "value": "0x7",
        },
        rpc.snapshot,
    ]


def test_dual_provider_requests_start_concurrently_without_weakening_agreement() -> None:
    barrier = Barrier(2)
    rpc = JsonRpc("https://primary.invalid", "https://secondary.invalid", ExecutionConfig())

    def response(url, method, params):
        barrier.wait(timeout=2)
        return url

    rpc._post = response
    try:
        result = rpc.dual("eth_chainId", [])
        assert result.primary == "https://primary.invalid"
        assert result.secondary == "https://secondary.invalid"
        with pytest.raises(ProviderDisagreement):
            result.require_agreement("chain")
    finally:
        rpc.close()


def test_usdc_path_encoding_interleaves_tokens_and_fees() -> None:
    cfg = ExecutionConfig()
    path = encode_v3_path((cfg.weth, cfg.usdc, cfg.target_ca), (cfg.usdc_weth_fee, 3000))
    assert path == (
        bytes.fromhex(cfg.weth[2:])
        + cfg.usdc_weth_fee.to_bytes(3, "big")
        + bytes.fromhex(cfg.usdc[2:])
        + (3000).to_bytes(3, "big")
        + bytes.fromhex(cfg.target_ca[2:])
    )


def test_usdc_two_hop_calldata_directions_and_wrap_unwrap() -> None:
    cfg = ExecutionConfig()
    data, value = buy_calldata_usdc(
        cfg,
        amount_in_wei=10**17,
        amount_out_min_laptop=1,
        laptop_usdc_fee=3000,
        recipient=cfg.target_ca,
    )
    assert value == 10**17
    commands, inputs = decode(["bytes", "bytes[]"], bytes.fromhex(data[len(EXECUTE_SELECTOR) :]))
    assert commands == bytes([0x0B, 0x00])
    _, amount_in, amount_min, path, payer = decode(
        ["address", "uint256", "uint256", "bytes", "bool"], inputs[1]
    )
    assert (amount_in, amount_min, payer) == (10**17, 1, False)
    assert path == encode_v3_path((cfg.weth, cfg.usdc, cfg.target_ca), (cfg.usdc_weth_fee, 3000))

    data = sell_calldata_usdc(
        cfg,
        amount_in_laptop=10**18,
        amount_out_min_weth=5,
        laptop_usdc_fee=3000,
        wallet=cfg.target_ca,
    )
    commands, inputs = decode(["bytes", "bytes[]"], bytes.fromhex(data[len(EXECUTE_SELECTOR) :]))
    assert commands == bytes([0x00, 0x0C])
    _, amount_in, amount_min, path, payer = decode(
        ["address", "uint256", "uint256", "bytes", "bool"], inputs[0]
    )
    assert (amount_in, amount_min, payer) == (10**18, 5, True)
    assert path == encode_v3_path((cfg.target_ca, cfg.usdc, cfg.weth), (3000, cfg.usdc_weth_fee))


def test_usdc_multi_hop_quote_sends_exact_input_path() -> None:
    cfg = ExecutionConfig()
    monitor = object.__new__(Monitor)
    monitor.config = cfg
    monitor.rpc = Mock()
    raw = (
        "0x"
        + encode(
            ["uint256", "uint160[]", "uint32[]", "uint256"],
            [2000 * 10**18, [2**96, 2**96], [1, 1], 100000],
        ).hex()
    )
    monitor.rpc.dual_eth_call.return_value = DualValue(raw, raw)
    pool = LivePool(3000, cfg.usdc, base=cfg.usdc)
    assert monitor._multi_hop_quote(pool, 10**17, buy=True) == 2000 * 10**18
    data = monitor.rpc.dual_eth_call.call_args.args[1]
    assert data.startswith(_selector("quoteExactInput(bytes,uint256)"))
    path, amount = decode(["bytes", "uint256"], bytes.fromhex(data[10:]))
    assert path == encode_v3_path((cfg.weth, cfg.usdc, cfg.target_ca), (cfg.usdc_weth_fee, 3000))
    assert amount == 10**17


def test_usdc_pool_buy_and_sell_quotes_route_through_multi_hop() -> None:
    cfg = ExecutionConfig()
    monitor = object.__new__(Monitor)
    monitor.config = cfg
    monitor.rpc = Mock()
    monitor.rpc.block_number.return_value = 42
    raw = (
        "0x"
        + encode(["uint256", "uint160[]", "uint32[]", "uint256"], [1000 * 10**18, [], [], 0]).hex()
    )
    monitor.rpc.dual_eth_call.return_value = DualValue(raw, raw)
    pool = LivePool(10000, cfg.usdc, base=cfg.usdc)
    amount, block = monitor._quote_buy(pool, 10**17)
    assert amount == 1000 * 10**18 and block == 42
    assert monitor._quote_sell(pool, 10**18) == 1000 * 10**18


def test_usdc_venue_swap_targets_universal_router_and_returns_native_value() -> None:
    cfg = ExecutionConfig()
    monitor = object.__new__(Monitor)
    monitor.config = cfg
    monitor.wallet = cfg.weth
    monitor.venue_scanner = None
    data, value, router = monitor._venue_swap(
        LivePool(10000, cfg.usdc, base=cfg.usdc), 10**17, 1, buy=True
    )
    assert router == cfg.universal_router and value == 10**17
    assert data.startswith(EXECUTE_SELECTOR)
    _, value2, router2 = monitor._venue_swap(
        LivePool(10000, cfg.usdc, base=cfg.usdc), 10**18, 5, buy=False
    )
    assert router2 == cfg.universal_router and value2 == 0


@pytest.mark.parametrize("decimals", [0, 6, 8, 18, 24, 77])
def test_token_gate_uses_exact_fraction_and_metadata(decimals):
    monitor = object.__new__(Monitor)
    monitor.config = ExecutionConfig.from_json(
        json.dumps(
            {
                "schema": "oasis.launch-execution-config.v2",
                "token_decimals": decimals,
                "reference_supply": 1000000,
                "max_fdv_usd_micros": 1000000000000,
            }
        )
    )
    receipt = 10**decimals
    monitor._quote_buy = Mock(return_value=(receipt, 1))
    pool = LivePool(500, "0x" + "1" * 40)
    minimum, quote, allowed = monitor._buy_gate(pool, 10**18, 1000000)
    assert quote == receipt and allowed is False
    assert minimum == receipt + 1  # exactly $1M FDV must be rejected at a $1M cap
    monitor._quote_buy.return_value = (receipt + 1, 1)
    assert monitor._buy_gate(pool, 10**18, 1000000)[2] is True


def test_token_baseline_rejects_configured_decimal_mismatch_before_trading():
    monitor = object.__new__(Monitor)
    monitor.config = ExecutionConfig.from_json(
        '{"schema":"oasis.launch-execution-config.v2","token_decimals":6}'
    )
    monitor.rpc = Mock()
    value = "0x" + encode(["uint8"], [18]).hex()
    monitor.rpc.dual_eth_call.return_value = DualValue(value, value)
    with pytest.raises(ProviderDisagreement, match="decimals differ"):
        monitor._check_token_baseline()
