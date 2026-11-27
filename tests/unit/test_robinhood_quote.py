import pytest
from eth_abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.evidence.robinhood_quote import (
    CHAIN_ID,
    INT128,
    MANAGER,
    PATH_ABI,
    NativeRoute,
    _pool_ids,
    first_grid_input,
    parse_routes,
    quote_multicall,
    read_quotes,
    route_capacity_diagnostics,
)
from oasis_agent.models.common import ZERO_ADDRESS
from oasis_agent.models.v4 import SwapDirection
from oasis_agent.uniswap_v4.quotes import QuotePoolKey
from oasis_agent.uniswap_v4.state_rpc import Slot0State

TOKEN = "0x" + "44" * 20
LEFT = QuotePoolKey(ZERO_ADDRESS, TOKEN, 200, 4, ZERO_ADDRESS)
RIGHT = QuotePoolKey(ZERO_ADDRESS, TOKEN, 500, 10, ZERO_ADDRESS)


def _aggregate(route_results, *, second_fee=500, protocol_fee=0):
    rows = [(True, encode(["uint256"], [CHAIN_ID]))]
    rows.extend((True, encode(["address"], [MANAGER])) for _ in range(2))
    for fee in (200, second_fee):
        rows.extend(
            (
                (
                    True,
                    encode(
                        ["uint160", "int24", "uint24", "uint24"], [1 << 96, 0, protocol_fee, fee]
                    ),
                ),
                (True, encode(["uint128"], [1000])),
            )
        )
    rows.extend(route_results)
    return "0x" + encode(["(bool,bytes)[]"], [rows]).hex()


def test_robinhood_round_trip_abi_and_circuit_checks() -> None:
    route = NativeRoute("control", 111, (LEFT, RIGHT))
    raw = bytes.fromhex(route.quote_data()[2:])
    assert raw[:4] == keccak(f"quoteExactInput({PATH_ABI})".encode())[:4]
    assert decode([PATH_ABI], raw[4:]) == (
        (
            ZERO_ADDRESS,
            ((TOKEN, 200, 4, ZERO_ADDRESS, b""), (ZERO_ADDRESS, 500, 10, ZERO_ADDRESS, b"")),
            111,
        ),
    )
    with pytest.raises(ValueError, match="distinct"):
        NativeRoute("control", 111, (LEFT, LEFT))
    with pytest.raises(ValueError, match="int128"):
        NativeRoute("overflow", INT128, (LEFT, RIGHT))
    pool = QuotePoolKey(TOKEN, "0x" + "55" * 20, 500, 10, ZERO_ADDRESS)
    with pytest.raises(ValueError, match="does not close"):
        NativeRoute("open", 5, (LEFT, pool))
    with pytest.raises(ValueError, match="noncontiguous"):
        NativeRoute("disconnected", 5, (pool, LEFT))
    with pytest.raises(ValueError, match="no-hook"):
        NativeRoute("hook", 5, (LEFT, QuotePoolKey(ZERO_ADDRESS, TOKEN, 100, 1, TOKEN)))


def test_robinhood_quote_reports_reverts_and_optimistic_costs_separately() -> None:
    routes = (
        NativeRoute("left", 100_000, (LEFT, RIGHT)),
        NativeRoute("right", 100_000, (RIGHT, LEFT)),
    )
    calls = decode(["(address,bool,bytes)[]"], bytes.fromhex(quote_multicall(routes)[10:]))[0]
    assert len(calls) == 9
    assert [allow for _, allow, _ in calls] == [False] * 7 + [True, True]
    quote_rows = [(True, encode(["uint256", "uint256"], [150_000, 43_000])), (False, b"\x12\x34")]
    result = read_quotes(_aggregate(quote_rows), routes, 3)
    assert result["whole_transaction_simulated"] is False
    assert result["quotes"][0] == {
        "route": "left",
        "hops": 2,
        "input_wei": 100_000,
        "status": "quoted",
        "output_wei": 150_000,
        "gross_wei": 50_000,
        "quoter_gas_estimate": 43_000,
        "optimistic_net_wei_before_other_costs": -13_000,
    }
    assert result["quotes"][1]["status"] == "quoter_reverted"
    assert result["quotes"][1]["revert_data"] == "0x1234"
    with pytest.raises(ValueError, match="static fee"):
        read_quotes(_aggregate(quote_rows, second_fee=600), routes, 3)
    with pytest.raises(ValueError, match="fee encoding"):
        read_quotes(_aggregate(quote_rows, protocol_fee=1001), routes, 3)


def test_robinhood_quote_scope_does_not_accept_ambiguous_input() -> None:
    with pytest.raises(ValueError, match="JSON integer"):
        parse_routes({"left": {"pools": [], "amount_in": True}})
    with pytest.raises(ValueError, match="unexpected"):
        parse_routes({"left": {"pools": [], "amount_in": 10, "token": TOKEN}})


def test_native_shadow_router_prepays_principal_and_collects_full_credit() -> None:
    route = NativeRoute("control", 111, (LEFT, RIGHT))
    raw = bytes.fromhex(route.shadow_router_data(120, 500)[2:])
    assert raw[:4] == keccak(b"execute(bytes,bytes[],uint256)")[:4]
    command, (plan,), deadline = decode(("bytes", "bytes[]", "uint256"), raw[4:])
    assert (command, deadline) == (bytes([0x10]), 500)
    actions, (settle, swap, take) = decode(("bytes", "bytes[]"), plan)
    assert actions == bytes([0x0B, 0x07, 0x0F])
    assert decode(("address", "uint256", "bool"), settle) == (ZERO_ADDRESS, 111, False)
    assert decode(
        ("(address,(address,uint24,int24,address,bytes)[],uint256[],uint128,uint128)",), swap
    ) == ((ZERO_ADDRESS, route.path(), (), 111, 120),)
    assert decode(("address", "uint256"), take) == (ZERO_ADDRESS, 120)
    for amount, when in ((110, 500), (True, 500), (INT128, 500), (120, 0), (120, True)):
        with pytest.raises(ValueError):
            route.shadow_router_data(amount, when)


def test_grid_distance_reproduces_retained_fourth_leg_without_claiming_capacity() -> None:
    pool = QuotePoolKey(
        ZERO_ADDRESS, "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18", 10_353, 207, ZERO_ADDRESS
    )
    slot = Slot0State(25_260_409_414_693_572_635_910_712_454_721, 115_299, 4_097_000, 10_353)
    grid = first_grid_input(pool, slot, 23_714_791_908_981_046_884, SwapDirection.ZERO_FOR_ONE)
    assert grid is not None
    assert (grid.tick, grid.fee_pips, grid.input_raw) == (115_299, 11_343, 689_656_662)
    assert grid.input_raw < 98_962_916_720_681  # retained three-hop prefix output in ETH wei


def test_grid_distance_uses_signed_tick_and_packed_fee_direction() -> None:
    slot = Slot0State(1 << 96, -1, (17 << 12) | 8, 500)
    left = first_grid_input(RIGHT, slot, 1000, SwapDirection.ZERO_FOR_ONE)
    right = first_grid_input(RIGHT, slot, 1000, SwapDirection.ONE_FOR_ZERO)
    assert left is not None and right is not None
    assert (left.tick, left.fee_pips, left.input_raw) == (-10, 508, 2)
    assert (right.tick, right.fee_pips, right.input_raw) == (0, 517, 0)
    incoherent = Slot0State(1 << 100, -1, 0, 500)
    assert first_grid_input(RIGHT, incoherent, 1000, SwapDirection.ZERO_FOR_ONE) is None


@pytest.mark.parametrize("failure", ["wrapped", "direct", "trailing_bytes", "unrelated_pool"])
def test_route_diagnostics_associate_exact_reverts_without_guessing_internal_inputs(
    failure: str,
) -> None:
    route = NativeRoute("control", 100_000, (LEFT, RIGHT))
    _, target_id = _pool_ids((route,))
    reported = bytes.fromhex("11" * 32) if failure == "unrelated_pool" else target_id
    payload = keccak(b"NotEnoughLiquidity(bytes32)")[:4] + reported
    if failure != "direct":
        payload = keccak(b"UnexpectedRevertBytes(bytes)")[:4] + encode(["bytes"], [payload])
    if failure == "trailing_bytes":
        payload += b"\x00"
    result = read_quotes(_aggregate([(False, payload)]), (route,), 3)
    (report,) = route_capacity_diagnostics((route,), result["pools"], result["quotes"])
    first, second = report["legs"]
    assert first["amount_in_raw"] == route.amount
    assert first["amount_exceeds_first_grid"] is True
    assert first["quoter_reported_liquidity_failure"] is False
    assert second["currency_in"] == TOKEN
    assert second["direction"] == "one_for_zero"
    assert second["amount_in_raw"] is second["amount_exceeds_first_grid"] is None
    assert second["quoter_reported_liquidity_failure"] is (failure in {"direct", "wrapped"})
