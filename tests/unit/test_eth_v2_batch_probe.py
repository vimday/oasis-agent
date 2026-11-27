import pytest
from eth_abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.fastpath.eth_v2_batch_probe import (
    WETH,
    build_batch_calldata,
    decode_batch_result,
)

ASSET = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
POOLS = ("0x" + "11" * 20, "0x" + "22" * 20)
ASSETS = (WETH, ASSET, WETH)
ROUTE_ABI = "(address[],address[],uint256,uint256)[]"
RETURN_ABI = ("address[]", "uint256[]", "uint256[]", "uint256[][]", "uint256")


def test_probe_abi_keeps_fixed_amounts_and_order_with_shared_pools() -> None:
    orders = ((ASSETS, POOLS, 2, 25), (ASSETS, POOLS[::-1], 1, 12))
    raw = bytes.fromhex(build_batch_calldata(orders)[2:])
    assert raw[:4] == keccak(f"observe({ROUTE_ABI})".encode())[:4]
    assert decode((ROUTE_ABI,), raw[4:]) == (orders,)


@pytest.mark.parametrize(
    "orders",
    [
        (),
        ((ASSETS, (POOLS[0], POOLS[0]), 0, 25),),
        ((ASSETS, POOLS, 4, 25),),
        ((ASSETS, POOLS, True, 25),),
        ((ASSETS, POOLS, 0, 0),),
        (((ASSET, WETH, ASSET), POOLS, 0, 25),),
    ],
)
def test_rejects_invalid_batch_inputs(orders) -> None:
    with pytest.raises(ValueError):
        build_batch_calldata(orders)


def _encoded(balances, routes, *, gas=27_293) -> str:
    return "0x" + encode(RETURN_ABI, ((WETH, ASSET), (40, 0), balances, routes, gas)).hex()


def test_batch_result_counts_one_wrap_and_preserves_an_observed_losing_route() -> None:
    output = _encoded((42, 0), ((25, 10, 31), (30, 20, 26)))
    batch = decode_batch_result(output)
    assert batch.assets == (WETH, ASSET)
    assert batch.start_balances == (40, 0)
    assert batch.end_balances == (42, 0)
    assert batch.route_amounts == ((25, 10, 31), (30, 20, 26))
    assert batch.function_body_gas == 27_293


@pytest.mark.parametrize(
    "result",
    [
        _encoded((43, 0), ((25, 10, 31), (30, 20, 26))),
        _encoded((42, 1), ((25, 10, 31), (30, 20, 26))),
        _encoded((42, 0), ((41, 10, 43),)),
        _encoded((42, 0), ((25, 0, 27),)),
        _encoded((42, 0), ((25, 10, 27),), gas=0),
        _encoded((42, 0), ((25, 10, 27),)) + "00",
    ],
)
def test_rejects_unreconciled_noncanonical_or_unsupported_observations(result: str) -> None:
    with pytest.raises(ValueError):
        decode_batch_result(result)
