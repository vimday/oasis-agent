"""Universal Router calldata builders (pure, deterministic).

Command layout follows the pinned Uniswap Universal Router runtime
(0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD on Base):
  V3_SWAP_EXACT_IN  = 0x00, input (address,uint256,uint256,bytes,bool)
  WRAP_ETH         = 0x0b; UNWRAP_WETH = 0x0c, input (address,uint256)
  execute(bytes commands, bytes[] inputs) payable

Correctness is verified by simulation before any broadcast; these builders
never touch keys or the network.
"""

from eth_abi import encode  # type: ignore[attr-defined]
from eth_utils import keccak, to_checksum_address  # type: ignore[attr-defined]

from .config import ExecutionConfig

EXECUTE_SELECTOR = "0x" + keccak(b"execute(bytes,bytes[])")[:4].hex()
QUOTE_EXACT_INPUT_SELECTOR = "0x" + keccak(b"quoteExactInput(bytes,uint256)")[:4].hex()
V3_SWAP_EXACT_IN = 0x00
WRAP_ETH = 0x0B
UNWRAP_WETH = 0x0C


def encode_v3_path(tokens: tuple[str, ...], fees: tuple[int, ...]) -> bytes:
    """Interleave [token0, fee0, token1, fee1, ..., tokenN] per v3 path layout."""
    if not tokens or len(tokens) != len(fees) + 1:
        raise ValueError("path needs N fees interleaving N+1 tokens")
    out = bytearray()
    for index, fee in enumerate(fees):
        if type(fee) is not int or not 0 <= fee < 2**24:
            raise ValueError("fee must be a 24-bit integer")
        out += bytes.fromhex(to_checksum_address(tokens[index])[2:])
        out += fee.to_bytes(3, "big")
    out += bytes.fromhex(to_checksum_address(tokens[-1])[2:])
    return bytes(out)


def path_bytes(tokens: tuple[str, str], fee: int) -> bytes:
    return encode_v3_path(tokens, (fee,))


def quote_exact_input_calldata(path: bytes, amount_in: int) -> str:
    if type(amount_in) is not int or amount_in <= 0:
        raise ValueError("positive amount_in required")
    if not path:
        raise ValueError("non-empty path required")
    return QUOTE_EXACT_INPUT_SELECTOR + encode(["bytes", "uint256"], [path, amount_in]).hex()


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
    wrap_input = encode(
        ["address", "uint256"], [to_checksum_address(config.universal_router), amount_in_wei]
    )
    return execute_calldata((WRAP_ETH, V3_SWAP_EXACT_IN), (wrap_input, swap_input)), amount_in_wei


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
    pull from the wallet), then command 0x0c unwraps the router WETH to ETH
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


def buy_calldata_usdc(
    config: ExecutionConfig,
    *,
    amount_in_wei: int,
    amount_out_min_laptop: int,
    laptop_usdc_fee: int,
    recipient: str,
) -> tuple[str, int]:
    """Native-ETH two-hop buy WETH -> USDC -> LAPTOP via Uniswap v3.

    The canonical WETH/USDC fee tier comes from config; the LAPTOP/USDC hop fee
    is the discovered pool's fee. The router wraps msg.value to WETH, then swaps
    through both hops. Returns (data, value) with value == amount_in_wei.
    """
    path = encode_v3_path(
        (config.weth, config.usdc, config.target_ca),
        (config.usdc_weth_fee, laptop_usdc_fee),
    )
    swap_input = v3_swap_exact_in_input(
        recipient=recipient,
        amount_in=amount_in_wei,
        amount_out_min=amount_out_min_laptop,
        path=path,
        payer_is_user=False,
    )
    wrap_input = encode(
        ["address", "uint256"], [to_checksum_address(config.universal_router), amount_in_wei]
    )
    return execute_calldata((WRAP_ETH, V3_SWAP_EXACT_IN), (wrap_input, swap_input)), amount_in_wei


def sell_calldata_usdc(
    config: ExecutionConfig,
    *,
    amount_in_laptop: int,
    amount_out_min_weth: int,
    laptop_usdc_fee: int,
    wallet: str,
) -> str:
    """Two-hop sell LAPTOP -> USDC -> WETH for native ETH via Uniswap v3.

    Command 0x00 swaps LAPTOP -> USDC -> WETH with the router as recipient
    (Permit2 pull from the wallet); command 0x0c unwraps the router WETH to ETH.
    """
    path = encode_v3_path(
        (config.target_ca, config.usdc, config.weth),
        (laptop_usdc_fee, config.usdc_weth_fee),
    )
    swap_input = v3_swap_exact_in_input(
        recipient=config.universal_router,
        amount_in=amount_in_laptop,
        amount_out_min=amount_out_min_weth,
        path=path,
        payer_is_user=True,
    )
    unwrap_input = unwrap_weth_input(recipient=wallet, amount_min=amount_out_min_weth)
    return execute_calldata((V3_SWAP_EXACT_IN, UNWRAP_WETH), (swap_input, unwrap_input))
