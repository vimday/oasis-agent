"""ABI for the constructor-disabled Ethereum V2 ordered-batch shadow probe."""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from ..arbitrage.cycle_plan import address
from ..arbitrage.cycle_protocols import abi, calldata, data_bytes
from ..models.common import ZERO_ADDRESS

SHADOW_CALLER = "0x000000000000000000000000000000000000d35c"
SHADOW_PROBE = "0x000000000000000000000000000000000000d35d"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
MAX_CAPITAL = 10**19
MAX_ROUTES = 8
MAX_LEGS = 24
_ROUTES = "(address[],address[],uint256,uint256)[]"
_RESULT = ("address[]", "uint256[]", "uint256[]", "uint256[][]", "uint256")


@dataclass(frozen=True, slots=True)
class BatchProbeResult:
    assets: tuple[str, ...]
    start_balances: tuple[int, ...]
    end_balances: tuple[int, ...]
    route_amounts: tuple[tuple[int, ...], ...]
    function_body_gas: int


def build_batch_calldata(
    routes: Sequence[tuple[Sequence[str], Sequence[str], int, int]],
) -> str:
    """Encode, in order, the assets, pools, Sushi mask and fixed input of each cycle."""
    if not 1 <= len(routes) <= MAX_ROUTES:
        raise ValueError("shadow batch route count out of range")
    total_legs = 0
    encoded = []
    for assets_raw, pools_raw, sushi_mask, amount_in in routes:
        hops = len(pools_raw)
        total_legs += hops
        if not 2 <= hops <= 6 or total_legs > MAX_LEGS or len(assets_raw) != hops + 1:
            raise ValueError("shadow batch hop count or asset path out of range")
        assets = tuple(map(address, assets_raw))
        pools = tuple(map(address, pools_raw))
        if (
            assets[0] != WETH
            or assets[-1] != WETH
            or ZERO_ADDRESS in (*assets, *pools)
            or any(a == b for a, b in pairwise(assets))
            or len(set(pools)) != hops
            or type(sushi_mask) is not int
            or not 0 <= sushi_mask < 1 << hops
            or type(amount_in) is not int
            or not 0 < amount_in <= MAX_CAPITAL
        ):
            raise ValueError("invalid shadow route, mask or input")
        encoded.append((assets, pools, sushi_mask, amount_in))
    return calldata(f"observe({_ROUTES})", (_ROUTES,), (tuple(encoded),))


def decode_batch_result(data: str) -> BatchProbeResult:
    """Reject malformed or internally unreconciled output before comparison to the model."""
    if len(data) > 2 + 2 * 8192:
        raise ValueError("shadow batch result exceeds byte budget")
    assets, starts, ends, amounts, gas = abi(_RESULT, data_bytes(data))
    if (
        not 2 <= len(assets) <= MAX_LEGS + 1
        or len(assets) != len(starts)
        or len(assets) != len(ends)
        or len(set(assets)) != len(assets)
        or assets[0] != WETH
        or ZERO_ADDRESS in assets
        or not 0 < starts[0] <= MAX_CAPITAL
        or any(starts[1:])
        or starts[1:] != ends[1:]
        or not 1 <= len(amounts) <= MAX_ROUTES
        or sum(len(route) - 1 for route in amounts) > MAX_LEGS
        or not gas
    ):
        raise ValueError("invalid shadow batch receipt")
    bankroll = starts[0]
    for route in amounts:
        if not 3 <= len(route) <= 7 or route[0] > min(bankroll, MAX_CAPITAL) or not all(route):
            raise ValueError("invalid shadow batch per-route amounts")
        bankroll = bankroll - route[0] + route[-1]
    if ends[0] != bankroll:
        raise ValueError("shadow batch WETH amounts do not reconcile")
    return BatchProbeResult(assets, starts, ends, amounts, gas)
