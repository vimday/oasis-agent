"""Verify ordered receipt commitments for the observed Elysium Nitro header profile."""

from dataclasses import dataclass

from eth_account import Account
from eth_hash.auto import keccak
from eth_keys.constants import SECPK1_N
from rlp import encode as rlp_encode  # type: ignore[import-untyped]

from ..arbitrage.cycle_evidence import header
from ..arbitrage.cycle_plan import address, hash32
from ..arbitrage.cycle_protocols import abi
from ..models.chain import BlockSnapshot, LogRecord
from .public_receipts import decode_receipt
from .rpc_validation import strict_list, strict_object
from .state_trie import EMPTY_TRIE, PRAGUE_FIELDS, hex_bytes, quantity

ELYSIUM_CHAIN_ID = 99901
MAX_RECEIPTS = 512
SYSTEM_TYPE = 0x6A
ARBOS = "0x00000000000000000000000000000000000a4b05"
START_BLOCK_ABI = ("uint256", "uint64", "uint64", "uint64")
START_BLOCK_SELECTOR = keccak(b"startBlock(uint256,uint64,uint64,uint64)")[:4]
SUPPORTED_TYPES = frozenset({0, 2, SYSTEM_TYPE})


def compact(path: str, *, leaf: bool) -> bytes:
    flag = (2 if leaf else 0) + len(path) % 2
    prefix = format(flag, "x") + ("0" if len(path) % 2 == 0 else "")
    return bytes.fromhex(prefix + path)


def trie_node(items: list[tuple[str, bytes]]) -> list[object]:
    if len(items) == 1:
        key, value = items[0]
        return [compact(key, leaf=True), value]

    first, last = items[0][0], items[-1][0]
    shared = 0
    while shared < min(len(first), len(last)) and first[shared] == last[shared]:
        shared += 1

    def child(entries: list[tuple[str, bytes]]) -> object:
        node = trie_node(entries)
        raw = rlp_encode(node)
        return node if len(raw) < 32 else keccak(raw)

    if shared:
        return [
            compact(first[:shared], leaf=False),
            child([(key[shared:], value) for key, value in items]),
        ]
    branch: list[object] = [b""] * 17
    groups: dict[int, list[tuple[str, bytes]]] = {}
    for key, value in items:
        if key:
            groups.setdefault(int(key[0], 16), []).append((key[1:], value))
        else:
            branch[16] = value
    for nibble, group in groups.items():
        branch[nibble] = child(group)
    return branch


def indexed_trie_root(values: list[bytes]) -> bytes:
    """Derive the non-secure trie keyed by RLP(transaction index), including index 0."""
    if not values:
        return EMPTY_TRIE
    return keccak(
        rlp_encode(
            trie_node(
                sorted((rlp_encode(index).hex(), value) for index, value in enumerate(values))
            )
        )
    )


def bloom_bits(logs: tuple[LogRecord, ...]) -> int:
    bits = 0
    for log in logs:
        for entry in (log.address, *log.topics):
            digest = keccak(bytes.fromhex(entry[2:]))
            for offset in (0, 2, 4):
                index = int.from_bytes(digest[offset : offset + 2], "big") & 2047
                bits |= 1 << index
    return bits


@dataclass(frozen=True, slots=True)
class NitroHeader(BlockSnapshot):
    transactions: tuple[str, ...]
    receipts_root: str
    transactions_root: str
    state_root: str
    logs_bloom: int
    gas_used: int


def nitro_header(value: object, number: int, expected_hash: str) -> NitroHeader:
    obj = strict_object(value, "Nitro header")
    if any(k in obj for k, _ in PRAGUE_FIELDS[16:]):
        raise ValueError("unsupported_nitro_optional_header")
    fields = [
        quantity(obj.get(name), -width)
        if width < 0
        else hex_bytes(obj.get(name), width or 32, exact=width > 0)
        for name, width in PRAGUE_FIELDS[:16]
    ]
    base = header(value, ELYSIUM_CHAIN_ID, number)
    if "0x" + keccak(rlp_encode(fields)).hex() != base.ref.block_hash or (
        base.ref.block_hash != expected_hash
    ):
        raise ValueError("nitro_header_commitment_mismatch")
    raw = strict_list(obj.get("transactions"), "Nitro transactions")
    if len(raw) > MAX_RECEIPTS:
        raise ValueError("nitro_receipt_block_size_limit")
    txs = tuple(hash32(item) for item in raw)
    if len(set(txs)) != len(txs):
        raise ValueError("nitro_duplicate_transaction")
    return NitroHeader(
        base.ref,
        base.parent_hash,
        txs,
        hash32(obj.get("receiptsRoot")),
        hash32(obj.get("transactionsRoot")),
        hash32(obj.get("stateRoot")),
        int.from_bytes(hex_bytes(obj.get("logsBloom"), 256, exact=True), "big"),
        quantity(obj.get("gasUsed"), 8),
    )


def nitro_state_header(value: object, number: int, expected_hash: str) -> tuple[BlockSnapshot, str]:
    validated = nitro_header(value, number, expected_hash)
    return validated, validated.state_root


@dataclass(frozen=True, slots=True)
class CommittedReceipt:
    tx_hash: str
    index: int
    type: int
    status: int
    gas_used: int
    log_count: int
    log_addresses: tuple[str, ...]
    sender: str
    recipient: str | None


def verify_receipt_root(block: NitroHeader, response: object) -> tuple[CommittedReceipt, ...]:
    rows = strict_list(response, "Nitro block receipts")
    if len(rows) != len(block.transactions):
        raise ValueError("nitro_receipt_coverage_mismatch")
    values = []
    summaries = []
    cumulative, next_log_index, aggregate_bloom = 0, 0, 0
    for index, (item, tx_hash) in enumerate(zip(rows, block.transactions, strict=True)):
        raw = strict_object(item, "receipt")
        observed = decode_receipt(raw, block.ref, tx_hash, index)
        kind = quantity(raw.get("type"), 1)
        if kind not in SUPPORTED_TYPES or (kind == SYSTEM_TYPE) != (index == 0):
            raise ValueError("unsupported_nitro_receipt_type_or_position")
        cumulative += observed.gas_used
        if cumulative != quantity(raw.get("cumulativeGasUsed"), 8):
            raise ValueError("nitro_receipt_cumulative_gas_mismatch")
        indices = [log.log_index for log in observed.logs]
        if indices != list(range(next_log_index, next_log_index + len(indices))):
            raise ValueError("nitro_receipt_global_log_index_mismatch")
        next_log_index += len(indices)
        bloom = hex_bytes(raw.get("logsBloom"), 256, exact=True)
        bits = bloom_bits(observed.logs)
        if int.from_bytes(bloom, "big") != bits:
            raise ValueError("nitro_receipt_bloom_mismatch")
        aggregate_bloom |= bits
        logs = [
            [
                bytes.fromhex(log.address[2:]),
                [bytes.fromhex(t[2:]) for t in log.topics],
                bytes.fromhex(log.data[2:]),
            ]
            for log in observed.logs
        ]
        payload = rlp_encode([observed.status, cumulative, bloom, logs])
        values.append((bytes([kind]) if kind else b"") + payload)
        summaries.append(
            CommittedReceipt(
                tx_hash,
                index,
                kind,
                observed.status,
                observed.gas_used,
                len(logs),
                tuple(sorted({log.address for log in observed.logs})),
                observed.sender,
                observed.recipient,
            )
        )
    if cumulative != block.gas_used or aggregate_bloom != block.logs_bloom:
        raise ValueError("nitro_block_gas_or_bloom_mismatch")
    if "0x" + indexed_trie_root(values).hex() != block.receipts_root:
        raise ValueError("nitro_receipt_commitment_mismatch")
    return tuple(summaries)


@dataclass(frozen=True, slots=True)
class StartBlock:
    parent_chain_base_fee_wei: int
    reported_parent_chain_block: int
    l2_block: int
    elapsed_seconds: int


def transaction_envelope(
    raw: dict[str, object], block: NitroHeader, index: int
) -> tuple[bytes, StartBlock | None]:
    """Reconstruct consensus bytes in memory from a retained full-block result."""
    if (
        hash32(raw.get("hash")) != block.transactions[index]
        or hash32(raw.get("blockHash")) != block.ref.block_hash
        or quantity(raw.get("blockNumber"), 8) != block.ref.number
        or quantity(raw.get("transactionIndex"), 4) != index
        or quantity(raw.get("chainId"), 8) != ELYSIUM_CHAIN_ID
    ):
        raise ValueError("nitro_transaction_position_or_chain_mismatch")
    kind = quantity(raw.get("type"), 1)
    data = hex_bytes(raw.get("input"), 128 * 1024)
    sender = address(raw.get("from"))
    if kind == SYSTEM_TYPE:
        if (
            index != 0
            or sender != ARBOS
            or raw.get("to") != ARBOS
            or any(quantity(raw.get(name), 32) for name in ("nonce", "gas", "value", "v", "r", "s"))
            or data[:4] != START_BLOCK_SELECTOR
        ):
            raise ValueError("unsupported_nitro_system_transaction")
        action = StartBlock(*abi(START_BLOCK_ABI, data[4:]))
        # The reference precompile accepts the L2 block number carried in this message.
        if action.l2_block != block.ref.number:
            raise ValueError("nitro_start_block_number_mismatch")
        return bytes([kind]) + rlp_encode([ELYSIUM_CHAIN_ID, data]), action
    if index == 0 or kind not in {0, 2} or "to" not in raw:
        raise ValueError("unsupported_nitro_transaction_type")
    dest = hex_bytes(raw["to"], 20, exact=True) if raw["to"] is not None else b""
    r, s = (quantity(raw.get(name), 32) for name in ("r", "s"))
    if not 0 < r < SECPK1_N or not 0 < s <= SECPK1_N // 2:
        raise ValueError("nitro_transaction_signature_bounds")
    nonce = quantity(raw.get("nonce"), 8)
    call = [quantity(raw.get("gas"), 8), dest, quantity(raw.get("value"), 32), data]
    v = quantity(raw.get("v"), 8)
    if kind == 0:
        if v not in (2 * ELYSIUM_CHAIN_ID + 35, 2 * ELYSIUM_CHAIN_ID + 36):
            raise ValueError("nitro_legacy_signature_chain")
        signed = rlp_encode([nonce, quantity(raw.get("gasPrice"), 32), *call, v, r, s])
    else:
        if v not in (0, 1) or quantity(raw.get("yParity"), 1) != v:
            raise ValueError("nitro_transaction_parity_disagreement")
        access = []
        for item in strict_list(raw.get("accessList"), "Nitro access list"):
            row = strict_object(item, "Nitro access list entry")
            keys = strict_list(row.get("storageKeys"), "Nitro access list keys")
            access.append(
                [
                    hex_bytes(row.get("address"), 20, exact=True),
                    [hex_bytes(key, 32, exact=True) for key in keys],
                ]
            )
        tip, fee = (
            quantity(raw.get(name), 32) for name in ("maxPriorityFeePerGas", "maxFeePerGas")
        )
        if tip > fee:
            raise ValueError("nitro_transaction_fee_bounds")
        signed = bytes([kind]) + rlp_encode(
            [
                ELYSIUM_CHAIN_ID,
                nonce,
                tip,
                fee,
                *call,
                access,
                v,
                r,
                s,
            ]
        )
    try:
        recovered = Account.recover_transaction(signed).lower()
    except Exception as exc:
        raise ValueError("nitro_transaction_sender_recovery") from exc
    if recovered != sender:
        raise ValueError("nitro_transaction_sender_mismatch")
    return signed, None


def verify_transactions_root(
    block: NitroHeader, full_block: object, receipts: tuple[CommittedReceipt, ...]
) -> StartBlock:
    obj = strict_object(full_block, "Nitro full block")
    txs = strict_list(obj.get("transactions"), "Nitro full transactions")
    if len(txs) != len(block.transactions) or len(txs) != len(receipts):
        raise ValueError("nitro_transaction_coverage_mismatch")
    header_projection = {**obj, "transactions": list(block.transactions)}
    if nitro_header(header_projection, block.ref.number, block.ref.block_hash) != block:
        raise ValueError("nitro_full_block_context_mismatch")
    values = []
    start = None
    for index, item in enumerate(txs):
        raw = strict_object(item, "Nitro full transaction")
        receipt = receipts[index]
        value, action = transaction_envelope(raw, block, index)
        to = raw.get("to")
        if (
            "0x" + keccak(value).hex() != block.transactions[index]
            or (receipt.tx_hash, receipt.index, receipt.type)
            != (block.transactions[index], index, quantity(raw.get("type"), 1))
            or (receipt.sender, receipt.recipient)
            != (address(raw.get("from")), address(to) if to is not None else None)
        ):
            raise ValueError("nitro_transaction_hash_or_receipt_type_mismatch")
        if action is not None:
            start = action
        values.append(value)
    if start is None or "0x" + indexed_trie_root(values).hex() != block.transactions_root:
        raise ValueError("nitro_transaction_commitment_mismatch")
    return start
