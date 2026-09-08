"""Collect block-pinned, two-provider Uniswap v4 single-pool quote evidence.

The probe checks the Quoter, Universal Router and PoolManager runtime identities,
verifies the Quoter's PoolManager binding, and samples exact-input and exact-output
quotes in both directions over a size grid.  Provider URLs are resolved only from
named environment variables and are never written to the output.  This command is
observe-only: its report always sets ``execution_eligible`` to false.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from eth_abi.abi import decode
from eth_hash.auto import keccak

from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json, normalize_address
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange
from oasis_agent.uniswap_v4.quotes import (
    QuoteDirection,
    QuoteKind,
    QuotePoolKey,
    SinglePoolQuoteRequest,
    decode_single_pool_quote,
    encode_single_pool_quote,
)

CONFIG_SCHEMA = "oasis.route-probe.config.v1"
REPORT_SCHEMA = "oasis.route-probe.v1"
HASH_ROLES = frozenset({"pool_manager", "quoter", "universal_router"})
POOL_MANAGER_SELECTOR = "0x" + keccak(b"poolManager()")[:4].hex()
UNEXPECTED_REVERT_BYTES = bytes.fromhex("6190b2b0")
NOT_ENOUGH_LIQUIDITY = bytes.fromhex("7a5ed734")
PRICE_LIMIT_ALREADY_EXCEEDED = bytes.fromhex("7c9c6e8f")


class CurlPoster:
    """Use the system TLS stack without leaking an endpoint in exceptions."""

    def __init__(self, url: str) -> None:
        self._url = url

    def post(self, payload: bytes) -> bytes:
        command = [
            "/usr/bin/curl",
            "--http1.1",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            self._url,
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
        ]
        try:
            return subprocess.run(
                command, input=payload, capture_output=True, check=True, timeout=25
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            code = getattr(exc, "returncode", None)
            suffix = "" if code is None else f" (exit {code})"
            raise OSError(f"RPC HTTPS request failed{suffix}") from None


@dataclass(frozen=True, slots=True)
class Endpoint:
    name: str
    url_env: str
    url: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.name):
            raise ValueError("provider name must be a safe non-empty label")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", self.url_env):
            raise ValueError("url_env must be an uppercase environment variable name")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("RPC provider must resolve to an HTTPS URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("RPC provider URL must not contain userinfo or a fragment")


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    chain_id: int
    endpoints: tuple[Endpoint, ...]
    pool_manager: str
    quoter: str
    universal_router: str
    pool: QuotePoolKey
    probes: tuple[SinglePoolQuoteRequest, ...]
    expected_hashes: dict[str, str]

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("chain_id must be a positive integer")
        if not self.endpoints:
            raise ValueError("at least one provider is required")
        names = [item.name for item in self.endpoints]
        envs = [item.url_env for item in self.endpoints]
        urls = [item.url for item in self.endpoints]
        if len(names) != len(set(names)) or len(envs) != len(set(envs)):
            raise ValueError("provider names and url_env values must be unique")
        if len(urls) != len(set(urls)):
            raise ValueError("provider endpoints must be different")
        for name in ("pool_manager", "quoter", "universal_router"):
            object.__setattr__(self, name, normalize_address(getattr(self, name)))
            if int(getattr(self, name), 16) == 0:
                raise ValueError(f"{name} must be nonzero")
        if not isinstance(self.expected_hashes, dict):
            raise TypeError("expected_hashes must be an object")
        unknown = set(self.expected_hashes) - HASH_ROLES
        if unknown:
            raise ValueError(f"unknown expected hash roles: {sorted(unknown)}")
        normalized_hashes: dict[str, str] = {}
        for role, value in self.expected_hashes.items():
            if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
                raise ValueError(f"{role} expected runtime hash must be bytes32 hex")
            normalized_hashes[role] = value.lower()
        object.__setattr__(self, "expected_hashes", normalized_hashes)
        groups: dict[tuple[QuoteKind, QuoteDirection], list[int]] = {}
        for probe in self.probes:
            groups.setdefault((probe.kind, probe.direction), []).append(probe.amount)
        required = {(kind, direction) for kind in QuoteKind for direction in QuoteDirection}
        if set(groups) != required:
            raise ValueError("probes must cover exact input/output in both directions")
        for key, amounts in groups.items():
            if len(set(amounts)) < 3:
                raise ValueError(f"{key[0].value}/{key[1].value} requires at least three sizes")


_ROOT_KEYS = frozenset(
    {
        "schema",
        "chain_id",
        "providers",
        "pool_manager",
        "quoter",
        "universal_router",
        "pool_key",
        "probes",
        "expected_hashes",
    }
)
_POOL_KEYS = frozenset({"currency0", "currency1", "fee", "tick_spacing", "hooks"})
_PROBE_KEYS = frozenset({"kind", "direction", "amount", "hook_data"})
_PROVIDER_KEYS = frozenset({"name", "url_env"})


def _strict_keys(value: dict[str, object], allowed: frozenset[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {name} keys: {sorted(unknown)}")


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{name} must be an object")
    return value


def load_config(path: Path, env: dict[str, str] | None = None) -> ProbeConfig:
    environment = os.environ if env is None else env
    raw = _object(json.loads(path.read_text(encoding="utf-8")), "config")
    _strict_keys(raw, _ROOT_KEYS, "config")
    if raw.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"schema must be {CONFIG_SCHEMA}")

    endpoint_items = raw.get("providers")
    if not isinstance(endpoint_items, list):
        raise TypeError("providers must be a list")
    endpoints: list[Endpoint] = []
    for index, item in enumerate(endpoint_items):
        provider = _object(item, f"providers[{index}]")
        _strict_keys(provider, _PROVIDER_KEYS, f"providers[{index}]")
        name = provider.get("name")
        url_env = provider.get("url_env")
        if not isinstance(name, str) or not isinstance(url_env, str):
            raise TypeError("provider name and url_env must be strings")
        url = environment.get(url_env)
        if not url:
            raise ValueError(f"RPC URL environment variable is missing: {url_env}")
        endpoints.append(Endpoint(name, url_env, url))

    pool_raw = _object(raw.get("pool_key"), "pool_key")
    _strict_keys(pool_raw, _POOL_KEYS, "pool_key")
    pool = QuotePoolKey(
        str(pool_raw.get("currency0")),
        str(pool_raw.get("currency1")),
        pool_raw.get("fee"),  # type: ignore[arg-type]
        pool_raw.get("tick_spacing"),  # type: ignore[arg-type]
        str(pool_raw.get("hooks")),
    )

    probe_items = raw.get("probes")
    if not isinstance(probe_items, list):
        raise TypeError("probes must be a list")
    probes: list[SinglePoolQuoteRequest] = []
    for index, item in enumerate(probe_items):
        probe = _object(item, f"probes[{index}]")
        _strict_keys(probe, _PROBE_KEYS, f"probes[{index}]")
        try:
            kind = QuoteKind(probe.get("kind"))
            direction = QuoteDirection(probe.get("direction"))
        except ValueError as exc:
            raise ValueError(f"invalid probes[{index}] kind or direction") from exc
        hook_data = probe.get("hook_data", "0x")
        if not isinstance(hook_data, str):
            raise TypeError("probe hook_data must be a hex string")
        probes.append(
            SinglePoolQuoteRequest(kind, direction, probe.get("amount"), hook_data)  # type: ignore[arg-type]
        )

    expected = _object(raw.get("expected_hashes", {}), "expected_hashes")
    return ProbeConfig(
        raw.get("chain_id"),  # type: ignore[arg-type]
        tuple(endpoints),
        str(raw.get("pool_manager")),
        str(raw.get("quoter")),
        str(raw.get("universal_router")),
        pool,
        tuple(probes),
        {key: str(value) for key, value in expected.items()},
    )


def _decode_address(value: str) -> str:
    raw = bytes.fromhex(value[2:]) if value.startswith("0x") else b""
    if len(raw) != 32 or any(raw[:12]):
        raise ValueError("address result must be one canonical ABI word")
    decoded = decode(["address"], raw, strict=True)[0]
    return normalize_address(str(decoded))


def _exchange_envelope(exchange: RpcExchange) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "method": exchange.method,
            "request_id": exchange.request_id,
            "attempt": exchange.attempt,
            "payload": exchange.request_payload.hex(),
        },
        {
            "method": exchange.method,
            "request_id": exchange.request_id,
            "attempt": exchange.attempt,
            "payload": exchange.response_payload.hex(),
        },
    )


def _classify_quote_error(exchange: RpcExchange) -> dict[str, object]:
    """Extract stable custom-error facts without copying an RPC message into reports."""

    try:
        response = json.loads(exchange.response_payload)
        error = response.get("error")
        data = error.get("data") if isinstance(error, dict) else None
        if not isinstance(data, str) or not data.startswith("0x"):
            return {"reason": "rpc_error"}
        raw = bytes.fromhex(data[2:])
        if raw[:4] == UNEXPECTED_REVERT_BYTES:
            (inner,) = decode(["bytes"], raw[4:], strict=True)
            raw = bytes(inner)
        if raw[:4] == NOT_ENOUGH_LIQUIDITY and len(raw) == 36:
            (pool_id,) = decode(["bytes32"], raw[4:], strict=True)
            return {
                "reason": "not_enough_liquidity",
                "revert_selector": "0x" + raw[:4].hex(),
                "pool_id": "0x" + bytes(pool_id).hex(),
            }
        if raw[:4] == PRICE_LIMIT_ALREADY_EXCEEDED and len(raw) == 68:
            current, limit = decode(["uint160", "uint160"], raw[4:], strict=True)
            return {
                "reason": "price_limit_already_exceeded",
                "revert_selector": "0x" + raw[:4].hex(),
                "sqrt_price_current_x96": int(current),
                "sqrt_price_limit_x96": int(limit),
            }
        if len(raw) >= 4:
            return {"reason": "unknown_revert", "revert_selector": "0x" + raw[:4].hex()}
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return {"reason": "rpc_error"}


def _persist(
    store: EvidenceStore,
    source: str,
    block: BlockRef,
    retrieved_at: str,
    exchange: RpcExchange,
    fact: object,
    interpretation: object,
) -> EvidenceRef:
    request, response = _exchange_envelope(exchange)
    return store.put(
        "v4_route_probe",
        canonical_json(fact),
        block,
        source,
        request=request,
        raw_response=response,
        interpretation=interpretation,
        retrieved_at=retrieved_at,
        schema_version=1,
    )


def _impact_ppm(
    request: SinglePoolQuoteRequest, quoted: int, base_amount: int, base_quoted: int
) -> int:
    if quoted <= 0 or base_quoted <= 0:
        raise ValueError("successful quotes must be nonzero")
    if request.kind is QuoteKind.EXACT_INPUT:
        numerator = base_quoted * request.amount - quoted * base_amount
        denominator = base_quoted * request.amount
    else:
        numerator = base_amount * quoted - request.amount * base_quoted
        denominator = base_amount * quoted
    return max(0, numerator * 1_000_000 // denominator)


def _quote_rows(report: dict[str, object]) -> list[dict[str, object]]:
    value = report.get("quotes")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError("internal provider report has invalid quotes")
    return value


def _required_int(row: dict[str, object], name: str) -> int:
    value = row.get(name)
    if type(value) is not int:
        raise TypeError(f"internal quote {name} must be an integer")
    return value


def _write_manifest(directory: Path) -> None:
    lines = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != "MANIFEST.sha256":
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (directory / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(config: ProbeConfig, block_number: int, output: Path) -> dict[str, object]:
    if block_number <= 0:
        raise ValueError("block must be positive")
    if output.exists():
        raise ValueError("output directory must not already exist")
    output.mkdir(parents=True)
    store = EvidenceStore(output / "evidence.db")
    retrieved_at = datetime.now(UTC).isoformat()
    provider_reports: dict[str, dict[str, object]] = {}
    all_refs: list[EvidenceRef] = []
    pinned_block: BlockRef | None = None
    try:
        for endpoint in config.endpoints:
            recorder = BoundedExchangeRecorder(max_exchanges=1000, max_bytes=20_000_000)
            rpc = RpcClient(
                JsonRpcTransport(
                    endpoint.url,
                    http=CurlPoster(endpoint.url),
                    retries=1,
                    recorder=recorder,
                ),
                expected_chain_id=config.chain_id,
            )
            start = rpc.get_block_snapshot(block_number).ref
            if start.timestamp is None:
                raise ValueError("pinned block timestamp is missing")
            if pinned_block is None:
                pinned_block = start
            elif start != pinned_block:
                raise ValueError("providers disagree on pinned block header")
            report: dict[str, object] = {"url_env": endpoint.url_env, "runtime_hashes": {}}
            runtime_hashes: dict[str, str] = {}
            for role, address in (
                ("pool_manager", config.pool_manager),
                ("quoter", config.quoter),
                ("universal_router", config.universal_router),
            ):
                before = len(recorder.exchanges)
                code = rpc.get_code(address, block_number)
                exchange = recorder.exchanges[before]
                digest = "0x" + keccak(bytes.fromhex(code[2:])).hex()
                runtime_hashes[role] = digest
                all_refs.append(
                    _persist(
                        store,
                        endpoint.name,
                        start,
                        retrieved_at,
                        exchange,
                        {"role": role, "address": address, "runtime_keccak": digest},
                        {"schema": REPORT_SCHEMA, "runtime_keccak": digest},
                    )
                )
            report["runtime_hashes"] = runtime_hashes

            before = len(recorder.exchanges)
            binding = _decode_address(rpc.call(config.quoter, POOL_MANAGER_SELECTOR, block_number))
            exchange = recorder.exchanges[before]
            report["quoter_pool_manager"] = binding
            all_refs.append(
                _persist(
                    store,
                    endpoint.name,
                    start,
                    retrieved_at,
                    exchange,
                    {"quoter": config.quoter, "pool_manager": binding},
                    {"schema": REPORT_SCHEMA, "binding_matches": binding == config.pool_manager},
                )
            )

            quote_rows: list[dict[str, object]] = []
            for probe in config.probes:
                calldata = encode_single_pool_quote(config.pool, probe)
                before = len(recorder.exchanges)
                row: dict[str, object] = {
                    "kind": probe.kind.value,
                    "direction": probe.direction.value,
                    "amount": probe.amount,
                    "hook_data_sha256": hashlib.sha256(
                        bytes.fromhex(probe.hook_data[2:])
                    ).hexdigest(),
                }
                try:
                    result = decode_single_pool_quote(
                        rpc.call(config.quoter, calldata, block_number)
                    )
                    row.update(
                        status="ok",
                        quoted_amount=result.quoted_amount,
                        gas_estimate=result.gas_estimate,
                    )
                except (RpcError, ValueError, TypeError):
                    if len(recorder.exchanges) == before:
                        raise
                    row.update(status="error", **_classify_quote_error(recorder.exchanges[before]))
                exchange = recorder.exchanges[before]
                all_refs.append(
                    _persist(
                        store,
                        endpoint.name,
                        start,
                        retrieved_at,
                        exchange,
                        row,
                        {"schema": REPORT_SCHEMA, **row},
                    )
                )
                quote_rows.append(row)
            report["quotes"] = quote_rows
            end = rpc.get_block_snapshot(block_number).ref
            if end != start:
                raise ValueError("pinned header changed during provider acquisition")
            provider_reports[endpoint.name] = report

        assert pinned_block is not None
        hash_sets = {
            role: {str(report["runtime_hashes"][role]) for report in provider_reports.values()}  # type: ignore[index]
            for role in HASH_ROLES
        }
        hashes_match = all(len(values) == 1 for values in hash_sets.values())
        expected_complete = set(config.expected_hashes) == HASH_ROLES
        expected_match = expected_complete and all(
            next(iter(hash_sets[role])) == config.expected_hashes[role] for role in HASH_ROLES
        )
        bindings = {str(report["quoter_pool_manager"]) for report in provider_reports.values()}
        identities_verified = (
            len(provider_reports) >= 2
            and hashes_match
            and expected_match
            and bindings == {config.pool_manager}
        )
        quote_sets: list[set[tuple[object, ...]]] = []
        for index in range(len(config.probes)):
            quote_sets.append(
                {
                    tuple(sorted(_quote_rows(report)[index].items()))
                    for report in provider_reports.values()
                }
            )
        quotes_agree = len(provider_reports) >= 2 and all(len(values) == 1 for values in quote_sets)
        all_quotes_ok = all(
            _quote_rows(report)[index].get("status") == "ok"
            for report in provider_reports.values()
            for index in range(len(config.probes))
        )

        canonical_quotes: list[dict[str, object]] = []
        if provider_reports:
            canonical_quotes = [
                dict(item) for item in _quote_rows(next(iter(provider_reports.values())))
            ]
            groups: dict[tuple[str, str], list[dict[str, object]]] = {}
            for row in canonical_quotes:
                groups.setdefault((str(row["kind"]), str(row["direction"])), []).append(row)
            for rows in groups.values():
                rows.sort(key=lambda item: _required_int(item, "amount"))
                if all(row.get("status") == "ok" for row in rows):
                    base = rows[0]
                    kind = QuoteKind(str(base["kind"]))
                    base_request = SinglePoolQuoteRequest(
                        kind,
                        QuoteDirection(str(base["direction"])),
                        _required_int(base, "amount"),
                    )
                    for row in rows:
                        request = SinglePoolQuoteRequest(
                            kind,
                            QuoteDirection(str(row["direction"])),
                            _required_int(row, "amount"),
                        )
                        row["impact_vs_smallest_ppm"] = _impact_ppm(
                            request,
                            _required_int(row, "quoted_amount"),
                            base_request.amount,
                            _required_int(base, "quoted_amount"),
                        )

        route_probe_verified = identities_verified and quotes_agree and all_quotes_ok
        report = {
            "schema": REPORT_SCHEMA,
            "retrieved_at": retrieved_at,
            "block": asdict(pinned_block),
            "contracts": {
                "pool_manager": config.pool_manager,
                "quoter": config.quoter,
                "universal_router": config.universal_router,
            },
            "pool_key": asdict(config.pool),
            "provider_count": len(provider_reports),
            "providers": provider_reports,
            "canonical_quotes": canonical_quotes,
            "expected_hashes_complete": expected_complete,
            "runtime_hashes_match": hashes_match,
            "expected_hashes_match": expected_match,
            "quoter_binding_verified": bindings == {config.pool_manager},
            "quotes_agree": quotes_agree,
            "all_quotes_ok": all_quotes_ok,
            "identities_verified": identities_verified,
            "route_probe_verified": route_probe_verified,
            "limitations": [
                "Direct V4Quoter success does not prove Universal Router or UI route selection.",
                "Quoted depth is not settled depth and does not prove organic order flow.",
                "A separate broadcast canary and receipt-level reconciliation are required.",
            ],
            "evidence": [asdict(reference) for reference in all_refs],
            "execution_eligible": False,
        }
        (output / "route-probe.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output / "README.md").write_text(
            "# Uniswap v4 route probe\n\n"
            f"- Chain/block: `{config.chain_id}` / `{pinned_block.number}` / "
            f"`{pinned_block.block_hash}`\n"
            f"- Providers: `{len(provider_reports)}`\n"
            f"- Identities verified: `{str(identities_verified).lower()}`\n"
            f"- Direct quote grid verified: `{str(route_probe_verified).lower()}`\n"
            "- Execution eligible: `false`\n\n"
            "This is observe-only direct Quoter evidence. Router selection, settled depth, "
            "organic flow, canary receipts, and accounting remain separate gates.\n",
            encoding="utf-8",
        )
        _write_manifest(output)
        return report
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    report = run(config, args.block, args.output)
    print(
        json.dumps(
            {key: report[key] for key in ("block", "route_probe_verified", "execution_eligible")},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
