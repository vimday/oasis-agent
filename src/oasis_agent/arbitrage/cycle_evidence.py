"""Read parsed historical exports without promoting them to transport evidence."""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..evidence.rpc_validation import (
    rpc_quantity,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex
from .cycle_plan import address, hash32, utc

MAX_FILES = 128
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 8192
METHODS = {
    "eth_chainId",
    "eth_getBlockByNumber",
    "eth_getTransactionReceipt",
    "eth_getCode",
    "eth_call",
}
PARSER = "oasis-parsed-rpc-export-v1"


class Unavailable(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("offline input size limit exceeded")
    return raw


@dataclass(frozen=True, slots=True)
class Export:
    source: str
    file_sha256: str
    index: int
    endpoint_sha256: str
    fetched: datetime | None
    method: str
    params: tuple[object, ...]
    request_sha256: str
    response_sha256: str
    result: object
    error_code: int | None
    error_selector: str | None = None

    @property
    def identity(self) -> str:
        return "parsed-" + sha256_hex(
            canonical_json(
                [
                    self.source,
                    self.endpoint_sha256,
                    self.fetched.isoformat() if self.fetched else None,
                    self.request_sha256,
                    self.response_sha256,
                ]
            )
        )

    def manifest(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.identity,
            "kind": "parsed_historical_export",
            "source": self.source,
            "endpoint_sha256": self.endpoint_sha256,
            "file_sha256": self.file_sha256,
            "record_index": self.index,
            "parser": PARSER,
            "method": self.method,
            "provider_error_code": self.error_code,
            "export_fetched_utc": self.fetched.isoformat() if self.fetched else None,
            "parsed_request_sha256": self.request_sha256,
            "parsed_response_sha256": self.response_sha256,
            "original_http_request_sha256": None,
            "original_http_response_sha256": None,
            "first_observed_utc": None,
        }
        if self.error_selector is not None:
            result["provider_error_selector"] = self.error_selector
        return result

    def reference(self, block: BlockRef | None) -> dict[str, object]:
        if self.fetched is None:
            return {
                "id": self.identity,
                "block": asdict(block) if block else None,
                "retrieved_at": None,
            }
        ref = EvidenceRef(
            kind="parsed_historical_export",
            source=self.source,
            block=block,
            content_hash=self.identity.removeprefix("parsed-"),
            retrieved_at=self.fetched.isoformat(),
            interpretation_hash=self.response_sha256,
        )
        return {"id": self.identity, **asdict(ref)}


def _decode(value: object, source: str, digest: str, index: int) -> Export:
    entry = strict_object(value, "export")
    if set(entry) != {"endpoint", "fetched_utc", "request", "response"}:
        raise ValueError("export must be a parsed endpoint/fetched_utc/request/response object")
    endpoint = strict_string(entry["endpoint"], "endpoint")
    if not endpoint or len(endpoint) > 2048:
        raise ValueError("invalid endpoint label")
    date = entry["fetched_utc"]
    fetched = utc(strict_string(date, "fetched_utc")) if date is not None else None
    req = strict_object(entry["request"], "parsed request")
    res = strict_object(entry["response"], "parsed response")
    method = strict_string(req.get("method"), "method")
    if method not in METHODS:
        raise ValueError("method outside offline import scope")
    if (
        set(req) != {"jsonrpc", "id", "method", "params"}
        or req["jsonrpc"] != "2.0"
        or res.get("jsonrpc") != "2.0"
        or type(req["id"]) not in (int, str)
        or type(req["id"]) is not type(res.get("id"))
        or req["id"] != res.get("id")
        or set(res) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"})
    ):
        raise ValueError("invalid parsed RPC envelope")
    params = tuple(strict_list(req["params"], "params"))
    error_code = None
    error_selector = None
    if "error" in res:
        error = strict_object(res["error"], "RPC error")
        error_code = strict_integer(error.get("code"), "code")
        error_data = error.get("data")
        # Retain only a bounded bytes4 diagnostic, never hostile messages or revert arguments.
        # A selector match is a hint, not proof of the reverting contract or ABI identity.
        if isinstance(error_data, str) and re.fullmatch(
            r"0x(?:[0-9a-fA-F]{2}){4,4096}", error_data
        ):
            error_selector = error_data[:10].lower()
    return Export(
        source,
        digest,
        index,
        sha256_hex(endpoint.encode()),
        fetched,
        method,
        params,
        sha256_hex(canonical_json(req)),
        sha256_hex(canonical_json(res)),
        res.get("result"),
        error_code,
        error_selector,
    )


def load_exports(
    inputs: Sequence[tuple[str, Path]], sources: tuple[str, ...]
) -> tuple[Export, ...]:
    if len(inputs) > MAX_FILES:
        raise ValueError("too many evidence files")
    records: dict[tuple[str, str, int], Export] = {}
    endpoints: dict[str, str] = {}
    remaining = MAX_TOTAL_BYTES
    for source, path in inputs:
        if source not in sources:
            raise ValueError("input source outside plan allowlist")
        raw = read_bounded(path, min(remaining, MAX_FILE_BYTES))
        remaining -= len(raw)
        digest = sha256_hex(raw)
        obj = strict_json_object(raw)
        if set(obj) == {"schema", "records"} and obj["schema"] == "oasis-parsed-rpc-bundle-v1":
            entries = strict_list(obj["records"], "export records")
            if not entries or len(entries) > MAX_RECORDS:
                raise ValueError("invalid export bundle size")
        else:
            entries = [obj]
        for index, entry in enumerate(entries):
            record = _decode(entry, source, digest, index)
            if endpoints.setdefault(source, record.endpoint_sha256) != record.endpoint_sha256:
                raise ValueError("source alias spans different declared endpoints")
            records[(source, digest, index)] = record
        if len(records) > MAX_RECORDS:
            raise ValueError("too many offline evidence records")
    return tuple(records[key] for key in sorted(records))


def header(value: object, chain_id: int, number: int) -> BlockSnapshot:
    obj = strict_object(value, "block")
    if rpc_quantity(obj.get("number")) != number:
        raise ValueError("block number mismatch")
    timestamp = rpc_quantity(obj.get("timestamp"))
    if timestamp >= 1 << 64:
        raise ValueError("block timestamp overflow")
    return BlockSnapshot(
        BlockRef(chain_id, number, hash32(obj.get("hash")), timestamp),
        hash32(obj.get("parentHash")),
    )


def log_record(value: object, block: BlockRef, tx_hash: str, tx_index: int) -> LogRecord:
    obj = strict_object(value, "receipt log")
    topics = tuple(hash32(v) for v in strict_list(obj.get("topics"), "log topics"))
    if (
        not 1 <= len(topics) <= 4
        or obj.get("removed", False) is not False
        or hash32(obj.get("blockHash")) != block.block_hash
        or rpc_quantity(obj.get("blockNumber")) != block.number
        or hash32(obj.get("transactionHash")) != tx_hash
        or rpc_quantity(obj.get("transactionIndex")) != tx_index
    ):
        raise ValueError("receipt log position mismatch")
    return LogRecord(
        address(obj.get("address")),
        topics,
        strict_string(obj.get("data"), "log data"),
        block.number,
        tx_hash,
        rpc_quantity(obj.get("logIndex")),
        block.block_hash,
        tx_index,
    )


class Reads:
    """A bounded, private view of one evidence cutoff and one research context."""

    def __init__(
        self,
        records: tuple[Export, ...],
        sources: tuple[str, ...],
        limit: int,
        cutoff: datetime | None = None,
    ) -> None:
        self.sources = sources
        self.limit = limit
        self._block: BlockRef | None = None
        self._index: dict[bytes, list[Export]] = defaultdict(list)
        self._cache: dict[bytes, tuple[object, tuple[str, ...], str | None]] = {}
        self.queries: list[dict[str, Any]] = []
        for record in records:
            if cutoff is None or (record.fetched is not None and record.fetched <= cutoff):
                self._index[canonical_json([record.method, record.params])].append(record)

    @property
    def block(self) -> BlockRef | None:
        return self._block

    def bind(self, block: BlockRef) -> None:
        if self._block is not None:
            raise RuntimeError("research context is already bound")
        self._block = block
        header_key = (hex(block.number), False)
        for query in self.queries:
            if query["method"] == "eth_getBlockByNumber" and query["query"] == header_key:
                for reference in query["evidence"]:
                    reference["block"] = asdict(block)

    def get(self, method: str, *params: object) -> tuple[object, tuple[str, ...]]:
        key = canonical_json([method, params])
        if key not in self._cache:
            if len(self._cache) >= self.limit:
                raise Unavailable("offline_query_budget_exhausted")
            self._cache[key] = self._read(key, method, params)
        result, refs, reason = self._cache[key]
        if reason is not None:
            raise Unavailable(reason)
        return result, refs

    def _read(
        self,
        key: bytes,
        method: str,
        params: tuple[object, ...],
    ) -> tuple[object, tuple[str, ...], str | None]:
        matched = self._index.get(key, [])
        result = None
        replies: set[bytes] = set()
        statuses = {}
        for source in self.sources:
            group = [record for record in matched if record.source == source]
            if not group:
                statuses[source] = "missing"
                continue
            if any(record.error_code is not None for record in group):
                statuses[source] = "provider_error"
                continue
            statuses[source] = "retained_reply"
            for record in group:
                result = record.result
                replies.add(canonical_json(result))
        reason = None
        if "provider_error" in statuses.values():
            reason = "provider_call_failed"
        elif len(replies) > 1:
            reason = "provider_results_conflict"
        elif "missing" in statuses.values() or result is None:
            reason = "missing_retained_reply"
        unique = {record.identity: record for record in matched}
        refs = tuple(sorted(unique))
        self.queries.append(
            {
                "method": method,
                "query_sha256": sha256_hex(key),
                "query": params,
                "pin": params[-1] if method in {"eth_call", "eth_getCode"} else None,
                "sources": statuses,
                "evidence": [unique[ref].reference(self.block) for ref in refs],
                "reason": reason,
            }
        )
        return result, refs, reason

    def chain(self, chain_id: int) -> None:
        result, _ = self.get("eth_chainId")
        if rpc_quantity(result) != chain_id:
            raise Unavailable("chain_id_mismatch")

    def pinned(self, method: str, target: object) -> tuple[object, tuple[str, ...]]:
        if self.block is None:
            raise RuntimeError("pinned reads require a verified block header")
        return self.get(
            method, target, {"blockHash": self.block.block_hash, "requireCanonical": True}
        )
