"""Static witnesses exclude PUSH data and cannot approve a token."""

from dataclasses import asdict

import pytest
from eth_hash.auto import keccak

from oasis_agent.evm.proxy_indicators import inspect_proxy_indicators


def test_dispatch_target_and_instruction_boundaries_are_required():
    selector = keccak(b"upgradeTo(address)")[:4]
    code = b"\x63" + selector + b"\x14\x60\x09\x57\x5b\x00"
    report = inspect_proxy_indicators(code)
    assert len(report.dispatch) == 1 and report.dispatch[0].destination == 9
    assert report.dispatch[0].candidate_signature == "upgradeTo(address)"
    assert not report.token_admitted and not report.execution_eligible
    for index, value in ((5, 0x11), (7, 8), (8, 0x56), (9, 0x00)):
        changed = bytearray(code)
        changed[index] = value
        assert not inspect_proxy_indicators(bytes(changed)).dispatch


def test_nested_push_payload_is_not_a_delegatecall_dispatch_or_storage_operation():
    data = b"\x63" + keccak(b"admin()")[:4] + b"\x14\x60\x09\x57\x5b\xf4"
    code = bytes([0x5F + len(data)]) + data + b"\x00"
    report = inspect_proxy_indicators(code)
    assert not report.dispatch and not report.delegatecall_offsets
    assert not report.legacy_slot_operations


def test_storage_operations_and_real_delegatecall_are_retained_with_exact_offsets():
    slot = keccak(b"org.zeppelinos.proxy.implementation")
    code = b"\x7f" + slot + b"\x54\x7f" + slot + b"\x55\xf4\x00"
    report = inspect_proxy_indicators(code)
    assert [(x.push_offset, x.operation) for x in report.legacy_slot_operations] == [
        (0, "SLOAD"),
        (34, "SSTORE"),
    ]
    assert report.delegatecall_offsets == (68,)
    assert report.runtime_keccak == "0x" + keccak(code).hex()
    assert asdict(report)["token_admitted"] is False


def test_truncated_push_is_explicit_and_no_absence_of_indicators_passes():
    report = inspect_proxy_indicators(b"\x00\x7f\xf4")
    assert report.truncated_push_offsets == (1,) and not report.delegatecall_offsets
    assert not report.token_admitted and not report.execution_eligible
    assert not inspect_proxy_indicators(b"\x00").token_admitted


@pytest.mark.parametrize("code", [b"", b"\x00" * 24577, "00", bytearray(b"\x00")])
def test_unbounded_or_ambiguous_input_refused(code):
    with pytest.raises(ValueError):
        inspect_proxy_indicators(code)
