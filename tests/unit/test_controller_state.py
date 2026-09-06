from dataclasses import replace

import pytest

from oasis_agent.models.finance import ControllerState


def state() -> ControllerState:
    return ControllerState("0x" + "ab" * 32, 4663, 0, 0, 0, 0, 1_000, 1_001, 1, 1)


@pytest.mark.parametrize("chain_id", [0, -1, True, 4663.0])
def test_controller_rejects_invalid_chain_identity(chain_id: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(state(), chain_id=chain_id)


@pytest.mark.parametrize("pool_id", [None, 1, "0x1234", "0x" + "z" * 64])
def test_controller_rejects_malformed_pool_identity(pool_id: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(state(), pool_id=pool_id)


def test_controller_normalizes_hex_case_and_rejects_time_reversal() -> None:
    assert replace(state(), pool_id="0x" + "AB" * 32).pool_id == state().pool_id
    with pytest.raises(ValueError, match="observed_at"):
        replace(state(), observed_at=1_002)
