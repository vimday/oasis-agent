"""Plan, or explicitly capture, one stopped two-service recovery evidence window.

Default mode is offline and does not read RPC environment variables. --capture
requires an exact approved plan hash and a retained zero-cost or paid scope declaration.
Those declarations record external review; this tool cannot verify provider billing
or create authorization. It never creates budgets, changes state/STOP, or signs.
--discover-gas-oracle selects a separate, at-most-209-read scope that proposes
three observed gas fingerprints and captures both services at that same head.
"""

import argparse
import json
import math
import os
import signal
import stat
import threading
import time
from collections import Counter
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlsplit

from oasis_agent.evidence.exact_capture import decode_header
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.rpc_validation import (
    decode_exchange,
    rpc_quantity,
    strict_digest,
    strict_json_object,
)
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.live.acquisition import acquire_pair
from oasis_agent.live.config import LiveConfig, PublicUsdConfig
from oasis_agent.live.feed_transition import _service_lock
from oasis_agent.live.gas_oracle import (
    LEGACY_PIN_NAMES,
    ORACLE,
    implementation_address,
    runtime_hash,
)
from oasis_agent.live.gas_oracle_evidence import (
    candidate_configuration,
    discovery_requests,
    retain_discovery_quorum,
)
from oasis_agent.live.positions import PositionOutcomeScope, outcome_requests
from oasis_agent.live.public_usd_prices import fetch_public_prices
from oasis_agent.live.rearm_settlements import (
    SettlementHeadCapture,
    persist_settlement_head,
    require_settlement_opening,
    settlement_requests,
)
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.startup_state import SOURCES, _position_identity, paths, require_file
from oasis_agent.live.state import StateStore
from oasis_agent.live.valuation import (
    NavRefusal,
    capture_nav,
    nav_fresh_until,
    replay_nav_quorum,
)
from oasis_agent.live.valuation import requests as nav_requests
from oasis_agent.live.wallet import WalletScope, wallet_requests
from oasis_agent.models.common import BlockRef, canonical_json, evidence_ref_from_json, sha256_hex
from oasis_agent.models.v4 import PoolKey
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import MAX_RESPONSE_BYTES, JsonRpcTransport, RpcExchange
from oasis_agent.rpc.transport import _UrlLibPoster as BoundedPoster
from scripts.live_recovery_checkpoint import review_checkpoint
from scripts.review_live_runtime_transition import review as review_runtime_transition

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("strategy", "authority")
RPC_ENV = ("BASE_RPC_PRIMARY_ALT", "BASE_RPC_PRIMARY")
MAX_SECONDS = 120
MAX_REQUESTS = 197
RUNTIME_MAX_REQUESTS = MAX_REQUESTS + 12
RUNTIME_PUBLICATIONS = (
    "fresh-nav.json",
    "references.json",
    "runtime-target.json",
    "runtime-references.json",
    "runtime-review.json",
)
MAX_RESPONSE = MAX_RESPONSE_BYTES
TRANSPORT_PROFILE = "shared_https_no_redirect_single_attempt.v1"
PAID_CEILING_MICROS = 100_000
# Conservative standard-read bound at 40 CU * $0.525/M for Alchemy
# and 20 CU * $0.30/M for dRPC. This does not fund account activation.
PAID_MICROS_PER_READ = dict(zip(SOURCES, (21, 6), strict=True))
PAID_DOMAINS = dict(zip(SOURCES, ("alchemy.com", "drpc.org"), strict=True))
PAID_READ_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getCode",
        "eth_getStorageAt",
        "eth_call",
        "eth_getBalance",
        "eth_getTransactionCount",
        "eth_getTransactionReceipt",
        "eth_getTransactionByHash",
    }
)
NAV_REFUSAL_CODES = {
    "NAV ETH / USD feed incomplete, stale or future": "nav_eth_usd_feed_incomplete_stale_or_future",
    "NAV USDC / USD feed incomplete, stale or future": (
        "nav_usdc_usd_feed_incomplete_stale_or_future"
    ),
}


def _identity(path):
    info = require_file(path)
    return info.st_dev, info.st_ino


def _unchanged(path):
    info = require_file(path)
    return tuple(
        getattr(info, field)
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )


@contextmanager
def prepared(strategy, authority, config, profile, *, discover_gas_oracle=False):
    """Hold original EX locks/STOP, and inspect originals without initializing any file."""
    config.validate()
    if discover_gas_oracle and set(config.expected_pins) != LEGACY_PIN_NAMES:
        raise ValueError("joint discovery requires the original five-pin configuration")
    if (
        config.oracle_max_age_seconds,
        config.usdc_oracle_max_age_seconds,
        config.max_capital_usd,
        config.max_daily_loss_usd,
        config.max_cumulative_loss_usd,
        config.secondary_rpc_env,
    ) != (1800, 1800, 500, 200, 500, RPC_ENV[1]):
        raise ValueError("strict owner limits and original RPC pair required")
    states, stores, evidence, budgets, checks, identities, immutable = {}, {}, {}, {}, [], {}, {}
    services = {}
    with ExitStack() as stack:
        for role, root in zip(ROLES, (strategy, authority), strict=True):
            directory = root.absolute()
            info = directory.lstat()
            if (
                directory.resolve() != directory
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("original private service directory required")
            state_path, evidence_path, lock = paths(directory, role)
            store = StateStore(state_path, require_existing=True)
            budget = directory / "live.receipt-budget.db"
            for path in (
                state_path,
                evidence_path,
                lock,
                store.journal_path,
                budget,
                directory / "STOP",
            ):
                identity = _identity(path)
                if identity in identities.values():
                    raise ValueError("distinct original service files required")
                identities[path] = identity
            for path in (evidence_path, store.journal_path, budget):
                if any(
                    os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")
                ):
                    raise ValueError("SQLite recovery files require separate inspection")
            checks.append(stack.enter_context(_service_lock(store, directory / "STOP", role)))
            state = store.load("live", read_only=True)
            if discover_gas_oracle and not state.halted:
                raise ValueError("joint discovery requires both original services halted")
            binding = store.journal.config_hash
            # Release SQLite's read transaction before any original evidence append.
            with closing(ReadOnlyEvidenceStore(evidence_path)) as retained:
                retained.validate_existing_schema()
                review_checkpoint(config, state, binding, retained, now=int(time.time()))
            if state.position is None or not 1 <= len(state.settled_blocks) <= 5:
                raise ValueError("bounded retained position and settlement inventory required")
            with RpcBudget(budget, profile.policies(SOURCES), read_only=True) as quota:
                counts = quota.counts()
                tip = quota.db.execute(
                    "SELECT digest FROM rpc_usage ORDER BY seq DESC LIMIT 1"
                ).fetchone()
            if any(not 0 < counts[source] < profile.max_requests for source in SOURCES) or not tip:
                raise ValueError("original nonempty RPC history required")
            for path in (state_path, store.journal_path):
                immutable[path] = _unchanged(path)
            states[role], stores[role], evidence[role], budgets[role] = (
                state,
                store,
                evidence_path,
                budget,
            )
            services[role] = {
                "state_hash": sha256_hex(canonical_json(asdict(state))),
                "journal_tip": store.journal.revision,
                "config_hash": binding,
                "budget_counts": counts,
                "budget_history_tip": tip[0],
                "budget_identity": identities[budget],
                "state_identity": identities[state_path],
                "journal_identity": identities[store.journal_path],
                "evidence_identity": identities[evidence_path],
                "directory": str(directory),
            }
        if (
            _position_identity(states["strategy"].position)
            != _position_identity(states["authority"].position)
            or states["strategy"].settled_blocks != states["authority"].settled_blocks
        ):
            raise ValueError("original service holdings or anchors disagree")
        position = states["strategy"].position
        # This dummy coordinate constructs request shapes only; never stored as evidence.
        block = BlockRef(config.chain_id, 0, "0x" + "00" * 32, 1)
        outcome = PositionOutcomeScope(
            config.chain_id,
            config.account,
            config.position_manager,
            config.state_view,
            config.expected_pins["position_manager"],
            config.expected_pins["state_view"],
            PoolKey(*config.pool.tuple()),
            position.token_id,
            position.lower,
            position.upper,
            position.liquidity,
            False,
        )
        nav_calls = nav_requests(config, block, position)
        if isinstance(config, PublicUsdConfig):
            nav_calls = nav_calls[:4] + nav_calls[12:]
        calls = (
            wallet_requests(WalletScope.from_config(config), block)
            + outcome_requests(outcome, block)
            + nav_calls
            + settlement_requests(block, tuple(states["strategy"].settled_blocks))
        )
        limits = {role: {source: len(calls) for source in SOURCES} for role in ROLES}
        if isinstance(config, PublicUsdConfig):
            # Both journals describe the same account and position. Acquire one
            # quorum; the other service independently replays its new raw records.
            limits["authority"] = dict.fromkeys(SOURCES, 0)
        limits["strategy"][SOURCES[0]] += 1  # shared initial head lookup
        if discover_gas_oracle:
            for source in SOURCES:
                limits["strategy"][source] += 6
        maximum = RUNTIME_MAX_REQUESTS if discover_gas_oracle else MAX_REQUESTS
        if sum(sum(values.values()) for values in limits.values()) > maximum or any(
            services[role]["budget_counts"][source] + limits[role][source] > profile.max_requests
            for role in ROLES
            for source in SOURCES
        ):
            raise ValueError("original quota cannot complete the entire bounded capture")
        plan = {
            "schema": "oasis.live-recovery-capture-plan.v1",
            "execution_eligible": False,
            "status": "proposal_not_authorization",
            "cost_status": "external_review_required",
            "config_hash": sha256_hex(canonical_json(asdict(config))),
            "profile": asdict(profile),
            "account": config.account,
            "chain_id": config.chain_id,
            "position": _position_identity(position),
            "anchors": states["strategy"].settled_blocks,
            "services": services,
            "request_limits": limits,
            "methods_per_provider_per_service": dict(Counter(method for method, _ in calls)),
            "total_requests": sum(sum(values.values()) for values in limits.values()),
            "max_seconds": MAX_SECONDS,
            "retries": 0,
            "transport_profile": TRANSPORT_PROFILE,
            "nav_schedule": "verify_price_prefix_before_inventory.v1",
        }
        if isinstance(config, PublicUsdConfig):
            plan.update(
                nav_schedule="public_usd_asof_block_before_inventory.v1",
                public_price_policy=config.usd_price_source,
                public_https_requests=4,
                account_capture="one_quorum_independent_replay_in_both_original_stores.v1",
                public_price_scope="Kraken/Bitstamp USD trades; no credentials or trading",
            )
        if discover_gas_oracle:
            plan.update(
                schema="oasis.live-runtime-recovery-capture-plan.v1",
                purpose="same_block_gas_discovery_and_two_service_nav_receipts",
                gas_oracle=ORACLE,
                gas_reads_per_source={
                    "eth_chainId": 1,
                    "eth_getBlockByNumber": 2,
                    "eth_getCode": 2,
                    "eth_getStorageAt": 1,
                },
                runtime_target="exact_source_config_plus_three_observed_gas_pins",
                runtime_approved=False,
                configuration_applied=False,
            )
        plan["plan_hash"] = sha256_hex(canonical_json(plan))

        def verify():
            for check in checks:
                check()
            if any(_identity(path) != identity for path, identity in identities.items()) or any(
                _unchanged(path) != identity for path, identity in immutable.items()
            ):
                raise ValueError("original recovery scope changed")
            for role, store in stores.items():
                state = store.load("live", read_only=True)
                if (
                    store.journal.revision != services[role]["journal_tip"]
                    or sha256_hex(canonical_json(asdict(state))) != services[role]["state_hash"]
                ):
                    raise ValueError("original journal changed during capture")

        verify()
        yield plan, states, evidence, budgets, verify


def _scope(path, approved_hash, plan, now, *, paid=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("regular cost scope required")
        raw = handle.read(16_385)
    if len(raw) > 16_384 or approved_hash != plan["plan_hash"]:
        raise ValueError("bounded scope and exact approved original plan required")
    scope = strict_json_object(raw)
    common = {
        "schema",
        "authorization_evidence_hash",
        "pricing_evidence_hash",
        "endpoint_hashes",
        "valid_until_ms",
    }
    if paid:
        methods = set(plan["methods_per_provider_per_service"]) | {"eth_getBlockByNumber"}
        methods.update(plan.get("gas_reads_per_source", {}))
        upper = sum(
            count * PAID_MICROS_PER_READ[source]
            for limits in plan["request_limits"].values()
            for source, count in limits.items()
        )
        prior, attempt = (
            scope.get("prior_cost_upper_micros"),
            scope.get("attempt_cost_upper_micros"),
        )
        if (
            set(scope)
            != common | {"plan_hash", "prior_cost_upper_micros", "attempt_cost_upper_micros"}
            or scope["schema"] != "oasis.live-recovery-paid-scope.v1"
            or scope["plan_hash"] != approved_hash
            or not methods <= PAID_READ_METHODS
            or type(prior) is not int
            or type(attempt) is not int
            or not 4_200 <= prior <= PAID_CEILING_MICROS
            or attempt != upper
            or prior + attempt > PAID_CEILING_MICROS
        ):
            raise ValueError("exact paid scope and cumulative ceiling required")
    elif (
        set(scope) != common | {"declared_cost_usd"}
        or scope["schema"] != "oasis.live-recovery-zero-cost-scope.v1"
        or scope["declared_cost_usd"] != "0"
    ):
        raise ValueError("explicit zero-cost scope declaration required")
    for field in ("authorization_evidence_hash", "pricing_evidence_hash"):
        if strict_digest(scope[field], field) == "0" * 64:
            raise ValueError("non-placeholder retained external review references required")
    if (
        type(scope["valid_until_ms"]) is not int
        or not now * 1000 < scope["valid_until_ms"] <= (now + 3600) * 1000
    ):
        raise ValueError("bounded current cost scope required")
    if not isinstance(scope["endpoint_hashes"], dict) or set(scope["endpoint_hashes"]) != set(
        SOURCES
    ):
        raise ValueError("two declared query endpoints required")
    for value in scope["endpoint_hashes"].values():
        strict_digest(value, "endpoint")
    return scope


class ResponseRefused(ValueError):
    """Fixed diagnostics only; provider error text must never enter retained output."""

    def __init__(self, category, *, code=None):
        super().__init__(category)
        self.category, self.code = category, code


def endpoint_reflected(value, url):
    parsed = urlsplit(url)
    components = {url, unquote(url)}
    if parsed.hostname:
        components.add(parsed.hostname)
    components.update(unquote(part) for part in parsed.path.split("/") if part)
    components.update(part for _, part in parse_qsl(parsed.query) if part)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            # Short routing components compare as whole fields; substring scans
            # would otherwise confuse a routing value like "1" with hex quantities.
            decoded = unquote(item)
            if any(part == decoded or (len(part) >= 8 and part in decoded) for part in components):
                return True
    return False


def successful_response(payload, raw, url):
    """Keep exact successful bytes; failed/malformed bodies may reflect credentials."""
    try:
        request, response = strict_json_object(payload), strict_json_object(raw)
        if set(response) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"}):
            raise ValueError("exact RPC response envelope required")
        _, response = decode_exchange(
            RpcExchange(request["method"], request["id"], payload, raw, 1)
        )
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ResponseRefused("rpc_protocol_error") from None
    if "error" in response:
        raise ResponseRefused("rpc_error", code=response["error"]["code"])
    if endpoint_reflected(response["result"], url):
        raise ResponseRefused("rpc_response_reflected_endpoint")
    return raw


class CaptureControl:
    def __init__(
        self,
        plan,
        budgets,
        profile,
        out,
        *,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=None,
        usage_limits=None,
    ):
        self.plan, self.budgets, self.profile = plan, budgets, profile
        self.usage_limits = usage_limits or {}
        self.clock, self.monotonic = clock, monotonic
        self.cancelled, self.lock = threading.Event(), threading.Lock()
        self.sleep = sleep or self.cancelled.wait
        self.paid = False
        self.started, self.started_mono = clock(), monotonic()
        self.deadline = self.started + MAX_SECONDS
        self.counts = {role: dict.fromkeys(SOURCES, 0) for role in ROLES}
        self.wire = (out / "wire.jsonl").open("xb")

    def remaining(self):
        now, mono = self.clock(), self.monotonic()
        left = min(self.deadline - now, MAX_SECONDS - (mono - self.started_mono))
        if (
            self.cancelled.is_set()
            or not all(math.isfinite(v) for v in (now, mono, left))
            or now < self.started
            or mono < self.started_mono
            or left <= 0
        ):
            raise ValueError("capture cancelled or deadline reached")
        return left

    def post(self, role, source, payload, url, transport):
        response, spent, outcome = None, False, "refused"
        failure = None
        http_status = None
        response_hash = response_size = rpc_error_code = None
        try:
            self.remaining()
            if self.paid and strict_json_object(payload).get("method") not in PAID_READ_METHODS:
                raise ValueError("paid read method not priced")
            if self.counts[role][source] >= self.plan["request_limits"][role][source]:
                raise ValueError("exact request allowance exhausted")
            while True:
                self.remaining()
                with RpcBudget(
                    self.budgets[role],
                    self.profile.policies(SOURCES),
                    require_existing=True,
                    usage_limit=self.usage_limits.get(role),
                ) as quota:
                    wait = quota.reserve(
                        source, now_ms=int(self.clock() * 1000), admission=self.remaining
                    )
                if not wait:
                    self.counts[role][source] += 1
                    spent = True
                    break
                if wait / 1000 >= self.remaining():
                    raise ValueError("pacing exceeds capture deadline")
                self.sleep(wait / 1000)
            # Durable attempted request before HTTP; survives worker kill or host failure.
            with self.lock:
                self.wire.write(
                    canonical_json(
                        {
                            "phase": "request_started",
                            "role": role,
                            "source": source,
                            "sequence": self.counts[role][source],
                            "request_hex": payload.hex(),
                            "budget_spent": True,
                        }
                    )
                    + b"\n"
                )
                self.wire.flush()
                os.fsync(self.wire.fileno())
            raw = transport(url, min(15.0, self.remaining())).post(payload)
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
                raise ResponseRefused("rpc_protocol_error")
            response_hash, response_size = sha256_hex(raw), len(raw)
            response = successful_response(payload, raw, url)
            self.remaining()
            outcome = "response_received"
            return response
        except BaseException as exc:
            self.cancelled.set()
            failure = (
                exc.category
                if isinstance(exc, ResponseRefused)
                else "http_error"
                if isinstance(exc, HTTPError)
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "transport_or_scope_error"
            )
            if isinstance(exc, HTTPError) and type(exc.code) is int and 100 <= exc.code <= 599:
                http_status = exc.code
            if isinstance(exc, ResponseRefused):
                rpc_error_code = exc.code
            raise
        finally:
            with self.lock:
                self.wire.write(
                    canonical_json(
                        {
                            "phase": "request_finished",
                            "role": role,
                            "source": source,
                            "sequence": self.counts[role][source],
                            "http_status": http_status,
                            "rpc_error_code": rpc_error_code,
                            "failure_category": failure,
                            "request_hex": payload.hex(),
                            "response_hex": response.hex() if response is not None else None,
                            "response_hash": response_hash,
                            "response_size": response_size,
                            "budget_spent": spent,
                            "outcome": outcome,
                        }
                    )
                    + b"\n"
                )
                self.wire.flush()
                os.fsync(self.wire.fileno())

    def session(self, role, source, urls, transport):
        control = self

        class Poster:
            def post(self, payload):
                return control.post(role, source, payload, urls[source], transport)

        recorder = BoundedExchangeRecorder()
        rpc = JsonRpcTransport(
            "https://unused.invalid", http=Poster(), recorder=recorder, retries=0
        )
        return rpc, recorder, source


def _capture_gas_candidate(control, urls, transport, header, config, out, verify):
    """Use the shared head and original strategy budgets; never approve the proposal."""
    block, captures, observed = header.ref, [], []
    for source in SOURCES:
        verify()
        control.remaining()
        rpc, recorder, _ = control.session("strategy", source, urls, transport)
        values = []
        for index, (method, params) in enumerate(discovery_requests(block, ORACLE)[:4]):
            value = rpc.request(method, params)
            if index == 0 and rpc_quantity(value) != config.chain_id:
                raise ValueError("gas discovery chain differs")
            if index == 1 and decode_header(value, chain_id=config.chain_id) != header:
                raise ValueError("gas discovery opening header differs")
            if index == 2:
                runtime_hash(value)
            if index == 3:
                implementation_address(value)
            if observed and index >= 2 and value != observed[0][index]:
                raise ValueError("gas discovery providers disagree")
            values.append(value)
        implementation = implementation_address(values[3])
        tail = discovery_requests(block, implementation)[4:]
        code = rpc.request(*tail[0])
        runtime_hash(code)
        if observed and code != observed[0][4]:
            raise ValueError("gas implementation providers disagree")
        if decode_header(rpc.request(*tail[1]), chain_id=config.chain_id) != header:
            raise ValueError("gas discovery closing header differs")
        observed.append([*values, code])
        captures.append(recorder.exchanges)
    verify()
    control.remaining()
    with closing(EvidenceStore(out / "gas-evidence.db")) as evidence:
        refs = retain_discovery_quorum(evidence, block, (captures[0], captures[1]), sources=SOURCES)
        candidate = candidate_configuration(
            evidence, refs, block, config, sources=SOURCES, now=int(control.clock())
        )
    with (out / "gas-candidate.json").open("xb") as handle:
        handle.write(canonical_json(candidate))
    return LiveConfig.from_json(canonical_json(candidate["candidate_config"])), refs


def _share_new_account_proofs(origin_path, target, references, head):
    """Retain only this head's new records; never copy a database or budget."""
    allowed = {
        "live_public_usd_nav",
        "live_public_usd_prices",
        "live_wallet",
        "live_position_outcome",
        "live_rearm_settlements",
    }
    pending = [
        evidence_ref_from_json(row) for field in ("nav", "settlements") for row in references[field]
    ]
    seen = set()
    with closing(ReadOnlyEvidenceStore(origin_path)) as origin:
        while pending:
            ref = pending.pop()
            key = canonical_json(asdict(ref))
            if key in seen:
                continue
            if len(seen) >= 16 or ref.kind not in allowed or ref.block != head:
                raise ValueError("only bounded newly captured account proofs may be shared")
            seen.add(key)
            record = origin.get_record(ref)
            if ref.kind == "live_public_usd_nav":
                body = strict_json_object(record.payload)
                pending.extend(
                    evidence_ref_from_json(body[name])
                    for name in ("wallet", "position_evidence", "price_evidence")
                    if body.get(name) is not None
                )
            copied = target.put(
                ref.kind,
                record.payload,
                ref.block,
                ref.source,
                request=record.request_payload,
                raw_response=record.raw_response_payload,
                interpretation=record.interpretation_payload,
                retrieved_at=ref.retrieved_at,
                schema_version=ref.schema_version,
            )
            if copied != ref:
                raise ValueError("new proof provenance changed while sharing")
    return tuple(evidence_ref_from_json(row) for row in references["nav"])


def capture(
    strategy,
    authority,
    config,
    profile,
    out,
    approved_hash,
    scope_path,
    *,
    discover_gas_oracle=False,
    paid=False,
    clock=time.time,
    monotonic=time.monotonic,
    sleep=None,
    transport=BoundedPoster,
):
    """Raw capture library; a hard process supervisor is REQUIRED for network use.

    This does not accept original action-history proofs or rearm either service.
    """
    if any(
        out.absolute().is_relative_to(directory.absolute()) for directory in (strategy, authority)
    ):
        raise ValueError("report directory must be outside original service directories")
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {
        "execution_eligible": False,
        "capture_passed": False,
        "status": "refused",
        "exit_code": 3,
        "request_counts": None,
    }
    control = None
    try:
        with prepared(
            strategy, authority, config, profile, discover_gas_oracle=discover_gas_oracle
        ) as (
            plan,
            states,
            evidence_paths,
            budgets,
            verify,
        ):
            report["plan"] = plan
            if discover_gas_oracle:
                report.update(runtime_approved=False, configuration_applied=False)
            scope = _scope(scope_path, approved_hash, plan, clock(), paid=paid)
            report["cost_scope_hash"] = sha256_hex(canonical_json(scope))
            report["cost_scope_status"] = "operator_declaration_not_provider_billing_verification"
            urls = {
                source: os.environ.get(name, "")
                for source, name in zip(SOURCES, RPC_ENV, strict=True)
            }
            if len({urlsplit(url).hostname for url in urls.values()}) != 2 or any(
                urlsplit(url).scheme != "https"
                or not urlsplit(url).hostname
                or urlsplit(url).username is not None
                or urlsplit(url).password is not None
                or urlsplit(url).fragment
                or sha256_hex(url.encode()) != scope["endpoint_hashes"][source]
                for source, url in urls.items()
            ):
                raise ValueError("declared independent query endpoints unresolved")
            if paid:
                for source, url in urls.items():
                    host = urlsplit(url).hostname or ""
                    domain = PAID_DOMAINS[source]
                    if host != domain and not host.endswith("." + domain):
                        raise ValueError("paid provider differs from pricing scope")
                report["paid_cost_upper_micros"] = scope["attempt_cost_upper_micros"]
                report["cumulative_cost_upper_micros"] = (
                    scope["prior_cost_upper_micros"] + scope["attempt_cost_upper_micros"]
                )
            (out / "plan.json").write_bytes(canonical_json(plan))
            control = CaptureControl(
                plan, budgets, profile, out, clock=clock, monotonic=monotonic, sleep=sleep
            )
            control.paid = paid
            control.deadline = min(control.deadline, scope["valid_until_ms"] / 1000)
            head_session = control.session("strategy", SOURCES[0], urls, transport)
            header = head_session[0].request("eth_getBlockByNumber", ["latest", False])
            header = decode_header(header, chain_id=config.chain_id)
            head = header.ref
            if head.timestamp is None or not 0 <= clock() - head.timestamp <= 120:
                raise ValueError("fresh pinned head required")
            control.deadline = min(control.deadline, head.timestamp + 120)
            report["head"] = asdict(head)
            if discover_gas_oracle:
                config, gas_refs = _capture_gas_candidate(
                    control, urls, transport, header, config, out, verify
                )
                report["target_config_hash"] = sha256_hex(canonical_json(asdict(config)))
            public_prices = None
            if isinstance(config, PublicUsdConfig):
                control.remaining()
                report["public_https_requests_attempted"] = 0

                def price_attempt(_):
                    report["public_https_requests_attempted"] += 1

                public_prices = fetch_public_prices(on_request=price_attempt)
                control.remaining()
            references, values = {}, []
            for role in ROLES:
                verify()
                control.remaining()
                with closing(
                    EvidenceStore(evidence_paths[role], require_existing=True)
                ) as evidence:

                    def session(source, service=role):
                        return control.session(service, source, urls, transport)

                    if isinstance(config, PublicUsdConfig) and role == "authority":
                        refs = _share_new_account_proofs(
                            evidence_paths["strategy"], evidence, references["strategy"], head
                        )
                        value = replay_nav_quorum(evidence, refs, config, sources=SOURCES)
                        control.deadline = min(
                            control.deadline,
                            nav_fresh_until(evidence, refs, config, sources=SOURCES),
                        )
                        review_checkpoint(
                            config,
                            states[role],
                            plan["services"][role]["config_hash"],
                            evidence,
                            fresh=refs,
                            now=int(clock()),
                        )
                        references[role] = references["strategy"]
                        values.append(value)
                        control.remaining()
                        continue
                    value, refs = capture_nav(
                        evidence,
                        config,
                        head,
                        states[role].position,
                        sources=SOURCES,
                        sessions=session,
                        **(
                            {"public_prices": public_prices, "observed_at_ms": int(clock() * 1000)}
                            if public_prices is not None
                            else {}
                        ),
                    )
                    if discover_gas_oracle and value.wallet.header != header:
                        raise ValueError("gas and NAV full headers differ")
                    control.deadline = min(
                        control.deadline, nav_fresh_until(evidence, refs, config, sources=SOURCES)
                    )
                    control.remaining()
                    review_checkpoint(
                        config,
                        states[role],
                        plan["services"][role]["config_hash"],
                        evidence,
                        fresh=refs,
                        now=int(clock()),
                    )
                    anchors = tuple(states[role].settled_blocks)
                    pair = session(SOURCES[0]), session(SOURCES[1])

                    def verify_settlement_head(pair=pair, nav_header=value.wallet.header):
                        for _, recorder, _ in pair:
                            require_settlement_opening(recorder.exchanges, nav_header)

                    acquire_pair(
                        pair,
                        settlement_requests(head, anchors),
                        checkpoint=(2, verify_settlement_head),
                    )
                    captures = tuple(
                        SettlementHeadCapture(head, anchors, item[2], item[1].exchanges)
                        for item in pair
                    )
                    if captures[0].reconstruct(nav_header=value.wallet.header) != captures[
                        1
                    ].reconstruct(nav_header=value.wallet.header):
                        raise ValueError("settled receipts disagree between providers")
                    settlement_refs = [persist_settlement_head(evidence, item) for item in captures]
                    references[role] = {
                        "nav": [asdict(ref) for ref in refs],
                        "settlements": [asdict(ref) for ref in settlement_refs],
                    }
                    values.append(value)
            verify()
            control.remaining()
            if values[0] != values[1] or control.counts != plan["request_limits"]:
                raise ValueError("complete two-service capture required")
            for role in ROLES:
                with RpcBudget(budgets[role], profile.policies(SOURCES), read_only=True) as quota:
                    if quota.counts() != {
                        source: plan["services"][role]["budget_counts"][source]
                        + control.counts[role][source]
                        for source in SOURCES
                    }:
                        raise ValueError("original budget changed outside this attempt")
            (out / "candidate-references.json").write_bytes(canonical_json(references))
            if discover_gas_oracle:
                runtime_refs = {
                    "schema": "oasis.runtime-transition-inputs.v1",
                    "nav": {role: references[role]["nav"] for role in ROLES},
                    "gas": [asdict(ref) for ref in gas_refs],
                }
                with (out / "candidate-runtime-references.json").open("xb") as handle:
                    handle.write(canonical_json(runtime_refs))
            report.update(
                capture_passed=True,
                status="raw_capture_requires_offline_rearm_review",
                exit_code=0,
                nav_micros=values[0].nav_micros,
            )
    except Exception as error:  # noqa: BLE001 - never echo endpoints or arbitrary exception text
        if control is not None:
            control.cancelled.set()
        report["refusal_code"] = "capture_unresolved"
        # Exact fixed library messages only; never stringify an untrusted error or argument.
        if type(error) is NavRefusal and len(error.args) == 1 and type(error.args[0]) is str:
            report["refusal_code"] = NAV_REFUSAL_CODES.get(error.args[0], "capture_unresolved")
        report["refusal"] = "capture scope, transport, freshness or evidence unresolved; no retry"
    finally:
        if control is not None:
            report["request_counts"] = control.counts
            control.wire.close()
        report["evidence_hash"] = sha256_hex(canonical_json(report))
        with (out / "report.json").open("xb") as handle:
            handle.write(canonical_json(report))
    return report


def _nav_publications(out, worker):
    refs_raw = (out / "candidate-references.json").read_bytes()
    refs = strict_json_object(refs_raw)
    if set(refs) != set(ROLES):
        raise ValueError("complete raw capture required")
    return {
        "fresh-nav.json": canonical_json({role: refs[role]["nav"] for role in ROLES}),
        "references.json": refs_raw,
    }


def _runtime_publications(out, worker, config):
    publications = _nav_publications(out, worker)
    refs_raw = (out / "candidate-runtime-references.json").read_bytes()
    refs = strict_json_object(refs_raw)
    candidate = strict_json_object((out / "gas-candidate.json").read_bytes())
    target = LiveConfig.from_json(canonical_json(candidate["candidate_config"]))
    gas_refs = tuple(evidence_ref_from_json(ref) for ref in refs["gas"])
    with closing(ReadOnlyEvidenceStore(out / "gas-evidence.db")) as evidence:
        expected = candidate_configuration(
            evidence,
            gas_refs,
            gas_refs[0].block,
            config,
            sources=SOURCES,
            now=candidate["reviewed_at"],
        )
    if (
        candidate != expected
        or refs["nav"] != strict_json_object(publications["fresh-nav.json"])
        or worker["head"] != candidate["block"]
        or worker["target_config_hash"] != candidate["candidate_config_hash"]
        or worker["plan"]["config_hash"] != candidate["source_config_hash"]
        or worker["plan"]["schema"] != "oasis.live-runtime-recovery-capture-plan.v1"
    ):
        raise ValueError("joint capture publication scope differs")
    report = review_runtime_transition(
        *(Path(worker["plan"]["services"][role]["directory"]) for role in ROLES),
        target,
        refs,
        out / "gas-evidence.db",
        now=int(time.time()),
    )
    if not report["block"]["timestamp"] <= int(time.time()) <= report["valid_until"]:
        raise ValueError("joint capture expired before publication")
    publications.update(
        {
            "runtime-target.json": canonical_json(asdict(target)),
            "runtime-references.json": refs_raw,
            "runtime-review.json": canonical_json(report),
        }
    )
    return publications


def supervise(
    operation,
    out,
    *,
    seconds=MAX_SECONDS,
    publisher=_nav_publications,
    publication_names=("fresh-nav.json", "references.json"),
):
    """POSIX CLI boundary: terminate the entire request worker at the hard deadline.

    The parent starts its clock before fork. It publishes reference files only after
    an exit-zero worker has completed inside that window. Failed/late evidence is
    retained for inspection, never promoted to a successful capture report.
    """
    if (
        not 0 < seconds <= MAX_SECONDS
        or os.path.lexists(out)
        or not publication_names
        or len(set(publication_names)) != len(publication_names)
        or any(
            not isinstance(name, str) or not name or name in {".", ".."} or Path(name).name != name
            for name in publication_names
        )
    ):
        raise ValueError("fresh attempt directory and bounded deadline required")
    started = time.monotonic()
    ready_read, ready_write = os.pipe()
    os.set_blocking(ready_read, False)
    try:
        pid = os.fork()
    except OSError:
        os.close(ready_read)
        os.close(ready_write)
        raise
    if pid == 0:
        try:
            os.close(ready_read)
            # A supervisor killed by SIGTERM/SIGKILL cannot run its finally block.
            # A default-action kernel alarm still terminates this worker and all
            # its threads even while DNS/HTTP is blocked. Never extend the parent
            # window, and do not inherit an ignored or blocked alarm from it.
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                os._exit(3)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
            signal.setitimer(signal.ITIMER_REAL, remaining)
            os.setsid()
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            # No acquisition can start before the parent can identify this
            # child's exclusive process group, even if the leader exits early.
            os.write(ready_write, b"1")
            os.close(ready_write)
            allowed = {
                name: os.environ[name]
                for name in (*RPC_ENV, "PATH", "PYTHONPATH")
                if name in os.environ
            }
            os.environ.clear()
            os.environ.update(allowed)
            report = operation()
            os._exit(
                0 if report.get("capture_passed") is True and report.get("exit_code") == 0 else 3
            )
        except BaseException:  # noqa: BLE001 - worker errors never expose credentials
            os._exit(3)
    os.close(ready_write)
    status, timed_out, group_ready, cleanup_refused = None, False, False, False

    def confirm_group():
        nonlocal group_ready
        if not group_ready:
            with suppress(BlockingIOError):
                group_ready = os.read(ready_read, 1) == b"1"

    try:
        while True:
            confirm_group()
            done, candidate = os.waitpid(pid, os.WNOHANG)
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                status = candidate if done else None
                break
            if done:
                status = candidate
                break
            time.sleep(min(0.02, remaining))
    finally:
        # A reaped leader can leave descendants. Use one SIGKILL for that group
        # instead of sending consecutive signals with no grace interval.
        signals = (signal.SIGTERM, signal.SIGKILL) if status is None else (signal.SIGKILL,)
        for sig in signals:
            confirm_group()
            try:
                if group_ready:
                    os.killpg(pid, sig)
                elif status is None:
                    # Before readiness, the child has no acquisition capability
                    # or descendants. Target only our still-unreaped child PID.
                    os.kill(pid, sig)
            except ProcessLookupError:
                if status is None:
                    with suppress(ProcessLookupError):
                        os.kill(pid, sig)
            except PermissionError:
                # A denied group kill cannot count as successful supervision.
                cleanup_refused = True
                if status is None:
                    with suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, sig)
            if sig == signal.SIGTERM and status is None:
                time.sleep(0.05)
        if status is None:
            _, status = os.waitpid(pid, 0)
        os.close(ready_read)
    accepted = not timed_out and not cleanup_refused and os.waitstatus_to_exitcode(status) == 0
    report = {
        "schema": "oasis.live-recovery-supervision.v1",
        "execution_eligible": False,
        "capture_passed": False,
        "exit_code": 3,
        "status": "hard_deadline_refused" if timed_out else "worker_refused",
        "worker_exit_code": os.waitstatus_to_exitcode(status),
        "worker_group_ready": group_ready,
        "cleanup_refused": cleanup_refused,
        "max_seconds": seconds,
        "elapsed_seconds": time.monotonic() - started,
    }
    out.mkdir(mode=0o700, parents=False, exist_ok=True)
    if accepted:
        try:
            raw = (out / "report.json").read_bytes()
            worker = strict_json_object(raw)
            if not worker["capture_passed"] or worker["exit_code"] != 0:
                raise ValueError("complete raw capture required")
            publications = publisher(out, worker)
            if set(publications) != set(publication_names) or any(
                not isinstance(raw, bytes) for raw in publications.values()
            ):
                raise ValueError("exact bounded publication inventory required")
            report.update(
                capture_passed=True,
                exit_code=0,
                status="raw_capture_requires_offline_rearm_review",
                worker_report_hash=sha256_hex(raw),
            )
            for name in publication_names:
                if time.monotonic() - started >= seconds:
                    raise TimeoutError("capture publication deadline reached")
                with (out / name).open("xb") as handle:
                    handle.write(publications[name])
        except Exception:  # noqa: BLE001 - leave artifacts and report refusal
            report.update(capture_passed=False, exit_code=3, status="worker_artifacts_unresolved")
    report["elapsed_seconds"] = time.monotonic() - started
    if report["elapsed_seconds"] >= seconds:
        report.update(capture_passed=False, exit_code=3, status="hard_deadline_refused")
    if not report["capture_passed"]:
        # Remove only this attempt's incomplete publication; raw/candidate evidence stays.
        for name in publication_names:
            (out / name).unlink(missing_ok=True)
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (out / "supervisor.json").open("xb") as handle:
        handle.write(canonical_json(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-dir", type=Path, required=True)
    parser.add_argument("--authority-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--rpc-profile", type=Path, default=ROOT / "configs/live.rpc.canary.json")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument(
        "--discover-gas-oracle",
        action="store_true",
        help="separately authorized joint gas discovery/NAV scope (at most 209 reads)",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--approved-plan-hash")
    cost = parser.add_mutually_exclusive_group()
    cost.add_argument("--zero-cost-scope", type=Path)
    cost.add_argument("--paid-scope", type=Path)
    args = parser.parse_args()
    scope = args.paid_scope or args.zero_cost_scope
    if args.capture != all(
        value is not None for value in (args.out, args.approved_plan_hash, scope)
    ) or (
        not args.capture
        and any(value is not None for value in (args.out, args.approved_plan_hash, scope))
    ):
        parser.error(
            "--capture requires --out, --approved-plan-hash and exactly one cost scope; "
            "plan mode accepts none"
        )
    try:
        config, profile = LiveConfig.load(args.config), LiveRpcProfile.load(args.rpc_profile)
        if args.capture:
            report = supervise(
                lambda: capture(
                    args.strategy_dir,
                    args.authority_dir,
                    config,
                    profile,
                    args.out,
                    args.approved_plan_hash,
                    scope,
                    paid=args.paid_scope is not None,
                    discover_gas_oracle=args.discover_gas_oracle,
                ),
                args.out,
                **(
                    {
                        "publisher": lambda out, worker: _runtime_publications(out, worker, config),
                        "publication_names": RUNTIME_PUBLICATIONS,
                    }
                    if args.discover_gas_oracle
                    else {}
                ),
            )
        else:
            with prepared(
                args.strategy_dir,
                args.authority_dir,
                config,
                profile,
                discover_gas_oracle=args.discover_gas_oracle,
            ) as prepared_scope:
                report = prepared_scope[0]
        print(json.dumps(report, indent=2))
        raise SystemExit(report.get("exit_code", 0))
    except Exception:  # noqa: BLE001 - fixed redacted offline refusal
        parser.exit(3, "Recovery capture inputs unresolved; no service startup authorized.\n")


if __name__ == "__main__":
    main()
