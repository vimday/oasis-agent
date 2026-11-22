"""Off-grid sizing regression from Ethereum block 26,471,752, in native token units."""

from scripts.analyze_eth_v2_tail_depth import USDC, WETH, model_route
from scripts.run_eth_v2_forward_shadow import bound_and_quote
from scripts.run_eth_v2_wide_shadow import GRID, envelope, exact_on_integer_sizes


def test_off_grid_optimum_still_fails_the_cost_floor():
    # WETH → AAVE → USDC → CRV → WETH. The second leg has just 2.317011 USDC.
    legs = [
        (16_060_435_856_284_806_125, 279_531_672_934_845_266_560),
        (21_619_756_541_837_918, 2_317_011),
        (5_014_095, 28_302_035_849_193_408_841),
        (28_234_263_209_985_626_003_112, 3_037_076_129_243_668_291),
    ]
    grid_gross = max(bound_and_quote(legs, amount)[0] - amount for amount in GRID)
    optimum, gross, _, _ = exact_on_integer_sizes(legs, midpoint=2)
    assert grid_gross == 186_078_803_207
    assert optimum == 49_460_114_820_510
    assert gross == bound_and_quote(legs, optimum)[0] - optimum == 2_860_521_360_232
    pools = [
        "0xdfc14d2af169b0d36c4eff567ada9b2e0cae044f",
        "0x674e114dad81838d151d9beda2271228eeae0e8b",
        "0x210a97ba874a8e279c95b350ae8ba143a143c159",
        "0x58dc5a51fe44589beb22e8ce67720b5bc5378009",
    ]
    venues = [
        {"address": address, "venue": name}
        for address, name in zip(pools, ("uni", "uni", "uni", "sushi"), strict=True)
    ]
    best = {"assets": ("WETH", "AAVE", "USDC", "CRV", "WETH"), "pools": pools, "input_wei": optimum}
    tx, _ = envelope(best, venues, b"00")
    payload = bytes.fromhex(tx["data"][2:])
    assert tx["value"] == hex(optimum)
    assert len(payload) == 452
    assert payload.count(0) == 263
    intrinsic = 21_000 + sum(4 if byte == 0 else 16 for byte in payload)
    gross_upper = bound_and_quote(legs, optimum)[1]
    assert gross <= gross_upper == 2_860_521_360_269
    assert gross_upper < intrinsic * 123_548_440


def test_repeated_pool_depth_reuses_updated_reserves():
    # Two WETH/USDC pools from Ethereum block 26,471,850, with USDC as token0.
    pools = {
        "uni": {
            "tokens": [USDC, WETH],
            "reserves": [8_415_546_811_430, 4_901_954_343_891_666_524_085],
        },
        "sushi": {
            "tokens": [USDC, WETH],
            "reserves": [110_282_495_106, 64_284_093_781_358_904_925],
        },
    }
    amount = 10**16
    route = ("uni", "sushi") * 200
    mutable = model_route(pools, route, amount, mutable=True)
    frozen = model_route(pools, route, amount, mutable=False)
    assert mutable["completed_hops"] == frozen["completed_hops"] == 400
    assert mutable["output_wei"] == 161_453_557_357_602
    assert frozen["output_wei"] == 3_401_906_358_573_858
    assert mutable["final_touched_reserves"]["uni"] != pools["uni"]["reserves"]
    assert frozen["final_touched_reserves"]["uni"] == pools["uni"]["reserves"]

    for first, second in (("uni", "sushi"), ("sushi", "uni")):
        legs = [pools[first]["reserves"][::-1], pools[second]["reserves"]]
        _, upper, numerator, denominator = bound_and_quote(legs, amount)
        assert numerator < denominator
        assert upper == 0
