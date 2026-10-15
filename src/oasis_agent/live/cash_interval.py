"""Shared bounded wallet conservation primitives; no settlement authorization."""

from dataclasses import dataclass
from typing import Literal

from eth_hash.auto import keccak

from ..evidence.exact_capture import RecordedSession, decode_header, successful_results
from ..evidence.rpc_validation import decode_exchange, rpc_quantity
from ..evidence.store import EvidenceStore
from ..farming.transaction_costs import replay_transaction_cost
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef
from ..rpc.transport import RpcExchange
from .config import LiveConfig
from .intent import SubmittedTransaction
from .wallet import WalletBalance, WalletScope, replay_wallet

MAX_CASH_BLOCKS = 128
TRANSFER = "0x" + keccak(b"Transfer(address,address,uint256)").hex()


def gas_only_wallets(
    store: EvidenceStore,
    config: LiveConfig,
    transaction: SubmittedTransaction,
    opening: EvidenceRef,
    closing: EvidenceRef,
    cost_ref: EvidenceRef,
    *,
    expected_status: Literal[0, 1],
) -> tuple[WalletBalance, WalletBalance]:
    before, after = replay_wallet(store, opening), replay_wallet(store, closing)
    if before.scope != after.scope or before.scope != WalletScope.from_config(config):
        raise ValueError("cash wallet scope changed")
    if opening.source != closing.source or opening.source != cost_ref.source:
        raise ValueError("cash proof providers crossed")
    a, b = before.reconstruct(), after.reconstruct()
    tx = transaction
    if tx.chain_id != config.chain_id or tx.account != config.account:
        raise ValueError("cash transaction scope changed")
    if not 0 < b.header.ref.number - a.header.ref.number <= MAX_CASH_BLOCKS:
        raise ValueError("cash interval outside bound")
    if a.nonce != tx.nonce or b.nonce != tx.nonce + 1:
        raise ValueError("cash action must consume exactly its signed nonce")
    capture = replay_transaction_cost(
        store, cost_ref, header=b.header, transaction_hash=tx.transaction_hash
    )
    cost = capture.reconstruct()
    receipt = decode_exchange(capture.exchanges[2])[1]["result"]
    if (
        not isinstance(receipt, dict)
        or receipt.get("to") != tx.target
        or rpc_quantity(receipt.get("gasUsed")) > tx.gas_limit
        or rpc_quantity(receipt.get("effectiveGasPrice")) > tx.max_fee_per_gas
    ):
        raise ValueError("cash receipt differs from transaction target/gas envelope")
    if cost.status != expected_status or cost.payer != config.account or cost.native_decimals != 18:
        raise ValueError("cash action needs its own expected receipt outcome")
    if a.native - b.native != cost.total_native or a.token != b.token:
        raise ValueError("cash wallet residual is unexplained")
    if a.token_runtime_hash != b.token_runtime_hash:
        raise ValueError("cash token runtime changed")
    return a, b


def _logs(config: LiveConfig, block: BlockRef) -> list[tuple[str, list[object]]]:
    owner = "0x" + "0" * 24 + config.account[2:]
    return [
        (
            "eth_getLogs",
            [
                {
                    "address": [config.pool.currency1, config.position_manager],
                    "blockHash": block.block_hash,
                    "topics": topics,
                }
            ],
        )
        for topics in ([TRANSFER, owner], [TRANSFER, None, owner])
    ]


@dataclass(frozen=True, slots=True)
class TransferFreeInterval:
    opening: BlockSnapshot
    headers: tuple[BlockSnapshot, ...]
    exchanges: tuple[RpcExchange, ...]

    def validate(self, config: LiveConfig, closing: BlockSnapshot) -> None:
        count = closing.ref.number - self.opening.ref.number
        if not 0 < count <= MAX_CASH_BLOCKS or len(self.headers) != count:
            raise ValueError("cash interval has a gap or exceeds bound")
        if (
            sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("cash interval byte budget exceeded")
        requests: list[tuple[str, list[object]]] = [
            ("eth_chainId", []),
            ("eth_getBlockByNumber", [hex(self.opening.ref.number), False]),
        ]
        for header in self.headers:
            requests.append(("eth_getBlockByNumber", [hex(header.ref.number), False]))
            requests.extend(_logs(config, header.ref))
        requests.append(("eth_getBlockByNumber", [hex(closing.ref.number), False]))
        values = successful_results(self.exchanges, requests)
        if rpc_quantity(values[0]) != config.chain_id:
            raise ValueError("cash window provider chain mismatch")
        if decode_header(values[1], chain_id=config.chain_id) != self.opening:
            raise ValueError("cash opening block moved")
        previous = self.opening
        for i, header in enumerate(self.headers):
            observed = decode_header(values[2 + i * 3], chain_id=config.chain_id)
            if (
                observed != header
                or header.parent_hash != previous.ref.block_hash
                or header.ref.number != previous.ref.number + 1
                or header.ref.timestamp is None
                or previous.ref.timestamp is None
                or header.ref.timestamp <= previous.ref.timestamp
            ):
                raise ValueError("cash interval is not parent linked")
            if values[3 + i * 3] != [] or values[4 + i * 3] != []:
                raise ValueError("cash interval contains owner token/NFT transfers")
            previous = header
        if previous != closing or decode_header(values[-1], chain_id=config.chain_id) != closing:
            raise ValueError("cash closing block moved")


def capture_interval(
    config: LiveConfig, start: BlockRef, end: BlockRef, session: RecordedSession
) -> tuple[BlockSnapshot, ...]:
    if not 0 < end.number - start.number <= MAX_CASH_BLOCKS:
        raise ValueError("cash interval outside bound")
    transport, _, _ = session
    transport.request("eth_chainId", [])
    transport.request("eth_getBlockByNumber", [hex(start.number), False])
    headers = []
    for height in range(start.number + 1, end.number + 1):
        header = decode_header(
            transport.request("eth_getBlockByNumber", [hex(height), False]),
            chain_id=config.chain_id,
        )
        headers.append(header)
        for method, params in _logs(config, header.ref):
            transport.request(method, params)
    transport.request("eth_getBlockByNumber", [hex(end.number), False])
    return tuple(headers)
