"""Conservative EVM log-emitter screening for independently pinned headers."""

from collections.abc import Iterable, Sequence

from eth_hash.auto import keccak

from ..arbitrage.cycle_plan import address
from .state_trie import hex_bytes


def log_emitter_mask(emitter: str) -> int:
    """Return the three Ethereum log Bloom bits for the raw 20-byte address."""
    hashed = keccak(bytes.fromhex(address(emitter)[2:]))
    mask = 0
    for offset in (0, 2, 4):
        mask |= 1 << (int.from_bytes(hashed[offset : offset + 2], "big") & 2047)
    return mask


def possible_log_emitters(
    emitters: Iterable[str], *, matching_header_blooms: Sequence[object]
) -> frozenset[str]:
    """Return possible emitters using only agreeing block-level Bloom witnesses.

    The caller binds headers to the same canonical hash and distinct providers.
    A match may be a collision. Absence rules out a log, which is weaker than
    ruling out every storage or balance change.
    """
    if len(matching_header_blooms) < 2:
        raise ValueError("two Base log Bloom witnesses are required")
    blooms = {hex_bytes(item, 256, exact=True) for item in matching_header_blooms}
    if len(blooms) != 1:
        raise ValueError("Base header log Blooms disagree")
    bloom = int.from_bytes(blooms.pop(), "big")
    masks = {emitter: log_emitter_mask(emitter) for emitter in emitters}
    return frozenset(emitter for emitter, mask in masks.items() if bloom & mask == mask)
