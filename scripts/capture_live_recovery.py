"""Plan, or explicitly capture, one stopped two-service recovery evidence window.

Default mode is offline and does not read RPC environment variables. --capture
requires an exact approved plan hash and a retained zero-cost scope declaration.
Those declarations record external review; this tool cannot verify provider billing
or create authorization. It never creates budgets, changes state/STOP, or signs.
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
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from oasis_agent.evidence.exact_capture import decode_header
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.rpc_validation import decode_exchange, strict_digest, strict_json_object
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.live.acquisition import acquire_pair
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.feed_transition import _no_acquisition, _service_lock, historical_config
from oasis_agent.live.positions import PositionOutcomeScope, outcome_requests
from oasis_agent.live.rearm_settlements import (
    SettlementHeadCapture,
    persist_settlement_head,
    settlement_requests,
)
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.startup_state import SOURCES, _position_identity, paths, require_file
from oasis_agent.live.state import StateStore, day_of, validate_state
from oasis_agent.live.valuation import capture_nav, nav_fresh_until, replay_nav, replay_nav_quorum
from oasis_agent.live.valuation import requests as nav_requests
from oasis_agent.live.wallet import WalletScope, wallet_requests
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.models.v4 import PoolKey
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("strategy", "authority")
RPC_ENV = ("BASE_RPC_PRIMARY_ALT", "BASE_RPC_PRIMARY")
MAX_SECONDS = 120
MAX_REQUESTS = 197
MAX_RESPONSE = 1_000_000


def review_checkpoint(config, state, binding, evidence, *, fresh=(), now):
    """Replay only the exact strict policy or legacy USDC age, preserving all risk."""
    previous = (
        config
        if binding == sha256_hex(canonical_json(asdict(config)))
        else historical_config(config, binding)
    )
    validate_state(state)
    risk = state.exact_risk
    if (
        state.mode != "live"
        or (previous is config and not state.halted)
        or state.pending is not None
        or state.signing_attempt is not None
        or state.legacy_gas_cost_incomplete
        or state.acknowledge_flow
        or risk is None
        or risk.basis is None
        or risk.last_nav is None
        or state.last_units is None
        or risk.day != day_of(risk.last_at)
        or not 0 < risk.checkpoint_at <= risk.last_at
    ):
        raise ValueError("complete halted integer baseline required")
    if any(
        value >= limit * 1_000_000
        for value, limit in zip(
            risk.losses(risk.last_nav),
            (config.max_daily_loss_usd, config.max_cumulative_loss_usd),
            strict=True,
        )
    ):
        raise ValueError("existing loss latch cannot be recovered by capture")
    loop = object.__new__(LiveLoop)
    loop.mode, loop.c, loop.state = "live", previous, deepcopy(state)
    loop.receipt_costs = RetainedReceiptCostReader(
        evidence,
        chain_id=previous.chain_id,
        account=previous.account,
        native_decimals=previous.decimals0,
        sources=SOURCES,
        sessions=_no_acquisition,
    )
    loop._check_nav_checkpoint()
    prior_nonce = None
    if state.nav_evidence:
        prior = replay_nav_quorum(evidence, state.nav_evidence, previous, sources=SOURCES)
        prior_position = replay_nav(evidence, state.nav_evidence[0], previous).position
        if (
            _position_identity(prior_position) != _position_identity(state.position)
            or [
                prior.wallet.native,
                prior.wallet.token,
                prior_position.liquidity if prior_position else 0,
            ]
            != state.last_units
        ):
            raise ValueError("historical holdings differ from checkpoint")
        prior_nonce = prior.wallet.nonce
    if not fresh:
        return
    value = replay_nav_quorum(evidence, fresh, config, sources=SOURCES)
    position = replay_nav(evidence, fresh[0], config).position
    block = value.wallet.header.ref
    if (
        block.timestamp is None
        or not risk.last_at
        < block.timestamp
        <= now
        <= nav_fresh_until(evidence, fresh, config, sources=SOURCES)
        or _position_identity(position) != _position_identity(state.position)
        or [value.wallet.native, value.wallet.token, position.liquidity if position else 0]
        != state.last_units
        or (prior_nonce is not None and prior_nonce != value.wallet.nonce)
        or any(
            a["number"] > block.number
            or (a["number"] == block.number and a["hash"] != block.block_hash)
            for a in state.settled_blocks
        )
    ):
        raise ValueError("fresh NAV does not reconcile original inventory or nonce")
    losses = deepcopy(risk).observe(
        day=day_of(block.timestamp), at=block.timestamp, nav=value.nav_micros, checkpoint=True
    )
    if any(
        value >= limit * 1_000_000
        for value, limit in zip(
            losses, (config.max_daily_loss_usd, config.max_cumulative_loss_usd), strict=True
        )
    ):
        raise ValueError("fresh loss budget exhausted")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect outside declared request scope")


class BoundedPoster:
    """One HTTPS request, no redirects/retries; CLI supervisor enforces hard cutoff."""

    def __init__(self, url, timeout):
        self.url, self.timeout = url, timeout

    def post(self, payload):
        request = Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("bounded response required")
            return raw


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
def prepared(strategy, authority, config, profile):
    """Hold original EX locks/STOP, and inspect originals without initializing any file."""
    config.validate()
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
        calls = (
            wallet_requests(WalletScope.from_config(config), block)
            + outcome_requests(outcome, block)
            + nav_requests(config, block, position)
            + settlement_requests(block, tuple(states["strategy"].settled_blocks))
        )
        limits = {role: {source: len(calls) for source in SOURCES} for role in ROLES}
        limits["strategy"][SOURCES[0]] += 1  # shared initial head lookup
        if sum(sum(values.values()) for values in limits.values()) > MAX_REQUESTS or any(
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
        }
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


def _scope(path, approved_hash, plan, now):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("regular cost scope required")
        raw = handle.read(16_385)
    if len(raw) > 16_384 or approved_hash != plan["plan_hash"]:
        raise ValueError("bounded scope and exact approved original plan required")
    scope = strict_json_object(raw)
    if (
        set(scope)
        != {
            "schema",
            "declared_cost_usd",
            "authorization_evidence_hash",
            "pricing_evidence_hash",
            "endpoint_hashes",
            "valid_until_ms",
        }
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
        self, plan, budgets, profile, out, *, clock=time.time, monotonic=time.monotonic, sleep=None
    ):
        self.plan, self.budgets, self.profile = plan, budgets, profile
        self.clock, self.monotonic = clock, monotonic
        self.cancelled, self.lock = threading.Event(), threading.Lock()
        self.sleep = sleep or self.cancelled.wait
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
            if self.counts[role][source] >= self.plan["request_limits"][role][source]:
                raise ValueError("exact request allowance exhausted")
            while True:
                self.remaining()
                with RpcBudget(
                    self.budgets[role], self.profile.policies(SOURCES), require_existing=True
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


def capture(
    strategy,
    authority,
    config,
    profile,
    out,
    approved_hash,
    scope_path,
    *,
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
        with prepared(strategy, authority, config, profile) as (
            plan,
            states,
            evidence_paths,
            budgets,
            verify,
        ):
            report["plan"] = plan
            scope = _scope(scope_path, approved_hash, plan, clock())
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
            (out / "plan.json").write_bytes(canonical_json(plan))
            control = CaptureControl(
                plan, budgets, profile, out, clock=clock, monotonic=monotonic, sleep=sleep
            )
            control.deadline = min(control.deadline, scope["valid_until_ms"] / 1000)
            head_session = control.session("strategy", SOURCES[0], urls, transport)
            header = head_session[0].request("eth_getBlockByNumber", ["latest", False])
            head = decode_header(header, chain_id=config.chain_id).ref
            if head.timestamp is None or not 0 <= clock() - head.timestamp <= 120:
                raise ValueError("fresh pinned head required")
            control.deadline = min(control.deadline, head.timestamp + 120)
            report["head"] = asdict(head)
            references, values = {}, []
            for role in ROLES:
                verify()
                control.remaining()
                with closing(
                    EvidenceStore(evidence_paths[role], require_existing=True)
                ) as evidence:

                    def session(source, service=role):
                        return control.session(service, source, urls, transport)

                    value, refs = capture_nav(
                        evidence,
                        config,
                        head,
                        states[role].position,
                        sources=SOURCES,
                        sessions=session,
                    )
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
                    acquire_pair(pair, settlement_requests(head, anchors))
                    captures = tuple(
                        SettlementHeadCapture(head, anchors, item[2], item[1].exchanges)
                        for item in pair
                    )
                    if captures[0].reconstruct() != captures[1].reconstruct():
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
            report.update(
                capture_passed=True,
                status="raw_capture_requires_offline_rearm_review",
                exit_code=0,
                nav_micros=values[0].nav_micros,
            )
    except Exception:  # noqa: BLE001 - never echo endpoints, provider errors or credentials
        if control is not None:
            control.cancelled.set()
        report["refusal"] = "capture scope, transport, freshness or evidence unresolved; no retry"
    finally:
        if control is not None:
            report["request_counts"] = control.counts
            control.wire.close()
        report["evidence_hash"] = sha256_hex(canonical_json(report))
        with (out / "report.json").open("xb") as handle:
            handle.write(canonical_json(report))
    return report


def supervise(operation, out, *, seconds=MAX_SECONDS):
    """POSIX CLI boundary: terminate the entire request worker at the hard deadline.

    The parent starts its clock before fork. It publishes reference files only after
    an exit-zero worker has completed inside that window. Failed/late evidence is
    retained for inspection, never promoted to a successful capture report.
    """
    if not 0 < seconds <= MAX_SECONDS or os.path.lexists(out):
        raise ValueError("fresh attempt directory and bounded deadline required")
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
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
    status, timed_out = None, False
    try:
        while True:
            done, candidate = os.waitpid(pid, os.WNOHANG)
            if time.monotonic() - started >= seconds:
                timed_out = True
                status = candidate if done else None
                break
            if done:
                status = candidate
                break
            time.sleep(min(0.02, seconds - (time.monotonic() - started)))
    finally:
        # A reaped leader can leave descendants; terminate its group in either case.
        # The worker calls setsid before any network capability is constructed.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                if status is None:
                    with suppress(ProcessLookupError):
                        os.kill(pid, sig)
            if sig == signal.SIGTERM and status is None:
                time.sleep(0.05)
        if status is None:
            _, status = os.waitpid(pid, 0)
    accepted = not timed_out and os.waitstatus_to_exitcode(status) == 0
    report = {
        "schema": "oasis.live-recovery-supervision.v1",
        "execution_eligible": False,
        "capture_passed": False,
        "exit_code": 3,
        "status": "hard_deadline_refused" if timed_out else "worker_refused",
        "worker_exit_code": os.waitstatus_to_exitcode(status),
        "max_seconds": seconds,
        "elapsed_seconds": time.monotonic() - started,
    }
    out.mkdir(mode=0o700, parents=False, exist_ok=True)
    if accepted:
        try:
            raw = (out / "report.json").read_bytes()
            worker = strict_json_object(raw)
            refs_raw = (out / "candidate-references.json").read_bytes()
            refs = strict_json_object(refs_raw)
            if not worker["capture_passed"] or worker["exit_code"] != 0 or set(refs) != set(ROLES):
                raise ValueError("complete raw capture required")
            report.update(
                capture_passed=True,
                exit_code=0,
                status="raw_capture_requires_offline_rearm_review",
                worker_report_hash=sha256_hex(raw),
            )
            if time.monotonic() - started >= seconds:
                raise TimeoutError("capture publication deadline reached")
            with (out / "fresh-nav.json").open("xb") as handle:
                handle.write(canonical_json({role: refs[role]["nav"] for role in ROLES}))
            if time.monotonic() - started >= seconds:
                raise TimeoutError("capture publication deadline reached")
            with (out / "references.json").open("xb") as handle:
                handle.write(refs_raw)
        except Exception:  # noqa: BLE001 - leave artifacts and report refusal
            report.update(capture_passed=False, exit_code=3, status="worker_artifacts_unresolved")
    report["elapsed_seconds"] = time.monotonic() - started
    if report["elapsed_seconds"] >= seconds:
        report.update(capture_passed=False, exit_code=3, status="hard_deadline_refused")
    if not report["capture_passed"]:
        # Remove only this attempt's incomplete publication; raw/candidate evidence stays.
        for name in ("references.json", "fresh-nav.json"):
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
    parser.add_argument("--out", type=Path)
    parser.add_argument("--approved-plan-hash")
    parser.add_argument("--zero-cost-scope", type=Path)
    args = parser.parse_args()
    if args.capture != all(
        value is not None for value in (args.out, args.approved_plan_hash, args.zero_cost_scope)
    ) or (
        not args.capture
        and any(
            value is not None for value in (args.out, args.approved_plan_hash, args.zero_cost_scope)
        )
    ):
        parser.error(
            "--capture requires --out, --approved-plan-hash and --zero-cost-scope; "
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
                    args.zero_cost_scope,
                ),
                args.out,
            )
        else:
            with prepared(args.strategy_dir, args.authority_dir, config, profile) as prepared_scope:
                report = prepared_scope[0]
        print(json.dumps(report, indent=2))
        raise SystemExit(report.get("exit_code", 0))
    except Exception:  # noqa: BLE001 - fixed redacted offline refusal
        parser.exit(3, "Recovery capture inputs unresolved; no service startup authorized.\n")


if __name__ == "__main__":
    main()
