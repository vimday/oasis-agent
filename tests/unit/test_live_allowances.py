"""Allowance decisions must agree at the same canonical decision block."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import encode

from oasis_agent.live.chain import ChainError, Rpc, calldata
from tests.unit.test_live_lp import CONFIG, live_loop, market


@dataclass
class AllowanceRpc(Rpc):
    usdc: int = CONFIG.approval_usdc
    permits: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    calls: list[Any] = field(default_factory=list)

    def call(self, method: str, params: list[Any]) -> str:
        assert method == "eth_call"
        self.calls.append(params)
        # A response for latest cannot establish the allowance at the decision.
        assert params[1] == {"blockHash": market().block_hash, "requireCanonical": True}
        data = params[0]["data"]
        if params[0]["to"] == CONFIG.pool.currency1:
            expected = calldata(
                "allowance(address,address)",
                ["address", "address"],
                [CONFIG.account, CONFIG.permit2],
            )
            assert data == "0x" + expected.hex()
            return "0x" + encode(["uint256"], [self.usdc]).hex()
        assert params[0]["to"] == CONFIG.permit2
        for spender in (CONFIG.position_manager, CONFIG.universal_router):
            expected = calldata(
                "allowance(address,address,address)",
                ["address"] * 3,
                [CONFIG.account, CONFIG.pool.currency1, spender],
            )
            if data == "0x" + expected.hex():
                values = self.permits.get(
                    spender, (CONFIG.approval_usdc, market().timestamp + 200_000, 3)
                )
                return "0x" + encode(["uint160", "uint48", "uint48"], values).hex()
        raise AssertionError("unexpected allowance request")


@pytest.mark.parametrize("condition", ["enough", "usdc", "manager", "router"])
def test_approval_decisions_read_canonical_hash_on_both_providers(
    tmp_path: Path, condition: str
) -> None:
    a, b = AllowanceRpc(), AllowanceRpc()
    if condition == "usdc":
        a.usdc = b.usdc = 0
    elif condition in {"manager", "router"}:
        spender = CONFIG.position_manager if condition == "manager" else CONFIG.universal_router
        a.permits[spender] = b.permits[spender] = (CONFIG.approval_usdc, 0, 3)
    result = live_loop(tmp_path, a, b)._approval_needed(market())
    if condition == "enough":
        assert result is None
    else:
        assert result is not None
        assert result[0] == ("approve_usdc" if condition == "usdc" else "approve_permit2")
        if condition != "usdc":
            assert result[1]["spender"] == spender
    assert a.calls == b.calls and a.calls


@pytest.mark.parametrize("difference", ["usdc", "amount", "expiration", "nonce"])
def test_allowance_disagreement_refuses_instead_of_picking_one_provider(
    tmp_path: Path, difference: str
) -> None:
    a, b = AllowanceRpc(), AllowanceRpc()
    if difference == "usdc":
        b.usdc = 0
    else:
        values = [CONFIG.approval_usdc, market().timestamp + 200_000, 3]
        values[["amount", "expiration", "nonce"].index(difference)] += 1
        b.permits[CONFIG.position_manager] = tuple(values)
    loop = live_loop(tmp_path, a, b)
    with pytest.raises(ChainError, match="allowance providers disagree"):
        loop._approval_needed(market())
    assert loop.state.pending is None
