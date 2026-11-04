"""Explicit continuity boundaries after raw wallet replay, with no I/O."""

from dataclasses import replace

import pytest

from oasis_agent.live.wallet import WalletBalance, require_wallet_continuity
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef

ANCHOR = WalletBalance(
    BlockSnapshot(BlockRef(8453, 100, "0x" + "11" * 32, 1800000000), "0x" + "10" * 32),
    4,
    1_000,
    2_000,
    "0x" + "22" * 32,
)


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("case", ["same", "child", "gap"])
def test_same_block_requires_explicit_permission_and_new_blocks_bind_parent_and_time(case, allow):
    current = ANCHOR
    if case != "same":
        gap = 1 if case == "child" else 30
        current = replace(
            current,
            header=BlockSnapshot(
                BlockRef(8453, 100 + gap, "0x" + "44" * 32, 1800000000 + gap),
                ANCHOR.header.ref.block_hash,
            ),
        )
    if case == "same" and not allow:
        with pytest.raises(ValueError):
            require_wallet_continuity(ANCHOR, current, allow_same_block=allow)
    else:
        require_wallet_continuity(ANCHOR, current, allow_same_block=allow)


@pytest.mark.parametrize(
    "fault",
    [
        "nonce",
        "bool_nonce",
        "negative_nonce",
        "units",
        "runtime",
        "parent",
        "chain",
        "time",
        "height",
    ],
)
def test_exact_block_permission_never_accepts_a_conflicting_wallet(fault):
    current = ANCHOR
    if fault in {"nonce", "bool_nonce", "negative_nonce"}:
        current = replace(
            current, nonce={"nonce": 5, "bool_nonce": True, "negative_nonce": -1}[fault]
        )
    elif fault == "units":
        current = replace(current, native=current.native + 1)
    elif fault == "runtime":
        current = replace(current, token_runtime_hash="0x" + "33" * 32)
    else:
        current = replace(
            current,
            header=BlockSnapshot(
                replace(
                    current.header.ref,
                    chain_id=1 if fault == "chain" else 8453,
                    timestamp=1800000001 if fault == "time" else 1800000000,
                    number=99 if fault == "height" else 100,
                ),
                "0x" + "99" * 32 if fault == "parent" else current.header.parent_hash,
            ),
        )
    with pytest.raises(ValueError):
        require_wallet_continuity(ANCHOR, current, allow_same_block=True)


def test_same_block_flag_is_a_boolean_not_a_truthy_default():
    with pytest.raises(ValueError):
        require_wallet_continuity(ANCHOR, ANCHOR, allow_same_block=1)
