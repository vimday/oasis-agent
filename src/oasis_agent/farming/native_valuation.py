"""Replay a pinned ETH/USD reference; incomplete oracle attestation grants no execution."""

from dataclasses import asdict, dataclass

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..evidence.rpc_validation import decode_exchange, restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..evidence.wallet_balance import NativeWalletSnapshot
from ..models.common import BlockRef, EvidenceRef, canonical_json, normalize_address


@dataclass(frozen=True, slots=True)
class EthUsdReference:
    block: BlockRef
    feed: str
    decimals: int
    answer: int
    round_id: int
    started_at: int
    updated_at: int
    answered_in_round: int
    runtime_keccak: str

    @property
    def payload(self) -> bytes:
        return canonical_json(asdict(self))


def replay_eth_usd_reference(
    store: EvidenceStore, reference: EvidenceRef, *, expected_feed: str
) -> EthUsdReference:
    feed = normalize_address(expected_feed)
    if reference.kind != "wallet_eth_usd_reference" or reference.schema_version != 1:
        raise ValueError("unsupported ETH/USD reference")
    block = reference.block
    if block is None or block.timestamp is None:
        raise ValueError("timestamped oracle block required")
    record = store.get_record(reference)
    if record.request_payload is None:
        raise ValueError("oracle raw requests missing")
    exchanges = restore_exchanges(record.request_payload, record.raw_response_payload)
    if [x.method for x in exchanges] != [
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_call",
        "eth_call",
        "eth_getCode",
        "eth_getBlockByNumber",
    ]:
        raise ValueError("oracle witness coverage mismatch")
    values = [decode_exchange(x) for x in exchanges]
    if any("error" in response for _, response in values):
        raise ValueError("oracle witness contains RPC error")
    if values[0][0] != [] or rpc_quantity(values[0][1]["result"]) != block.chain_id:
        raise ValueError("oracle chain mismatch")
    headers = []
    for index in (1, 5):
        params, response = values[index]
        header = response["result"]
        if params != [hex(block.number), False] or not isinstance(header, dict):
            raise ValueError("oracle header not pinned")
        if (
            rpc_quantity(header.get("number")) != block.number
            or header.get("hash") != block.block_hash
            or rpc_quantity(header.get("timestamp")) != block.timestamp
        ):
            raise ValueError("oracle block identity mismatch")
        headers.append(header)
    if headers[0] != headers[1]:
        raise ValueError("oracle header changed")
    decoded = []
    for index, signature, types in (
        (2, "decimals()", ["uint8"]),
        (3, "latestRoundData()", ["uint80", "int256", "uint256", "uint256", "uint80"]),
    ):
        params, response = values[index]
        selector = "0x" + keccak(signature.encode())[:4].hex()
        if params != [{"to": feed, "data": selector}, hex(block.number)]:
            raise ValueError("oracle call binding mismatch")
        wire = response["result"]
        if not isinstance(wire, str) or not wire.startswith("0x"):
            raise ValueError("invalid oracle ABI")
        raw = bytes.fromhex(wire[2:])
        abi_values = decode(types, raw)
        if encode(types, abi_values) != raw:
            raise ValueError("noncanonical oracle ABI")
        decoded.append(abi_values)
    decimals = int(decoded[0][0])
    round_id, answer, started, updated, answered = map(int, decoded[1])
    if (
        not 0 <= decimals <= 18
        or answer <= 0
        or answered < round_id
        or not 0 < started <= updated <= block.timestamp
        or block.timestamp - updated > 3600
    ):
        raise ValueError("stale or invalid oracle round")
    params, response = values[4]
    code_hex = response["result"]
    if (
        params != [feed, hex(block.number)]
        or not isinstance(code_hex, str)
        or not code_hex.startswith("0x")
    ):
        raise ValueError("oracle runtime not pinned")
    code = bytes.fromhex(code_hex[2:])
    if not code:
        raise ValueError("oracle runtime missing")
    result = EthUsdReference(
        block,
        feed,
        decimals,
        answer,
        round_id,
        started,
        updated,
        answered,
        "0x" + keccak(code).hex(),
    )
    if result.payload != record.payload:
        raise ValueError("oracle interpretation differs from raw RPC")
    return result


def native_usd_micros(
    snapshot: NativeWalletSnapshot, price: EthUsdReference, *, round_up: bool
) -> int:
    """Same-block ETH reference only; use ceil for commitments, floor for asset reporting."""
    if snapshot.block != price.block or type(round_up) is not bool:
        raise ValueError("native balance and ETH price must share exact block and rounding policy")
    if (
        type(price.answer) is not int
        or price.answer <= 0
        or type(price.decimals) is not int
        or not 0 <= price.decimals <= 18
    ):
        raise ValueError("positive bounded ETH price required")
    numerator = snapshot.native_wei * price.answer * 1_000_000
    denominator = 10**18
    for _ in range(price.decimals):
        denominator *= 10
    return (numerator + denominator - 1) // denominator if round_up else numerator // denominator
