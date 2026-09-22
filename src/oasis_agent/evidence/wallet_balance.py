"""Raw-RPC replay of a pinned native wallet balance; never a USD or trading claim."""

from dataclasses import asdict, dataclass

from ..models.common import BlockRef, EvidenceRef, canonical_json, normalize_address
from ..rpc.transport import RpcExchange
from .rpc_validation import decode_exchange, restore_exchanges, rpc_quantity, strict_json_object
from .store import EvidenceStore


@dataclass(frozen=True, slots=True)
class NativeWalletSnapshot:
    account: str
    block: BlockRef
    native_wei: int

    def __post_init__(self) -> None:
        if self.account != normalize_address(self.account) or int(self.account, 16) == 0:
            raise ValueError("canonical nonzero wallet required")
        if self.block.timestamp is None:
            raise ValueError("timestamped wallet snapshot required")
        if type(self.native_wei) is not int or not 0 <= self.native_wei < 2**256:
            raise ValueError("native balance must be uint256 wei")

    @property
    def payload(self) -> bytes:
        return canonical_json(asdict(self))


def reconstruct_native_balance(
    exchanges: tuple[RpcExchange, ...], *, account: str, block: BlockRef
) -> NativeWalletSnapshot:
    methods = [exchange.method for exchange in exchanges]
    discovery = len(exchanges) == 5
    expected = ["eth_chainId", "eth_getBlockByNumber", "eth_getBalance", "eth_getBlockByNumber"]
    if discovery:
        expected.insert(1, "eth_getBlockByNumber")
    if methods != expected:
        raise ValueError(
            "wallet capture must include chain, pinned header, balance and header recheck"
        )
    decoded = [decode_exchange(exchange) for exchange in exchanges]
    if any("error" in response for _, response in decoded):
        raise ValueError("wallet capture contains an RPC error")
    if decoded[0][0] != [] or rpc_quantity(decoded[0][1]["result"]) != block.chain_id:
        raise ValueError("wallet capture chain mismatch")
    if discovery:
        params, response = decoded.pop(1)
        head = response["result"]
        if params != ["latest", False] or not isinstance(head, dict):
            raise ValueError("invalid wallet head discovery")
        if rpc_quantity(head.get("number")) < block.number:
            raise ValueError("wallet discovery precedes pinned block")
    headers = []
    for index in (1, 3):
        params, response = decoded[index]
        header = response["result"]
        if params != [hex(block.number), False] or not isinstance(header, dict):
            raise ValueError("wallet header is not number-pinned")
        if (
            rpc_quantity(header.get("number")) != block.number
            or header.get("hash") != block.block_hash
            or rpc_quantity(header.get("timestamp")) != block.timestamp
        ):
            raise ValueError("wallet block identity mismatch")
        headers.append(header)
    if headers[0] != headers[1]:
        raise ValueError("wallet header changed during capture")
    params, response = decoded[2]
    if params != [account, hex(block.number)]:
        raise ValueError("wallet balance account/block mismatch")
    return NativeWalletSnapshot(account, block, rpc_quantity(response["result"]))


def replay_native_balance(store: EvidenceStore, reference: EvidenceRef) -> NativeWalletSnapshot:
    if (
        reference.kind != "wallet_native_snapshot"
        or reference.schema_version != 1
        or reference.block is None
    ):
        raise ValueError("unsupported native wallet evidence")
    record = store.get_record(reference)
    if record.request_payload is None:
        raise ValueError("wallet raw request missing")
    # The first one-off diagnostic retained exact hex bytes with no encoding label.
    # Accept only that precisely defined legacy shape; never alter stored evidence.
    envelopes = []
    for raw in (record.request_payload, record.raw_response_payload):
        envelope = strict_json_object(raw)
        if set(envelope) == {"exchanges"}:
            envelope = {"encoding": "hex_exact_bytes", **envelope}
        envelopes.append(canonical_json(envelope))
    exchanges = restore_exchanges(envelopes[0], envelopes[1])
    payload = strict_json_object(record.payload)
    account = payload.get("account")
    if not isinstance(account, str):
        raise ValueError("wallet account missing")
    snapshot = reconstruct_native_balance(exchanges, account=account, block=reference.block)
    if snapshot.payload != record.payload:
        raise ValueError("wallet balance interpretation differs from raw RPC")
    return snapshot
