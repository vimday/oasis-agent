"""Universal Router calldata builders (pure, deterministic).

Command layout follows the pinned Uniswap Universal Router runtime
(0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD on Base):
  V3_SWAP_EXACT_IN  = 0x00, input (address,uint256,uint256,bytes,bool)
  UNWRAP_WETH       = 0x06, input (address,uint256)
  execute(bytes commands, bytes[] inputs) payable

Correctness is verified by simulation before any broadcast; these builders
never touch keys or the network.
"""

from eth_abi import encode  # type: ignore[attr-defined]
from eth_utils import keccak, to_checksum_address  # type: ignore[attr-defined]

from .config import ExecutionConfig

EXECUTE_SELECTOR = "0x" + keccak(b"execute(bytes,bytes[])")[:4].hex()
V3_SWAP_EXACT_IN = 0x00
UNWRAP_WETH = 0x06


def path_bytes(tokens: tuple[str, str], fee: int) -> bytes:
    if len(tokens) != 2 or not (0 <= fee < 2**24):
        raise ValueError("path needs exactly two tokens and a 24-bit fee")
    left = to_checksum_address(tokens[0])[2:]
    right = to_checksum_address(tokens[1])[2:]
    return bytes.fromhex(left) + fee.to_bytes(3, "big") + bytes.fromhex(right)


def v3_swap_exact_in_input(
    *,
    recipient: str,
    amount_in: int,
    amount_out_min: int,
    path: bytes,
    payer_is_user: bool,
) -> bytes:
    if amount_in <= 0 or amount_out_min <= 0:
        raise ValueError("positive amounts required")
    return encode(
        ["address", "uint256", "uint256", "bytes", "bool"],
        [to_checksum_address(recipient), amount_in, amount_out_min, path, payer_is_user],
    )


def unwrap_weth_input(*, recipient: str, amount_min: int) -> bytes:
    if amount_min < 0:
        raise ValueError("non-negative amount_min required")
    return encode(
        ["address", "uint256"],
        [to_checksum_address(recipient), amount_min],
    )


def execute_calldata(commands: tuple[int, ...], inputs: tuple[bytes, ...]) -> str:
    if not commands or len(commands) != len(inputs):
        raise ValueError("commands and inputs must be non-empty and equal length")
    for command in commands:
        if not 0 <= command <= 0xFF:
            raise ValueError("command byte out of range")
    commands_bytes = bytes(commands)
    encoded = encode(["bytes", "bytes[]"], [commands_bytes, list(inputs)])
    return EXECUTE_SELECTOR + encoded.hex()


def buy_calldata(
    config: ExecutionConfig,
    *,
    amount_in_wei: int,
    amount_out_min_laptop: int,
    fee: int,
    recipient: str,
) -> tuple[str, int]:
    """Native-ETH exact-in buy of LAPTOP via Uniswap v3.

    Path is WETH -> fee -> LAPTOP; the router wraps msg.value. Returns
    (data, value) where value equals amount_in_wei.
    """
    path = path_bytes((config.weth, config.target_ca), fee)
    swap_input = v3_swap_exact_in_input(
        recipient=recipient,
        amount_in=amount_in_wei,
        amount_out_min=amount_out_min_laptop,
        path=path,
        payer_is_user=False,
    )
    return execute_calldata((V3_SWAP_EXACT_IN,), (swap_input,)), amount_in_wei


def sell_calldata(
    config: ExecutionConfig,
    *,
    amount_in_laptop: int,
    amount_out_min_weth: int,
    fee: int,
    wallet: str,
) -> str:
    """Exact-in sell of LAPTOP for native ETH via Uniswap v3.

    Command 0x00 swaps LAPTOP -> WETH with the router as recipient (Permit2
    pull from the wallet), then command 0x06 unwraps the router WETH to ETH
    sent to the wallet. No msg.value is required.
    """
    path = path_bytes((config.target_ca, config.weth), fee)
    swap_input = v3_swap_exact_in_input(
        recipient=config.universal_router,
        amount_in=amount_in_laptop,
        amount_out_min=amount_out_min_weth,
        path=path,
        payer_is_user=True,
    )
    unwrap_input = unwrap_weth_input(recipient=wallet, amount_min=amount_out_min_weth)
    return execute_calldata((V3_SWAP_EXACT_IN, UNWRAP_WETH), (swap_input, unwrap_input))
