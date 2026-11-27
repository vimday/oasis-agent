"""Keep custom conversion evidence scoped to the independently retained observation."""

from dataclasses import replace

from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.models.common import BlockRef
from scripts.replay_eth_tail_case import (
    _REGISTERED_BLOCK,
    _REGISTERED_PATH,
    _REGISTERED_TX,
    WitnessLeg,
    _serial,
)

SENDER = "0x" + "01" * 20
EXECUTOR = "0x" + "02" * 20


def _receipt() -> ObservedReceipt:
    return ObservedReceipt(
        BlockRef(1, 26_478_290, _REGISTERED_BLOCK),
        _REGISTERED_TX,
        5,
        SENDER,
        EXECUTOR,
        1,
        1,
        1,
        (),
    )


def test_external_swap_or_wrapper_signature_cannot_certify_an_unknown_path() -> None:
    unrelated = replace(
        _receipt(),
        transaction_hash="0x" + "12" * 32,
        transaction_index=8,
    )
    report = _serial(unrelated, b"unregistered", {}, (WitnessLeg(12, 10, 11),))
    assert report["status"] == "unresolved"
    assert "distinct_serial_pools" not in report
    assert "gross_raw" not in report


def test_rebound_receipt_and_leg_indices_cannot_replace_the_registered_trace() -> None:
    # Even an input pretending to have the known receipt identity and witness
    # positions cannot use substituted ABI-compatible call evidence.
    forged = b'{"result":{"input":"0xa64833a0","output":"0x01"}}'
    report = _serial(_receipt(), forged, {}, _REGISTERED_PATH)
    assert report["status"] == "unresolved"
    assert "gross_raw" not in report
