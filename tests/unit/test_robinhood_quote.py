import pytest
from eth_abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.evidence.robinhood_quote import (
    CHAIN_ID,
    INT128,
    MANAGER,
    PATH_ABI,
    NativeRoute,
    parse_routes,
    quote_multicall,
    read_quotes,
)
from oasis_agent.models.common import ZERO_ADDRESS
from oasis_agent.uniswap_v4.quotes import QuotePoolKey

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
