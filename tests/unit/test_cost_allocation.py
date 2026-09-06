import pytest

from oasis_agent.farming.cost_allocation import allocate_cost


@pytest.mark.parametrize("total", [0, 1, 2, 3, 100, 10**30, 2**256 - 1])
def test_allocation_conserves_exact_cost_and_is_order_independent(total):
    weights = (("nft-1", 1), ("nft-2", 2), ("nft-3", 3))
    result = allocate_cost(total, weights)
    assert result == allocate_cost(total, tuple(reversed(weights)))
    assert sum(x.native_units for x in result) == total
    for x in result:
        assert abs(x.native_units * 6 - total * x.weight) < 6


@pytest.mark.parametrize("weights", [(("a", 0),), (("a", True),), (("a", 1), ("a", 2)), ()])
def test_invalid_allocation_rejected(weights):
    with pytest.raises(ValueError):
        allocate_cost(10, weights)
