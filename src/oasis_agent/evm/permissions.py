from __future__ import annotations

from ..models.evm import HookPermissions


def decode_hook_permissions(address: str) -> HookPermissions:
    value = int(address, 16) & ((1 << 14) - 1)
    flags = [(value >> bit) & 1 == 1 for bit in range(13, -1, -1)]
    return HookPermissions(*flags)
