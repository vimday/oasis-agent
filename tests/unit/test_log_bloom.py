import pytest

from oasis_agent.evidence.log_bloom import log_emitter_mask, possible_log_emitters

_POOL = "0x000000000004444c5dc75cb358380d2e3de08a90"
_OTHER = "0x4200000000000000000000000000000000000006"


def _bloom(mask: int) -> str:
    return "0x" + mask.to_bytes(256, "big").hex()


def test_log_bloom_only_excludes_negative_emitters():
    full = _bloom(log_emitter_mask(_POOL))
    assert possible_log_emitters([_POOL, _OTHER], matching_header_blooms=[full] * 3) == {_POOL}
    both = _bloom(log_emitter_mask(_POOL) | log_emitter_mask(_OTHER))
    assert possible_log_emitters([_POOL, _OTHER], matching_header_blooms=[both] * 2) == {
        _POOL,
        _OTHER,
    }


def test_log_bloom_rejects_missing_malformed_and_conflicting_witnesses():
    empty = _bloom(0)
    with pytest.raises(ValueError, match="two"):
        possible_log_emitters([_POOL], matching_header_blooms=[empty])
    with pytest.raises(ValueError, match="invalid_evm_bytes"):
        possible_log_emitters([_POOL], matching_header_blooms=[empty, "0x00"])
    with pytest.raises(ValueError, match="disagree"):
        possible_log_emitters([_POOL], matching_header_blooms=[empty, _bloom(1)])
