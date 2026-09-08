"""Collect and verify deployment identities for an explicitly selected EVM chain.

Observe-only pre-release deployment tool. For the target network and an explicit block
pin it retrieves, from every configured RPC provider, the chain id, the runtime
bytecode keccak of the core v4 runtimes (PoolManager / PositionManager /
StateView / Permit2), hook and executor; their bindings/control state; hook
permission bits; token metadata; and the reconstructed PoolId/slot0. Every fact is
persisted as block-pinned evidence (request/raw/interpretation hashes) and
classified into a deployment checklist; ``identities_verified`` is True only
when all canonical checks pass. The tool never signs, never broadcasts and
never authorizes execution (``execution_eligible`` is always False).

Usage:
    .venv/bin/python scripts/run_deploy_checklist.py \
        --network robinhood --block 56905986 \
        --identity-config configs/identity.robinhood.json \
        --output reports/deploy-check-2026-09-08
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.deploy.checklist import (
    EXPECTED_HOOK_PERMISSION_BITS,
    CheckStatus,
    DeployCheck,
    DeploymentChecklist,
)
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.strategy import OnchainAmountBandFeePolicy
from oasis_agent.models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key

PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
NATIVE_ADDRESS = "0x" + "00" * 20


@dataclass(frozen=True, slots=True)
class RpcEndpoint:
    name: str
    url: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.name):
            raise ValueError("RPC provider name must be a safe non-empty label")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("RPC provider endpoint must be an HTTPS URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("RPC provider endpoint must not contain credentials or a fragment")


@dataclass(frozen=True, slots=True)
class NetworkSpec:
    chain_id: int
    rpcs: tuple[RpcEndpoint, ...]
    pool_manager: str
    position_manager: str
    state_view: str


NETWORKS: dict[str, NetworkSpec] = {
    # Canonical cores pinned by the local fork preflight (run_pinned_fork.py)
    # and the 2026-09-07 evidence packages; hashes must still be pinned by the
    # operator in the identity config before any deployment.
    "robinhood": NetworkSpec(
        chain_id=4663,
        rpcs=(RpcEndpoint("robinhood-public", "https://rpc.mainnet.chain.robinhood.com"),),
        pool_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        position_manager="0x58daec3116aae6d93017baaea7749052e8a04fa7",
        state_view="0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
    ),
    "base-sepolia": NetworkSpec(
        chain_id=84532,
        rpcs=(RpcEndpoint("base-public", "https://sepolia.base.org"),),
        pool_manager="0x05e73354cfdd6745c338b50bcfdfa3aa6fa03408",
        position_manager="0x4b2c77d209d3405f41a037ec6c77f7f5b8e2ca80",
        state_view="0x571291b572ed32ce6751a2cb2486ebee8defb9b4",
    ),
    "robinhood-testnet": NetworkSpec(
        chain_id=46630,
        rpcs=(RpcEndpoint("robinhood-testnet-public", "https://rpc.testnet.chain.robinhood.com"),),
        pool_manager="",
        position_manager="",
        state_view="0x571291b572ed32ce6751a2cb2486ebee8defb9b4",
    ),
}


class PublicPoster:
    def __init__(self, url: str) -> None:
        self.url = url

    def post(self, payload: bytes) -> bytes:
        command = [
            "/usr/bin/curl",
            "--http1.1",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            self.url,
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
            # CalledProcessError includes the full command, which can contain a
            # provider credential in the endpoint path or query. Never let it
            # reach evidence, logs, or the terminal.
            code = getattr(exc, "returncode", None)
            suffix = "" if code is None else f" (exit {code})"
            raise OSError(f"RPC HTTPS request failed{suffix}") from None


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    hook: str = ""
    executor: str = ""
    token0: str = ""
    token1: str = ""
    initializer: str = ""
    governor: str = ""
    guardian: str = ""
    lp_owner: str = ""
    fee: int = DYNAMIC_FEE_FLAG
    tick_spacing: int = 60
    max_liquidity: int = 0
    executor_last_nonce: int = 0
    executor_bound_pool_id: str = "0x" + "00" * 32
    hook_fee: OnchainAmountBandFeePolicy | None = None
    expected_hashes: dict[str, str] = field(default_factory=dict)
    rpc_extra: tuple[RpcEndpoint, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "hook", normalize_address(self.hook) if self.hook else "")
        object.__setattr__(
            self, "executor", normalize_address(self.executor) if self.executor else ""
        )
        object.__setattr__(self, "token0", normalize_address(self.token0) if self.token0 else "")
        object.__setattr__(self, "token1", normalize_address(self.token1) if self.token1 else "")
        for name in ("initializer", "governor", "guardian", "lp_owner"):
            value = getattr(self, name)
            object.__setattr__(self, name, normalize_address(value) if value else "")
        for name in ("hook", "executor", "initializer", "governor", "guardian", "lp_owner"):
            value = getattr(self, name)
            if value and int(value, 16) == 0:
                raise ValueError(f"{name} must be nonzero when configured")
        configured_roles = tuple(
            value for value in (self.governor, self.guardian, self.lp_owner) if value
        )
        if len(configured_roles) != len(set(configured_roles)):
            raise ValueError("governor, guardian, and LP owner must be separate addresses")
        if type(self.fee) is not int or self.fee != DYNAMIC_FEE_FLAG:
            raise ValueError("Oasis hook deployment requires the dynamic fee flag")
        if type(self.tick_spacing) is not int:
            raise TypeError("tick_spacing must be an integer")
        if not 1 <= self.tick_spacing <= 32_767:
            raise ValueError("tick_spacing must be between 1 and 32767")
        if type(self.max_liquidity) is not int or self.max_liquidity < 0:
            raise ValueError("max_liquidity must be a nonnegative integer")
        if type(self.executor_last_nonce) is not int or not 0 <= self.executor_last_nonce < 1 << 64:
            raise ValueError("executor_last_nonce must be a uint64")
        object.__setattr__(
            self,
            "executor_bound_pool_id",
            normalize_hash32(self.executor_bound_pool_id, "executor bound pool id"),
        )
        if self.hook_fee is not None and not isinstance(self.hook_fee, OnchainAmountBandFeePolicy):
            raise TypeError("hook_fee must be an OnchainAmountBandFeePolicy")
        expected = self.expected_hashes
        if not isinstance(expected, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in expected.items()
        ):
            raise ValueError("expected_hashes must map role names to hex hashes")
        normalized_expected: dict[str, str] = {}
        for key, value in expected.items():
            role = key.lower()
            if role not in _EXPECTED_HASH_ROLES:
                raise ValueError(f"unknown expected hash role: {key}")
            normalized_expected[role] = normalize_hash32(value, f"{role} runtime hash")
        object.__setattr__(self, "expected_hashes", normalized_expected)
        if not all(isinstance(item, RpcEndpoint) for item in self.rpc_extra):
            raise TypeError("rpc_extra must contain RpcEndpoint values")


_IDENTITY_CONFIG_KEYS = frozenset(
    {
        "hook",
        "executor",
        "token0",
        "token1",
        "initializer",
        "governor",
        "guardian",
        "lp_owner",
        "fee",
        "tick_spacing",
        "max_liquidity",
        "executor_last_nonce",
        "executor_bound_pool_id",
        "hook_fee",
        "expected_hashes",
        "rpc_extra",
    }
)
_EXPECTED_HASH_ROLES = frozenset(
    {
        "pool_manager",
        "position_manager",
        "state_view",
        "permit2",
        "hook",
        "executor",
        "token0",
        "token1",
    }
)
_HOOK_FEE_KEYS = frozenset(
    {
        "base_fee",
        "medium_fee",
        "high_fee",
        "medium_amount0_threshold",
        "high_amount0_threshold",
        "medium_amount1_threshold",
        "high_amount1_threshold",
        "rule_version",
    }
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_identity_config(path: Path, environ: Mapping[str, str] | None = None) -> IdentityConfig:
    """Strict identity config loader: unknown keys and malformed values are rejected."""
    raw = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    if not isinstance(raw, Mapping):
        raise TypeError("identity config must be a JSON object")
    unknown = set(raw) - _IDENTITY_CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown identity config key(s): {sorted(unknown)}")
    expected = raw.get("expected_hashes", {})
    if not isinstance(expected, Mapping):
        raise TypeError("expected_hashes must be an object")
    unknown_hashes = set(expected) - _EXPECTED_HASH_ROLES
    if unknown_hashes:
        raise ValueError(f"unknown expected hash role(s): {sorted(unknown_hashes)}")
    extra = raw.get("rpc_extra", [])
    if not isinstance(extra, list):
        raise TypeError("rpc_extra must be a list of provider objects")
    resolved_environ = os.environ if environ is None else environ
    endpoints: list[RpcEndpoint] = []
    for index, item in enumerate(extra):
        if not isinstance(item, Mapping) or set(item) != {"name", "url_env"}:
            raise ValueError("each rpc_extra entry must contain exactly name and url_env")
        name = item["name"]
        url_env = item["url_env"]
        if not isinstance(name, str) or not isinstance(url_env, str):
            raise TypeError("RPC provider name and url_env must be strings")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", url_env):
            raise ValueError(f"rpc_extra[{index}].url_env is invalid")
        url = resolved_environ.get(url_env)
        if not url:
            raise ValueError(f"required RPC endpoint environment variable is missing: {url_env}")
        endpoints.append(RpcEndpoint(name, url))
    names = [item.name for item in endpoints]
    urls = [item.url for item in endpoints]
    if len(names) != len(set(names)) or len(urls) != len(set(urls)):
        raise ValueError("rpc_extra provider names and endpoints must be unique")
    fee = raw.get("fee", DYNAMIC_FEE_FLAG)
    tick_spacing = raw.get("tick_spacing", 60)
    if type(fee) is not int or type(tick_spacing) is not int:
        raise TypeError("fee and tick_spacing must be integers")
    hook_fee_raw = raw.get("hook_fee")
    if hook_fee_raw is not None and (
        not isinstance(hook_fee_raw, Mapping) or set(hook_fee_raw) != _HOOK_FEE_KEYS
    ):
        raise ValueError("hook_fee has invalid fields")
    hook_fee = (
        None if hook_fee_raw is None else OnchainAmountBandFeePolicy(**hook_fee_raw)  # type: ignore[arg-type]
    )
    return IdentityConfig(
        hook=str(raw["hook"]) if "hook" in raw else "",
        executor=str(raw["executor"]) if "executor" in raw else "",
        token0=str(raw["token0"]) if "token0" in raw else "",
        token1=str(raw["token1"]) if "token1" in raw else "",
        initializer=str(raw["initializer"]) if "initializer" in raw else "",
        governor=str(raw["governor"]) if "governor" in raw else "",
        guardian=str(raw["guardian"]) if "guardian" in raw else "",
        lp_owner=str(raw["lp_owner"]) if "lp_owner" in raw else "",
        fee=fee,
        tick_spacing=tick_spacing,
        max_liquidity=raw.get("max_liquidity", 0),  # type: ignore[arg-type]
        executor_last_nonce=raw.get("executor_last_nonce", 0),  # type: ignore[arg-type]
        executor_bound_pool_id=str(raw.get("executor_bound_pool_id", "0x" + "00" * 32)),
        hook_fee=hook_fee,
        expected_hashes={str(k): normalize_hash32(v) for k, v in expected.items()},
        rpc_extra=tuple(endpoints),
    )


def _selector(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()[:8]


_SELECTOR_SYMBOL = "0x95d89b41"
_SELECTOR_DECIMALS = "0x313ce567"
_SELECTOR_GET_SLOT0 = _selector("getSlot0(bytes32)")
_SELECTOR_GET_HOOK_PERMISSIONS = _selector("getHookPermissions()")
_SELECTOR_POOL_MANAGER = _selector("POOL_MANAGER()")
_SELECTOR_POSITION_MANAGER = _selector("POSITION_MANAGER()")
_SELECTOR_INITIALIZER = _selector("INITIALIZER()")
_SELECTOR_OWNER = _selector("owner()")
_SELECTOR_PENDING_OWNER = _selector("pendingOwner()")
_SELECTOR_GUARDIAN = _selector("guardian()")
_SELECTOR_RISK_PAUSED = _selector("riskPaused()")
_SELECTOR_BOUND_POOL_ID = _selector("boundPoolId()")
_SELECTOR_IS_WHITELISTED = _selector("isWhitelisted(address)")
_SELECTOR_MAX_LIQUIDITY = _selector("MAX_LIQUIDITY()")
_SELECTOR_PAUSED = _selector("paused()")
_SELECTOR_LAST_NONCE = _selector("lastNonce()")
_SELECTOR_FEE_CONFIG = _selector("feeConfig((address,address,uint24,int24,address))")


def _decode_uint8(value: object, name: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"{name} must be hex ABI data")
    raw = bytes.fromhex(value[2:])
    if len(raw) != 32:
        raise ValueError(f"{name} must be one ABI word")
    decoded = int.from_bytes(raw)
    if decoded > 255:
        raise ValueError(f"{name} must fit uint8")
    return decoded


def _decode_word(value: object, name: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"{name} must be hex ABI data")
    raw = bytes.fromhex(value[2:])
    if len(raw) != 32:
        raise ValueError(f"{name} must be one ABI word")
    return raw


def _decode_address(value: object, name: str) -> str:
    raw = _decode_word(value, name)
    if any(raw[:12]):
        raise ValueError(f"{name} address padding is nonzero")
    return normalize_address("0x" + raw[12:].hex())


def _decode_bool(value: object, name: str) -> bool:
    raw = _decode_word(value, name)
    number = int.from_bytes(raw)
    if number not in (0, 1):
        raise ValueError(f"{name} is not a canonical ABI bool")
    return bool(number)


def _decode_uint(value: object, name: str) -> int:
    return int.from_bytes(_decode_word(value, name))


def _decode_hash(value: object, name: str) -> str:
    return normalize_hash32("0x" + _decode_word(value, name).hex(), name)


def _decode_hook_fee(value: object) -> dict[str, int]:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("feeConfig result must be hex ABI data")
    raw = bytes.fromhex(value[2:])
    if len(raw) != 9 * 32:
        raise ValueError("feeConfig result has invalid ABI length")
    values = decode(
        [
            "uint24",
            "uint24",
            "uint24",
            "uint128",
            "uint128",
            "uint128",
            "uint128",
            "uint64",
            "bool",
        ],
        raw,
    )
    if values[-1] is not True:
        raise ValueError("hook pool fee config is not configured")
    names = (
        "base_fee",
        "medium_fee",
        "high_fee",
        "medium_amount0_threshold",
        "high_amount0_threshold",
        "medium_amount1_threshold",
        "high_amount1_threshold",
        "rule_version",
    )
    return {name: int(value) for name, value in zip(names, values[:-1], strict=True)}


def _decode_symbol(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("symbol result must be hex data")
    raw = bytes.fromhex(value[2:])
    if len(raw) < 64:
        raise ValueError("symbol result too short")
    return raw[64:].rstrip(b"\x00").decode("utf-8", "replace")


def _decode_hook_permission_bits(value: object) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("hook permissions result must be hex data")
    raw = bytes.fromhex(value[2:])
    if len(raw) != 14 * 32:
        raise ValueError("hook permissions result has invalid ABI length")
    permissions = decode(["bool"] * 14, raw)
    return sum((1 << (13 - index)) for index, enabled in enumerate(permissions) if enabled)


def _decode_slot0(value: object) -> dict[str, int]:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("slot0 result must be hex ABI data")
    raw = bytes.fromhex(value[2:])
    if len(raw) != 4 * 32:
        raise ValueError("slot0 result has invalid ABI length")
    sqrt_price_x96, tick, protocol_fee, lp_fee = decode(
        ["uint160", "int24", "uint24", "uint24"], raw
    )
    return {
        "sqrt_price_x96": int(sqrt_price_x96),
        "tick": int(tick),
        "protocol_fee": int(protocol_fee),
        "lp_fee": int(lp_fee),
    }


def _all_provider_values_agree(values: Mapping[str, object], provider_count: int) -> bool:
    """Return true only for complete, multi-provider canonical consensus."""
    return (
        len(values) == provider_count
        and len(values) >= 2
        and len({canonical_json(value) for value in values.values()}) == 1
    )


def _write_manifest(directory: Path) -> None:
    lines = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}")
    (directory / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=sorted(NETWORKS), required=True)
    parser.add_argument("--block", type=int, required=True, help="explicit block pin")
    parser.add_argument("--identity-config", type=Path, help="JSON identity config")
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    args = parser.parse_args()
    if args.block <= 0 or args.output.exists():
        parser.error("positive explicit block and new output directory required")
    args.output.mkdir(parents=True)

    spec = NETWORKS[args.network]
    identity = (
        load_identity_config(args.identity_config)
        if args.identity_config is not None
        else IdentityConfig()
    )
    pool_manager = spec.pool_manager
    position_manager = spec.position_manager
    state_view = spec.state_view
    if not pool_manager or not position_manager or not state_view:
        parser.error("core deployment addresses are not known for this network")
    endpoints = spec.rpcs + identity.rpc_extra
    if not endpoints:
        parser.error("at least one RPC provider is required")
    provider_names = [item.name for item in endpoints]
    provider_urls = [item.url for item in endpoints]
    if len(provider_names) != len(set(provider_names)):
        parser.error("RPC provider names must be unique")
    if len(provider_urls) != len(set(provider_urls)):
        parser.error("RPC provider endpoints must be unique")

    store = EvidenceStore(args.output / "evidence.db")
    probe_start = datetime.now(UTC)
    retrieved_at = probe_start.isoformat()
    try:
        # Each provider gets its own recorder so evidence provenance stays
        # source-attributable; the pinned header must agree across providers.
        providers: list[tuple[str, RpcClient, BoundedExchangeRecorder]] = []
        headers: dict[str, tuple[int, str, int]] = {}
        chain_exchanges: dict[str, RpcExchange] = {}
        for endpoint in endpoints:
            recorder = BoundedExchangeRecorder()
            rpc = RpcClient(
                JsonRpcTransport(
                    endpoint.url,
                    http=PublicPoster(endpoint.url),
                    recorder=recorder,
                    retries=1,
                ),
                expected_chain_id=spec.chain_id,
            )
            chain_exchanges[endpoint.name] = recorder.exchanges[-1]
            snapshot = rpc.get_block_snapshot(args.block)
            if snapshot.ref.timestamp is None:
                raise ValueError("pinned block timestamp is unavailable")
            headers[endpoint.name] = (
                snapshot.ref.number,
                snapshot.ref.block_hash,
                snapshot.ref.timestamp,
            )
            providers.append((endpoint.name, rpc, recorder))
        head = next(iter(headers.values()))
        if any(value != head for value in headers.values()):
            raise ValueError("RPC providers disagree on the pinned block header")
        block_number, block_hash, block_timestamp = head
        block = BlockRef(spec.chain_id, block_number, block_hash, block_timestamp)

        def persist(
            kind: str,
            fact: object,
            source: str,
            recorder: BoundedExchangeRecorder,
            request: object | None,
            raw_response: object,
            interpretation: object,
        ) -> EvidenceRef:
            return store.put(
                kind,
                canonical_json(fact),
                block,
                source,
                request=request,
                raw_response=raw_response,
                interpretation=interpretation,
                retrieved_at=retrieved_at,
            )

        # --- chain id ----------------------------------------------------
        chain_refs: list[EvidenceRef] = []
        for source, rpc, recorder in providers:
            exchange = chain_exchanges[source]
            chain_refs.append(
                persist(
                    "deployment_identity",
                    {"chain_id": spec.chain_id},
                    source,
                    recorder,
                    request={"method": exchange.method, "payload": exchange.request_payload.hex()},
                    raw_response={"payload": exchange.response_payload.hex()},
                    interpretation={"chain_id": spec.chain_id, "block": block_hash},
                )
            )

        # --- contract runtime identities --------------------------------
        contract_checks: list[DeployCheck] = []

        def probe_code(role: str, address: str) -> None:
            expected = identity.expected_hashes.get(role)
            evidence: list[EvidenceRef] = []
            retrieved: dict[str, str] = {}
            for source, rpc, recorder in providers:
                try:
                    before = len(recorder.exchanges)
                    code = rpc.get_code(address, block_number)
                    exchange = recorder.exchanges[before]
                    digest = "0x" + keccak(bytes.fromhex(code[2:])).hex()
                    retrieved[source] = digest
                    evidence.append(
                        persist(
                            "deployment_identity",
                            {"role": role, "address": address, "runtime_keccak": digest},
                            source,
                            recorder,
                            request={
                                "method": exchange.method,
                                "payload": exchange.request_payload.hex(),
                            },
                            raw_response={"payload": exchange.response_payload.hex()},
                            interpretation={
                                "role": role,
                                "runtime_keccak": digest,
                                "block": block_hash,
                            },
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - RPC failure is UNKNOWN, never crash
                    evidence.append(
                        persist(
                            "deployment_identity",
                            {"role": role, "address": address, "error": str(exc)},
                            source,
                            recorder,
                            request=None,
                            raw_response={"error": str(exc)},
                            interpretation={"role": role, "unavailable": True},
                        )
                    )
            observed_hashes = set(retrieved.values())
            if (
                expected
                and len(retrieved) == len(providers)
                and len(retrieved) >= 2
                and observed_hashes == {expected}
            ):
                status = CheckStatus.PASS
                detail = f"runtime keccak matches pinned {expected}"
            elif expected and retrieved:
                status = CheckStatus.UNKNOWN
                detail = (
                    f"{len(retrieved)} provider(s) returned runtime keccak "
                    f"{sorted(observed_hashes)}; need >=2 all matching pinned {expected}"
                )
            elif retrieved:
                status = CheckStatus.UNKNOWN
                detail = f"no pinned hash provided; retrieved {sorted(observed_hashes)}"
            else:
                status = CheckStatus.UNKNOWN
                detail = "runtime bytecode unavailable at pinned block"
            contract_checks.append(
                DeployCheck(
                    name=role + "_identity", status=status, evidence=tuple(evidence), detail=detail
                )
            )

        for role, address in (
            ("pool_manager", pool_manager),
            ("position_manager", position_manager),
            ("state_view", state_view),
            ("permit2", PERMIT2),
        ):
            probe_code(role, address)
        if identity.hook:
            probe_code("hook", identity.hook)
        else:
            contract_checks.append(
                DeployCheck(
                    name="hook_identity",
                    status=CheckStatus.UNKNOWN,
                    evidence=(),
                    detail="hook address not configured",
                )
            )
        if identity.executor:
            probe_code("executor", identity.executor)
        else:
            contract_checks.append(
                DeployCheck(
                    name="executor_identity",
                    status=CheckStatus.UNKNOWN,
                    evidence=(),
                    detail="executor address not configured",
                )
            )

        # --- hook permission bits ----------------------------------------
        hook_evidence: list[EvidenceRef] = []
        hook_status = CheckStatus.UNKNOWN
        permission_detail = "hook address not configured"
        if identity.hook:
            observed: dict[str, int] = {}
            for source, rpc, recorder in providers:
                try:
                    before = len(recorder.exchanges)
                    result = rpc.call(identity.hook, _SELECTOR_GET_HOOK_PERMISSIONS, block_number)
                    exchange = recorder.exchanges[before]
                    bits = _decode_hook_permission_bits(result)
                    observed[source] = bits
                    hook_evidence.append(
                        persist(
                            "deployment_identity",
                            {"hook": identity.hook, "permission_bits": bits},
                            source,
                            recorder,
                            request={
                                "method": exchange.method,
                                "payload": exchange.request_payload.hex(),
                            },
                            raw_response={"payload": exchange.response_payload.hex()},
                            interpretation={
                                "hook": identity.hook,
                                "permission_bits": bits,
                                "block": block_hash,
                            },
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - see probe_code
                    hook_evidence.append(
                        persist(
                            "deployment_identity",
                            {"hook": identity.hook, "error": str(exc)},
                            source,
                            recorder,
                            request=None,
                            raw_response={"error": str(exc)},
                            interpretation={"hook": identity.hook, "unavailable": True},
                        )
                    )
            observed_bits = set(observed.values())
            if (
                len(observed) == len(providers)
                and len(observed) >= 2
                and observed_bits == {EXPECTED_HOOK_PERMISSION_BITS}
            ):
                hook_status = CheckStatus.PASS
                permission_detail = f"permission bits {hex(EXPECTED_HOOK_PERMISSION_BITS)} match REQUIRED_HOOK_FLAGS"
            elif observed:
                hook_status = CheckStatus.UNKNOWN
                permission_detail = (
                    f"{len(observed)} provider(s) returned permission bits "
                    f"{sorted(hex(v) for v in observed_bits)}; need >=2 all matching "
                    f"{hex(EXPECTED_HOOK_PERMISSION_BITS)}"
                )
            else:
                permission_detail = "hook permission bits unavailable"

        # --- immutable bindings and live control state ------------------
        def probe_bound_state(
            *,
            name: str,
            address: str,
            calls: tuple[tuple[str, str, Callable[[object], object]], ...],
            expected: dict[str, object],
            unavailable_detail: str,
        ) -> DeployCheck:
            if not address or not calls:
                return DeployCheck(name, CheckStatus.UNKNOWN, (), unavailable_detail)
            evidence: list[EvidenceRef] = []
            observed: dict[str, dict[str, object]] = {}
            for source, rpc, recorder in providers:
                try:
                    before = len(recorder.exchanges)
                    facts = {
                        field_name: decoder(rpc.call(address, data, block_number))
                        for field_name, data, decoder in calls
                    }
                    exchanges = recorder.exchanges[before:]
                    observed[source] = facts
                    evidence.append(
                        persist(
                            "deployment_identity",
                            {"contract": address, "state": facts},
                            source,
                            recorder,
                            request={"exchanges": [e.request_payload.hex() for e in exchanges]},
                            raw_response={
                                "exchanges": [e.response_payload.hex() for e in exchanges]
                            },
                            interpretation={
                                "contract": address,
                                "state": facts,
                                "block": block_hash,
                            },
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - unavailable is fail-closed
                    evidence.append(
                        persist(
                            "deployment_identity",
                            {"contract": address, "error": str(exc)},
                            source,
                            recorder,
                            request=None,
                            raw_response={"error": str(exc)},
                            interpretation={"contract": address, "unavailable": True},
                        )
                    )
            all_agree = (
                len(observed) == len(providers)
                and len(observed) >= 2
                and len({canonical_json(item) for item in observed.values()}) == 1
            )
            if all_agree and next(iter(observed.values())) == expected:
                status = CheckStatus.PASS
                detail = f"{name} matches all configured bindings and safe state"
            elif all_agree:
                status = CheckStatus.FAIL
                detail = f"{name} providers agree on state that differs from configuration"
            else:
                status = CheckStatus.UNKNOWN
                detail = (
                    f"{len(observed)} provider(s) returned {name}; need every configured "
                    "provider and at least two agreeing"
                )
            return DeployCheck(name, status, tuple(evidence), detail)

        binding_inputs = all(
            (
                identity.hook,
                identity.token0,
                identity.token1,
                identity.initializer,
                identity.governor,
                identity.guardian,
                identity.lp_owner,
                identity.hook_fee,
            )
        )
        hook_binding_check = DeployCheck(
            "hook_bindings_and_state",
            CheckStatus.UNKNOWN,
            (),
            "hook roles, pool, LP owner, or fee config not fully configured",
        )
        configured_pool_id = ""
        configured_key_values: tuple[str, str, int, int, str] | None = None
        if binding_inputs:
            configured_key = PoolKey(
                identity.token0,
                identity.token1,
                identity.fee,
                identity.tick_spacing,
                identity.hook,
            )
            configured_pool_id = pool_id_from_key(configured_key)
            configured_key_values = (
                configured_key.currency0,
                configured_key.currency1,
                configured_key.fee,
                configured_key.tick_spacing,
                configured_key.hooks,
            )
            assert identity.hook_fee is not None
            hook_calls: tuple[tuple[str, str, Callable[[object], object]], ...] = (
                (
                    "pool_manager",
                    _SELECTOR_POOL_MANAGER,
                    lambda v: _decode_address(v, "pool manager"),
                ),
                (
                    "position_manager",
                    _SELECTOR_POSITION_MANAGER,
                    lambda v: _decode_address(v, "position manager"),
                ),
                ("initializer", _SELECTOR_INITIALIZER, lambda v: _decode_address(v, "initializer")),
                ("owner", _SELECTOR_OWNER, lambda v: _decode_address(v, "owner")),
                (
                    "pending_owner",
                    _SELECTOR_PENDING_OWNER,
                    lambda v: _decode_address(v, "pending owner"),
                ),
                ("guardian", _SELECTOR_GUARDIAN, lambda v: _decode_address(v, "guardian")),
                ("risk_paused", _SELECTOR_RISK_PAUSED, lambda v: _decode_bool(v, "risk paused")),
                (
                    "bound_pool_id",
                    _SELECTOR_BOUND_POOL_ID,
                    lambda v: _decode_hash(v, "bound pool id"),
                ),
                (
                    "lp_owner_whitelisted",
                    _SELECTOR_IS_WHITELISTED + encode(["address"], [identity.lp_owner]).hex(),
                    lambda v: _decode_bool(v, "LP owner whitelist"),
                ),
                (
                    "fee_config",
                    _SELECTOR_FEE_CONFIG
                    + encode(
                        ["(address,address,uint24,int24,address)"],
                        [configured_key_values],
                    ).hex(),
                    _decode_hook_fee,
                ),
            )
            hook_binding_check = probe_bound_state(
                name="hook_bindings_and_state",
                address=identity.hook,
                calls=hook_calls,
                expected={
                    "pool_manager": pool_manager,
                    "position_manager": position_manager,
                    "initializer": identity.initializer,
                    "owner": identity.governor,
                    "pending_owner": NATIVE_ADDRESS,
                    "guardian": identity.guardian,
                    "risk_paused": False,
                    "bound_pool_id": configured_pool_id,
                    "lp_owner_whitelisted": True,
                    "fee_config": asdict(identity.hook_fee),
                },
                unavailable_detail="hook state is not fully configured",
            )

        executor_binding_check = DeployCheck(
            "executor_bindings_and_state",
            CheckStatus.UNKNOWN,
            (),
            "executor roles, pool, or liquidity cap not fully configured",
        )
        if (
            identity.executor
            and identity.lp_owner
            and identity.guardian
            and identity.max_liquidity > 0
        ):
            executor_binding_check = probe_bound_state(
                name="executor_bindings_and_state",
                address=identity.executor,
                calls=(
                    (
                        "pool_manager",
                        _SELECTOR_POOL_MANAGER,
                        lambda v: _decode_address(v, "pool manager"),
                    ),
                    (
                        "position_manager",
                        _SELECTOR_POSITION_MANAGER,
                        lambda v: _decode_address(v, "position manager"),
                    ),
                    ("owner", _SELECTOR_OWNER, lambda v: _decode_address(v, "owner")),
                    (
                        "pending_owner",
                        _SELECTOR_PENDING_OWNER,
                        lambda v: _decode_address(v, "pending owner"),
                    ),
                    ("guardian", _SELECTOR_GUARDIAN, lambda v: _decode_address(v, "guardian")),
                    ("paused", _SELECTOR_PAUSED, lambda v: _decode_bool(v, "paused")),
                    (
                        "bound_pool_id",
                        _SELECTOR_BOUND_POOL_ID,
                        lambda v: _decode_hash(v, "bound pool id"),
                    ),
                    ("last_nonce", _SELECTOR_LAST_NONCE, lambda v: _decode_uint(v, "last nonce")),
                    (
                        "max_liquidity",
                        _SELECTOR_MAX_LIQUIDITY,
                        lambda v: _decode_uint(v, "max liquidity"),
                    ),
                ),
                expected={
                    "pool_manager": pool_manager,
                    "position_manager": position_manager,
                    "owner": identity.lp_owner,
                    "pending_owner": NATIVE_ADDRESS,
                    "guardian": identity.guardian,
                    "paused": False,
                    "bound_pool_id": identity.executor_bound_pool_id,
                    "last_nonce": identity.executor_last_nonce,
                    "max_liquidity": identity.max_liquidity,
                },
                unavailable_detail="executor state is not fully configured",
            )

        # --- tokens -------------------------------------------------------
        def probe_token(role: str, address: str) -> DeployCheck:
            if not address:
                return DeployCheck(
                    name=role + "_identity",
                    status=CheckStatus.UNKNOWN,
                    evidence=(),
                    detail=f"{role} address not configured",
                )
            if address == NATIVE_ADDRESS:
                return DeployCheck(
                    name=role + "_identity",
                    status=CheckStatus.PASS,
                    evidence=tuple(chain_refs),
                    detail="native currency; no ERC-20 metadata to verify",
                )
            expected_hash = identity.expected_hashes.get(role)
            token_evidence: list[EvidenceRef] = []
            metadata: dict[str, dict[str, object]] = {}
            for source, rpc, recorder in providers:
                try:
                    before = len(recorder.exchanges)
                    code = rpc.get_code(address, block_number)
                    if code == "0x":
                        raise ValueError("token has no runtime bytecode")
                    symbol = rpc.call(address, _SELECTOR_SYMBOL, block_number)
                    decimals = rpc.call(address, _SELECTOR_DECIMALS, block_number)
                    exchanges = recorder.exchanges[before:]
                    decoded_symbol = _decode_symbol(symbol)
                    decimals_value = _decode_uint8(decimals, "decimals result")
                    runtime_keccak = "0x" + keccak(bytes.fromhex(code[2:])).hex()
                    facts: dict[str, object] = {
                        "symbol": decoded_symbol,
                        "decimals": decimals_value,
                        "runtime_keccak": runtime_keccak,
                    }
                    metadata[source] = facts
                    token_evidence.append(
                        persist(
                            "deployment_identity",
                            {
                                "role": role,
                                "address": address,
                                **facts,
                            },
                            source,
                            recorder,
                            request={"exchanges": [e.request_payload.hex() for e in exchanges]},
                            raw_response={
                                "exchanges": [e.response_payload.hex() for e in exchanges]
                            },
                            interpretation={
                                "role": role,
                                **facts,
                                "block": block_hash,
                            },
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - see probe_code
                    token_evidence.append(
                        persist(
                            "deployment_identity",
                            {"role": role, "address": address, "error": str(exc)},
                            source,
                            recorder,
                            request=None,
                            raw_response={"error": str(exc)},
                            interpretation={"role": role, "unavailable": True},
                        )
                    )
            status = (
                CheckStatus.PASS
                if expected_hash
                and len(metadata) == len(providers)
                and len(metadata) >= 2
                and len({canonical_json(value) for value in metadata.values()}) == 1
                and {str(value["runtime_keccak"]) for value in metadata.values()} == {expected_hash}
                else CheckStatus.UNKNOWN
            )
            detail = (
                "; ".join(
                    f"{source}: {value['symbol']}/{value['decimals']} "
                    f"runtime_keccak={value['runtime_keccak']}"
                    for source, value in metadata.items()
                )
                or "metadata unavailable"
            )
            if metadata and not expected_hash:
                detail += "; no pinned token runtime hash configured"
            return DeployCheck(
                name=role + "_identity",
                status=status,
                evidence=tuple(token_evidence),
                detail=detail,
            )

        token0_check = probe_token("token0", identity.token0)
        token1_check = probe_token("token1", identity.token1)

        # --- pool ---------------------------------------------------------
        pool_check = DeployCheck(
            name="pool_id_and_initialization",
            status=CheckStatus.UNKNOWN,
            evidence=(),
            detail="token pair not configured",
        )
        if identity.token0 and identity.token1 and identity.hook:
            try:
                key = PoolKey(
                    currency0=identity.token0,
                    currency1=identity.token1,
                    fee=identity.fee,
                    tick_spacing=identity.tick_spacing,
                    hooks=identity.hook,
                )
                pool_id = pool_id_from_key(key)
                pool_evidence: list[EvidenceRef] = []
                initialized: dict[str, dict[str, int]] = {}
                for source, rpc, recorder in providers:
                    try:
                        before = len(recorder.exchanges)
                        slot0 = rpc.call(
                            state_view,
                            _SELECTOR_GET_SLOT0 + pool_id[2:].rjust(64, "0"),
                            block_number,
                        )
                        exchange = recorder.exchanges[before]
                        slot0_state = _decode_slot0(slot0)
                        if slot0_state["sqrt_price_x96"] == 0:
                            raise ValueError("pool is not initialized")
                        initialized[source] = slot0_state
                        pool_evidence.append(
                            persist(
                                "deployment_identity",
                                {"pool_id": pool_id, "slot0": slot0_state},
                                source,
                                recorder,
                                request={
                                    "method": exchange.method,
                                    "payload": exchange.request_payload.hex(),
                                },
                                raw_response={"payload": exchange.response_payload.hex()},
                                interpretation={
                                    "pool_id": pool_id,
                                    "slot0": slot0_state,
                                    "block": block_hash,
                                },
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - see probe_code
                        pool_evidence.append(
                            persist(
                                "deployment_identity",
                                {"pool_id": pool_id, "error": str(exc)},
                                source,
                                recorder,
                                request=None,
                                raw_response={"error": str(exc)},
                                interpretation={"pool_id": pool_id, "unavailable": True},
                            )
                        )
                pool_check = DeployCheck(
                    name="pool_id_and_initialization",
                    status=(
                        CheckStatus.PASS
                        if _all_provider_values_agree(initialized, len(providers))
                        else CheckStatus.UNKNOWN
                    ),
                    evidence=tuple(pool_evidence),
                    detail=(
                        f"PoolId {pool_id} initialized consistently at pinned block by "
                        f"{len(initialized)} provider(s)"
                        if len(initialized) == len(providers) and len(initialized) >= 2
                        else (
                            f"PoolId {pool_id} returned by {len(initialized)} provider(s); "
                            "need every configured provider and at least two"
                        )
                    ),
                )
            except (ValueError, TypeError) as exc:
                pool_check = DeployCheck(
                    name="pool_id_and_initialization",
                    status=CheckStatus.UNKNOWN,
                    evidence=(),
                    detail=f"pool key invalid: {exc}",
                )

        # --- provider diversity ------------------------------------------
        sources: set[str] = set()
        for check in contract_checks + [token0_check, token1_check, pool_check]:
            sources.update(reference.source for reference in check.evidence)
        diversity_check = DeployCheck(
            name="provider_diversity",
            status=CheckStatus.PASS if len(sources) >= 2 else CheckStatus.UNKNOWN,
            evidence=tuple(chain_refs) if len(sources) >= 2 else (),
            detail=f"{len(sources)} distinct provider(s) contributed evidence (need >= 2)",
        )

        all_checks = [
            DeployCheck(
                name="chain_id",
                status=CheckStatus.PASS if len(chain_refs) >= 2 else CheckStatus.UNKNOWN,
                evidence=tuple(chain_refs),
                detail=f"chain id {spec.chain_id} confirmed by {len(chain_refs)} provider(s)",
            ),
            *contract_checks,
            DeployCheck(
                name="hook_permission_bits",
                status=hook_status,
                evidence=tuple(hook_evidence),
                detail=permission_detail,
            ),
            hook_binding_check,
            executor_binding_check,
            token0_check,
            token1_check,
            pool_check,
            diversity_check,
        ]
        # Some public RPCs on the target chain reject EIP-1898 block-hash
        # selectors. Calls therefore use the explicit block number, bracketed
        # by matching header reads. Any reorg during collection invalidates the
        # entire evidence package instead of silently mixing states.
        for source, rpc, _recorder in providers:
            snapshot = rpc.get_block_snapshot(block_number)
            final_header = (
                snapshot.ref.number,
                snapshot.ref.block_hash,
                snapshot.ref.timestamp,
            )
            if final_header != headers[source]:
                raise ValueError(f"RPC provider {source} changed the pinned block header")
        checklist = DeploymentChecklist(checks=tuple(all_checks))
    finally:
        store.close()

    output: dict[str, object] = {
        "schema": "oasis.deployment-checklist.v2",
        "network": args.network,
        "chain_id": spec.chain_id,
        "block_number": block_number,
        "block_hash": block_hash,
        "retrieved_at": retrieved_at,
        "elapsed_seconds": round((datetime.now(UTC) - probe_start).total_seconds(), 3),
        "checks": [
            {
                "name": check.name,
                "status": check.status.value,
                "detail": check.detail,
                "evidence": [asdict(reference) for reference in check.evidence],
            }
            for check in checklist.checks
        ],
        "identities_verified": checklist.identities_verified,
        "execution_eligible": checklist.execution_eligible,
        "reasons": list(checklist.reasons),
    }
    (args.output / "checklist.json").write_bytes(canonical_json(output))
    (args.output / "README.md").write_text(
        "Oasis deployment identity check.\n"
        f"network: {args.network}; chain-id: {spec.chain_id}; block: {block_number}; "
        f"block-hash: {block_hash}\n"
        f"providers: {', '.join(provider_names)}\n"
        f"identities_verified: {str(checklist.identities_verified).lower()}; "
        "execution_eligible: false\n"
        "No RPC endpoint, credential, private key, signature, or raw transaction is retained.\n",
        encoding="utf-8",
    )
    _write_manifest(args.output)
    print(f"Identities verified: {checklist.identities_verified}")
    for check in checklist.checks:
        print(f"  [{check.status.value:9}] {check.name}: {check.detail}")
    print(f"Evidence: {args.output}")
    raise SystemExit(0 if checklist.identities_verified else 1)


if __name__ == "__main__":
    main()
