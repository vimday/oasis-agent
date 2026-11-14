"""Ethereum execution-header and EIP-1186 account/storage proof validation."""

import re
from dataclasses import dataclass, replace

from eth_hash.auto import keccak
from rlp import DecodingError  # type: ignore[import-untyped]
from rlp import decode as rlp_decode
from rlp import encode as rlp_encode

from ..arbitrage.cycle_evidence import header
from ..models.chain import BlockSnapshot
from .public_rpc_plan import PublicProofPlan
from .rpc_validation import rpc_quantity, strict_list, strict_object

EMPTY_TRIE = keccak(b"\x80")
EMPTY_CODE = keccak(b"")

# RLP field order through Prague, including the EIP-7685 requests commitment.
# Positive widths are fixed byte arrays. Negative widths bound RPC quantities.
PRAGUE_FIELDS = (
    ("parentHash", 32),
    ("sha3Uncles", 32),
    ("miner", 20),
    ("stateRoot", 32),
    ("transactionsRoot", 32),
    ("receiptsRoot", 32),
    ("logsBloom", 256),
    ("difficulty", -32),
    ("number", -8),
    ("gasLimit", -8),
    ("gasUsed", -8),
    ("timestamp", -8),
    ("extraData", 0),
    ("mixHash", 32),
    ("nonce", 8),
    ("baseFeePerGas", -32),
    ("withdrawalsRoot", 32),
    ("blobGasUsed", -8),
    ("excessBlobGas", -8),
    ("parentBeaconBlockRoot", 32),
    ("requestsHash", 32),
)


def hex_bytes(value: object, max_bytes: int, *, exact: bool = False) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) > 2 + 2 * max_bytes
        or (exact and len(value) != 2 + 2 * max_bytes)
        or re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value) is None
    ):
        raise ValueError("invalid_evm_bytes")
    return bytes.fromhex(value[2:])


def quantity(value: object, max_bytes: int) -> int:
    if not isinstance(value, str) or len(value) > 2 + 2 * max_bytes:
        raise ValueError("invalid_evm_quantity_width")
    return rpc_quantity(value)


def prague_header(value: object, number: int, expected_hash: str) -> tuple[BlockSnapshot, str]:
    obj = strict_object(value, "Prague header")
    fields = []
    for name, width in PRAGUE_FIELDS:
        raw = obj.get(name)
        field = quantity(raw, -width) if width < 0 else hex_bytes(raw, width or 32, exact=width > 0)
        fields.append(field)
    block = header(value, 1, number)
    computed = "0x" + keccak(rlp_encode(fields)).hex()
    if computed != block.ref.block_hash or computed != expected_hash:
        raise ValueError("ethereum_header_commitment_mismatch")
    return block, "0x" + hex_bytes(obj["stateRoot"], 32, exact=True).hex()


def decode(raw: bytes) -> object:
    try:
        return rlp_decode(raw)
    except (DecodingError, RecursionError) as exc:
        raise ValueError("invalid_trie_rlp") from exc


def child_ref(value: object, *, allow_empty: bool) -> bytes | list[object]:
    if isinstance(value, bytes) and (len(value) == 32 or (allow_empty and not value)):
        return value
    if isinstance(value, list) and len(rlp_encode(value)) < 32:
        return value
    raise ValueError("invalid_trie_child_reference")


def trie_value(root: bytes, key: bytes, proof: object) -> bytes | None:
    """Walk one secure-trie path, yielding None only on proved exclusion."""
    nodes = strict_list(proof, "trie proof")
    if len(root) != 32 or len(key) != 32 or len(nodes) > 65:
        raise ValueError("invalid_trie_path_bounds")
    if root == EMPTY_TRIE and not nodes:
        return None
    raw_nodes = iter(hex_bytes(node, 1024) for node in nodes)
    remaining = key.hex()
    ref: bytes | list[object] = root
    at_root = True
    result = None
    while ref:
        if isinstance(ref, bytes):
            raw = next(raw_nodes, None)
            if raw is None or (not at_root and len(raw) < 32) or keccak(raw) != ref:
                raise ValueError("trie_node_missing_or_hash_mismatch")
            node = strict_list(decode(raw), "trie node")
        else:
            node = ref
        at_root = False
        if len(node) == 17 and remaining and node[16] == b"":
            ref = child_ref(node[int(remaining[0], 16)], allow_empty=True)
            remaining = remaining[1:]
            continue
        if len(node) != 2 or not isinstance(node[0], bytes) or not node[0]:
            raise ValueError("invalid_trie_node_shape")
        encoded = node[0].hex()
        flag = int(encoded[0], 16)
        if flag > 3 or (flag % 2 == 0 and encoded[1] != "0"):
            raise ValueError("invalid_trie_compact_path")
        path = encoded[1 if flag % 2 else 2 :]
        if flag >= 2:
            if len(path) != len(remaining) or not isinstance(node[1], bytes) or not node[1]:
                raise ValueError("invalid_secure_trie_leaf")
            result = node[1] if path == remaining else None
            break
        if not 0 < len(path) < len(remaining):
            raise ValueError("invalid_secure_trie_extension")
        ref = child_ref(node[1], allow_empty=False)
        if not remaining.startswith(path):
            break
        remaining = remaining[len(path) :]
    if next(raw_nodes, None) is not None:
        raise ValueError("unexpected_extra_trie_nodes")
    return result


def scalar(value: object, max_bytes: int) -> int:
    if not isinstance(value, bytes) or len(value) > max_bytes or value.startswith(b"\0"):
        raise ValueError("noncanonical_trie_integer")
    return int.from_bytes(value, "big")


@dataclass(frozen=True, slots=True)
class ProvenSlot:
    slot: str
    value_raw: int
    present: bool


@dataclass(frozen=True, slots=True)
class ProvenAccount:
    address: str
    present: bool
    nonce: int
    native_balance_wei: int
    code_hash: str
    storage_root: str
    slots: tuple[ProvenSlot, ...]

    def verify_code(self, result: object) -> None:
        # Keep a bounded wire size without imposing a modern deployment limit on old accounts.
        if "0x" + keccak(hex_bytes(result, 256 * 1024)).hex() != self.code_hash:
            raise ValueError("account_proof_code_mismatch")


def account_proof(
    state_root: str, address: str, slots: tuple[str, ...], result: object
) -> ProvenAccount:
    obj = strict_object(result, "account proof")
    address_bytes = hex_bytes(address, 20, exact=True)
    if hex_bytes(obj.get("address"), 20, exact=True) != address_bytes:
        raise ValueError("account_proof_address_mismatch")
    raw = trie_value(
        hex_bytes(state_root, 32, exact=True), keccak(address_bytes), obj.get("accountProof")
    )
    if raw is None:
        nonce, balance, storage_root, code_hash = 0, 0, EMPTY_TRIE, EMPTY_CODE
    else:
        values = strict_list(decode(raw), "state account")
        if len(values) != 4:
            raise ValueError("invalid_state_account_rlp")
        nonce, balance = scalar(values[0], 8), scalar(values[1], 32)
        account_root, account_code = values[2], values[3]
        if not isinstance(account_root, bytes) or len(account_root) != 32:
            raise ValueError("invalid_storage_root")
        if not isinstance(account_code, bytes) or len(account_code) != 32:
            raise ValueError("invalid_account_code_hash")
        storage_root, code_hash = account_root, account_code
    claimed_storage = hex_bytes(obj.get("storageHash"), 32, exact=True)
    claimed_code = hex_bytes(obj.get("codeHash"), 32, exact=True)
    # Geth may report zero hash sentinels for an excluded account. The path, not the
    # RPC sentinel, determines whether its account and storage are empty.
    if raw is None:
        if claimed_storage == bytes(32):
            claimed_storage = EMPTY_TRIE
        if claimed_code == bytes(32):
            claimed_code = EMPTY_CODE
    if (
        quantity(obj.get("nonce"), 8) != nonce
        or quantity(obj.get("balance"), 32) != balance
        or claimed_storage != storage_root
        or claimed_code != code_hash
    ):
        raise ValueError("account_proof_claim_mismatch")
    entries = strict_list(obj.get("storageProof"), "storage proofs")
    if len(entries) != len(slots):
        raise ValueError("storage_proof_coverage_mismatch")
    verified = []
    for slot, entry in zip(slots, entries, strict=True):
        row = strict_object(entry, "storage proof")
        key = hex_bytes(slot, 32, exact=True)
        # Geth preserves 32-byte requested slot keys. Also accept the older quantity form.
        claimed_key = row.get("key")
        matches = (
            hex_bytes(claimed_key, 32, exact=True) == key
            if isinstance(claimed_key, str) and len(claimed_key) == 66
            else quantity(claimed_key, 32) == int.from_bytes(key, "big")
        )
        if not matches:
            raise ValueError("storage_proof_key_mismatch")
        raw_slot = trie_value(storage_root, keccak(key), row.get("proof"))
        value = scalar(decode(raw_slot), 32) if raw_slot is not None else 0
        if (raw_slot is not None and value == 0) or quantity(row.get("value"), 32) != value:
            raise ValueError("storage_proof_value_mismatch")
        verified.append(ProvenSlot(slot, value, raw_slot is not None))
    return ProvenAccount(
        address,
        raw is not None,
        nonce,
        balance,
        "0x" + code_hash.hex(),
        "0x" + storage_root.hex(),
        tuple(verified),
    )


@dataclass(frozen=True, slots=True)
class ProofSnapshot(BlockSnapshot):
    state_root: str
    accounts: tuple[ProvenAccount, ...] = ()


def proof_context(
    plan: PublicProofPlan, index: int, result: object, first: object
) -> ProofSnapshot:
    if index in (1, len(plan.requests()) - 1):
        block, root = prague_header(result, plan.block_number, plan.block_hash)
        snapshot = ProofSnapshot(block.ref, block.parent_hash, root)
        if index == 1:
            return snapshot
        if not isinstance(first, ProofSnapshot) or replace(first, accounts=()) != snapshot:
            raise ValueError("closing_proof_header_changed")
        if len(first.accounts) != len(plan.accounts):
            raise ValueError("incomplete_proof_accounts")
        return first
    if not isinstance(first, ProofSnapshot):
        raise ValueError("proof_without_opening_header")
    position, code_read = divmod(index - 2, 2)
    target = plan.accounts[position]
    if code_read:
        first.accounts[position].verify_code(result)
        return first
    account = account_proof(first.state_root, target.address, target.slots, result)
    return replace(first, accounts=(*first.accounts, account))
