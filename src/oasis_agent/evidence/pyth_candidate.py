"""Replay retained Solana Pyth discovery. Agreement is not oracle authentication."""

import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..rpc.protocol import response_envelope, strict_json_object
from .pyth_price import FEEDS, MAX_AGE_MS, RECEIVER, PythAccountFields, decode_pyth_account
from .rpc_validation import strict_integer, strict_list, strict_object, strict_string

GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
PUSH = "pythWSnswVUd12oZpeFP8e9CVaEqJg25g1Vtc2biRsT"
CONFIG = "DaWUKXCyXsnzcvLUyeJRWou8KTn7XtadgTsdhJ6RHS7b"
LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
INITIAL = (*(address for address, _ in FEEDS.values()), RECEIVER, PUSH, CONFIG)
_MAX_LINE = 22_000_000  # 16 MB response bound, plus its base64 wire envelope.
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    slot: int
    block_hash: str
    block_time: int
    prices: tuple[PythAccountFields, ...]
    account_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateCapture:
    source: str
    wire_sha256: str
    exchanges: tuple[tuple[int, str], ...]
    programdata_addresses: tuple[str, ...]
    snapshots: tuple[CandidateSnapshot, CandidateSnapshot]


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _positive(value: object, name: str) -> int:
    number = strict_integer(value, name)
    if not 0 < number < 2**63:
        raise ValueError(f"{name} out of range")
    return number


def _base58(raw: bytes) -> str:
    value = int.from_bytes(raw, "big")
    suffix = ""
    while value:
        value, digit = divmod(value, 58)
        suffix = _ALPHABET[digit] + suffix
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + suffix


def _block_hash(value: object) -> str:
    label = strict_string(value, "block hash")
    if not 32 <= len(label) <= 44 or any(c not in _ALPHABET for c in label):
        raise ValueError("invalid block hash")
    number = 0
    for c in label:
        number = number * 58 + _ALPHABET.index(c)
    if len(label) - len(label.lstrip("1")) + (number.bit_length() + 7) // 8 != 32 or not number:
        raise ValueError("invalid block hash")
    return label


def _account(value: object, owner: str, *, executable: bool = False) -> bytes:
    account = strict_object(value, "account")
    if account.get("owner") != owner or account.get("executable") is not executable:
        raise ValueError("account owner or executable mismatch")
    data = strict_list(account.get("data"), "account data")
    if len(data) != 2 or data[1] != "base64":
        raise ValueError("account encoding mismatch")
    raw = base64.b64decode(strict_string(data[0], "account bytes"), validate=True)
    if not 0 < len(raw) <= 5_000_000 or strict_integer(account.get("space"), "space") != len(raw):
        raise ValueError("account length mismatch")
    return raw


def _pointer(program: bytes) -> str:
    if len(program) != 36 or program[:4] != b"\x02\0\0\0" or not any(program[4:]):
        raise ValueError("invalid upgradeable program pointer")
    return _base58(program[4:])


def _programdata(raw: bytes, initial_slot: int) -> None:
    if (
        len(raw) <= 49
        or raw[:4] != b"\x03\0\0\0"
        or not 0 < int.from_bytes(raw[4:12], "little") < initial_slot
        or raw[12] not in (0, 1)
        or raw[45:49] != b"\x7fELF"
    ):
        raise ValueError("unsupported ProgramData or deployment during capture")


def replay_pyth_candidate(path: Path) -> CandidateCapture:
    """Replay exactly the five discovery exchanges, without contacting an endpoint."""
    source = ""
    previous_id = previous_time = 0
    wire_hash = hashlib.sha256()
    exchanges: list[tuple[int, str]] = []
    with path.open("rb") as handle:

        def read(method: str) -> tuple[list[object], object, int]:
            nonlocal source, previous_id, previous_time
            line = handle.readline(_MAX_LINE + 1)
            if not line.endswith(b"\n") or len(line) > _MAX_LINE:
                raise ValueError("missing or oversized wire row")
            wire_hash.update(line)
            row = strict_json_object(line)
            alias = strict_string(row.get("source"), "source")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias) or (source and alias != source):
                raise ValueError("wire source mismatch")
            source = alias
            request = strict_object(row.get("request"), "request")
            request_id = _positive(request.get("id"), "request id")
            if (
                set(request) != {"id", "jsonrpc", "method", "params"}
                or request["jsonrpc"] != "2.0"
                or request["method"] != method
                or (previous_id and request_id != previous_id + 1)
                or strict_integer(row.get("status"), "HTTP status") != 200
            ):
                raise ValueError("discovery request or HTTP status mismatch")
            clock = _positive(row.get("retrieved_at_ms"), "retrieval clock")
            if clock < previous_time or (exchanges and clock - exchanges[0][0] > 120_000):
                raise ValueError("discovery retrieval clock mismatch")
            previous_id, previous_time = request_id, clock
            body = base64.b64decode(strict_string(row.get("body_base64"), "body"), validate=True)
            body_hash = _sha(body)
            if len(body) > 16_000_000 or body_hash != row.get("body_sha256"):
                raise ValueError("response size or digest mismatch")
            response = response_envelope(body, request_id)
            if "result" not in response:
                raise ValueError("RPC returned an error")
            exchanges.append((clock, body_hash))
            return strict_list(request["params"], "params"), response["result"], clock

        params, result, _ = read("getGenesisHash")
        if params or result != GENESIS:
            raise ValueError("Solana mainnet genesis mismatch")
        pointers: tuple[str, ...] = ()
        snapshots: list[CandidateSnapshot] = []
        for phase in range(2):
            addresses = [*INITIAL, *pointers]
            params, result, account_clock = read("getMultipleAccounts")
            if params != [addresses, {"encoding": "base64", "commitment": "finalized"}]:
                raise ValueError("unexpected account selection or commitment")
            accounts = strict_object(result, "accounts result")
            context = strict_object(accounts.get("context"), "context")
            slot = _positive(context.get("slot"), "context slot")
            values = strict_list(accounts.get("value"), "accounts")
            if len(values) != len(addresses) or (phase and slot < snapshots[0].slot):
                raise ValueError("account count or context slot mismatch")

            programs = tuple(_account(v, LOADER, executable=True) for v in values[2:4])
            derived = tuple(_pointer(program) for program in programs)
            if len(set(derived)) != 2 or set(derived).intersection(INITIAL):
                raise ValueError("ProgramData pointer aliases an account")
            config = _account(values[4], RECEIVER)
            if config[:8] != hashlib.sha256(b"account:Config").digest()[:8]:
                raise ValueError("unsupported config discriminator")
            programdata = tuple(_account(v, LOADER) for v in values[5:])
            for raw in programdata:
                _programdata(raw, snapshots[0].slot)
            if phase and derived != pointers:
                raise ValueError("ProgramData pointers changed during capture")
            pointers = derived

            params, result, block_clock = read("getBlock")
            if len(params) != 2 or _positive(params[0], "requested slot") != slot:
                raise ValueError("getBlock does not identify the account context")
            options = strict_object(params[1], "block options")
            version = strict_integer(options.get("maxSupportedTransactionVersion"), "version")
            if (
                version not in (0, 1)
                or options
                != {
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": version,
                    "rewards": False,
                    "transactionDetails": "none",
                }
                or options["rewards"] is not False
            ):
                raise ValueError("unexpected block options")
            block = strict_object(result, "block")
            block_hash = _block_hash(block.get("blockhash"))
            block_time = _positive(block.get("blockTime"), "block time")
            if block_time * 1000 > account_clock:
                raise ValueError("account context time follows its retrieval")
            raw_prices = tuple(_account(v, RECEIVER) for v in values[:2])
            prices = tuple(
                decode_pyth_account(
                    raw,
                    symbol=symbol,
                    account_address=FEEDS[symbol][0],
                    reported_owner=RECEIVER,
                    executable=False,
                    context_slot=slot,
                    context_block_time=block_time,
                    observed_at_ms=block_clock,
                )
                for symbol, raw in zip(FEEDS, raw_prices, strict=True)
            )
            hashes = tuple(map(_sha, (*raw_prices, *programs, config, *programdata)))
            snapshot = CandidateSnapshot(slot, block_hash, block_time, prices, hashes)
            if phase:
                first = snapshots[0]
                if hashes[2:5] != first.account_hashes[2:] or block_time < first.block_time:
                    raise ValueError("program, config or block time changed inconsistently")
                _compare_shared_slot(first, snapshot)
            snapshots.append(snapshot)
        if handle.read(1):
            raise ValueError("unexpected trailing wire rows")
    return CandidateCapture(
        source, wire_hash.hexdigest(), tuple(exchanges), pointers, (snapshots[0], snapshots[1])
    )


def _compare_shared_slot(first: CandidateSnapshot, second: CandidateSnapshot) -> None:
    overlap = min(len(first.account_hashes), len(second.account_hashes))
    if first.slot == second.slot and (
        first.block_hash != second.block_hash
        or first.block_time != second.block_time
        or first.account_hashes[:overlap] != second.account_hashes[:overlap]
    ):
        raise ValueError("same-slot block or account bytes disagree")


def compare_pyth_candidates(
    first: CandidateCapture, second: CandidateCapture, *, at_ms: int
) -> str:
    """Return the observed agreement scope at the explicit, unverified caller clock."""
    _positive(at_ms, "review clock")
    if first.source == second.source or first.exchanges == second.exchanges:
        raise ValueError("distinct source captures required")
    for left in first.snapshots:
        for right in second.snapshots:
            _compare_shared_slot(left, right)
    closing = (first.snapshots[1], second.snapshots[1])
    if (
        first.programdata_addresses != second.programdata_addresses
        or closing[0].account_hashes != closing[1].account_hashes
    ):
        raise ValueError("closing Pyth price, deployment or config bytes disagree")
    for snapshot in closing:
        for price in snapshot.prices:
            if not (
                price.observed_at_ms <= at_ms
                and 0 <= at_ms - price.publish_time * 1000 <= MAX_AGE_MS
                and 0 <= at_ms - snapshot.block_time * 1000 <= MAX_AGE_MS
            ):
                raise ValueError("stale or future candidate at review clock")
    return (
        "same_finalized_slot"
        if closing[0].slot == closing[1].slot
        else "matching_bytes_distinct_slots"
    )
