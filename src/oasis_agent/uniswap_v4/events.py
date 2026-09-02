from __future__ import annotations

from ..errors import FeeEncodingError
from ..models.chain import LogRecord
from ..models.v4 import PoolInitialized, PoolKey, SwapObservation

POOL_INITIALIZED_TOPIC = "0x" + ""  # Filled by callers from the configured event signature.


def _words(data: str) -> list[int]:
    raw = bytes.fromhex(data[2:])
    if len(raw) % 32:
        raise ValueError("event data is not word aligned")
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def _signed(value: int, bits: int) -> int:
    if value >= 1 << (bits - 1):
        return value - (1 << bits)
    return value


def decode_pool_initialized(log: LogRecord) -> PoolInitialized:
    if len(log.topics) < 4:
        raise ValueError("PoolInitialized requires id and two currency topics")
    words = _words(log.data)
    if len(words) != 5:
        raise ValueError("PoolInitialized requires five data words")
    fee, tick_spacing, hooks, sqrt_price, tick = words
    key = PoolKey(
        "0x" + log.topics[2][-40:],
        "0x" + log.topics[3][-40:],
        fee,
        _signed(tick_spacing, 24),
        "0x" + f"{hooks:064x}"[-40:],
    )
    return PoolInitialized(log.topics[1], key, sqrt_price, _signed(tick, 24), log.block_number)


def decode_swap(log: LogRecord) -> SwapObservation:
    if len(log.topics) < 2:
        raise ValueError("Swap requires a pool id topic")
    words = _words(log.data)
    if len(words) != 5:
        raise ValueError("Swap requires five data words")
    amount0 = _signed(words[0], 256)
    amount1 = _signed(words[1], 256)
    sqrt_price = words[2]
    liquidity = words[3]
    tick = _signed(words[4], 24)
    sender = "0x" + log.topics[1][-40:] if len(log.topics) > 1 else "0x" + "0" * 40
    return SwapObservation(log.topics[0], sender, amount0, amount1, sqrt_price, liquidity, tick, None, log.block_number)
