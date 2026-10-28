"""Interpret retained Solana Pyth account fields; never authorize NAV or execution.

Layout: pyth-network/pyth-crosschain c2b6cf416412b3282831e67f62b43fea496823d8,
PriceUpdateV2 (receiver SDK) and PriceFeedMessage (pythnet SDK). This supports
only the observed 134-byte, Full, exponent -8, zero-padding subset. Owner and
Full are RPC-reported fields, not proofs of program execution or signatures.
"""

import hashlib
import struct
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

UPSTREAM_COMMIT = "c2b6cf416412b3282831e67f62b43fea496823d8"
RECEIVER = "rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ"
# Sponsored shard-0 Core accounts; pro-compatible accounts are intentionally absent.
FEEDS = MappingProxyType(
    {
        "ETH": (
            "42amVS4KgzR9rA28tkVYqVXjq9Qa8dcZQMbH5EYFX6XC",
            "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
        ),
        "USDC": (
            "Dpw1EAVrSB1ibxiDQyTAW6Zip3J4Btk2x4SgApQCeFbX",
            "eaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
        ),
    }
)
DISCRIMINATOR = hashlib.sha256(b"account:PriceUpdateV2").digest()[:8]
MAX_AGE_MS = 1_800_000


@dataclass(frozen=True, slots=True)
class PythAccountFields:
    """Untrusted decoded observation. Integer price/confidence units are 10^-8 USD."""

    symbol: str
    account_address: str
    account_sha256: str
    write_authority_hex: str
    price_integer: int
    confidence_integer: int
    publish_time: int
    previous_publish_time: int
    ema_price_integer: int
    ema_confidence_integer: int
    posted_slot: int
    context_slot: int
    context_block_time: int
    observed_at_ms: int

    @property
    def oracle_validated(self) -> Literal[False]:
        return False

    @property
    def execution_eligible(self) -> Literal[False]:
        return False


def decode_pyth_account(
    raw: bytes,
    *,
    symbol: str,
    account_address: str,
    reported_owner: str,
    executable: bool,
    context_slot: int,
    context_block_time: int,
    observed_at_ms: int,
) -> PythAccountFields:
    """Decode one observation at its explicit clock, with the owner's 1800s ceiling.

    No I/O, quorum, genesis/block proof, PDA, deployed code or upgrade-authority
    verification is performed. The caller still owns request/response provenance.
    The returned fields are not a price accepted by any production valuation path.
    """
    if not isinstance(symbol, str) or symbol not in FEEDS:
        raise ValueError("unsupported Pyth feed")
    address, feed_id = FEEDS[symbol]
    if account_address != address or reported_owner != RECEIVER or executable is not False:
        raise ValueError("Pyth account identity or executable mismatch")
    for value in (context_slot, context_block_time, observed_at_ms):
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError("bounded integer Pyth observation context required")
    if type(raw) is not bytes or len(raw) != 134 or raw[:8] != DISCRIMINATOR:
        raise ValueError("unsupported Pyth account layout")
    if raw[40] != 1 or raw[133] != 0:
        raise ValueError("Full field and supported padding required")
    if raw[41:73].hex() != feed_id:
        raise ValueError("Pyth feed identity mismatch")
    price, confidence, exponent, published, previous, ema, ema_confidence, posted = struct.unpack(
        "<qQiqqqQQ", raw[73:133]
    )
    if exponent != -8 or not price > confidence >= 0:
        raise ValueError("unsupported Pyth price or confidence")
    if not 0 < previous <= published <= context_block_time or not 0 < posted <= context_slot:
        raise ValueError("inconsistent Pyth publish time or posted slot")
    # Check milliseconds without flooring age: 1800s + 1ms must refuse.
    if not (
        0 <= observed_at_ms - published * 1000 <= MAX_AGE_MS
        and 0 <= observed_at_ms - context_block_time * 1000 <= MAX_AGE_MS
    ):
        raise ValueError("stale or future Pyth observation")
    return PythAccountFields(
        symbol=symbol,
        account_address=address,
        account_sha256=hashlib.sha256(raw).hexdigest(),
        write_authority_hex=raw[8:40].hex(),
        price_integer=price,
        confidence_integer=confidence,
        publish_time=published,
        previous_publish_time=previous,
        ema_price_integer=ema,
        ema_confidence_integer=ema_confidence,
        posted_slot=posted,
        context_slot=context_slot,
        context_block_time=context_block_time,
        observed_at_ms=observed_at_ms,
    )
