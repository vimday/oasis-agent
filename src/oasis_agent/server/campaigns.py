"""Versioned, independently managed LP-shadow and Launch-observation campaigns.

Only two fixed observation entry points can be started. Secrets are ephemeral
or selected from an explicit environment allowlist; child environments contain
no ambient credentials. A lifetime flock is inherited by children so a new
controller cannot race an orphan after an unclean server exit.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ..errors import ConfigurationError
from ..launch.models import Address, StrictModel
from ..models.common import canonical_json, sha256_hex
from ..shadow.market_archive import ArchiveConfig
from ..shadow.observer_config import Config as ObserverConfig
from ..shadow.service import parse_policy, serializable
from ..sniping.config import ExecutionConfig
from .audit import ServerOpsJournal
from .config import ServerCampaignInput, ServerConfig, ServerShadowRunnerConfig
from .runner import ShadowRunCoordinator

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ACTIVE = frozenset({"queued", "running", "stopping"})
MAX_ASSET_BYTES = 256 * 1024 * 1024
_REPOSITORY = Path(__file__).resolve().parents[3]


class CampaignError(ValueError):
    """Safe-to-display control-plane error."""


class CampaignIntegrityError(RuntimeError):
    """Retained campaign state is corrupt; no operation may proceed."""


class CampaignSpec(StrictModel):
    kind: Literal["launch", "lp", "market", "archive"]
    mode: Literal["observe", "shadow"]
    policy: Annotated[str, Field(min_length=2, max_length=65_536)]
    wallet: Address | None = None
    rpc_primary: Name | None = None
    rpc_secondary: Name | None = None
    input_source: Name | None = None
    cycles: Annotated[int, Field(ge=1, le=10_000)] = 100
    timeout_seconds: Annotated[int, Field(ge=10, le=86_400)] = 900
    discovery_from_block: Annotated[int, Field(ge=0, lt=2**64)] | None = None
    events: bool = False

    @model_validator(mode="after")
    def validate_kind(self) -> "CampaignSpec":
        if self.kind in {"market", "archive"}:
            if (
                self.mode != "observe"
                or not self.rpc_primary
                or not self.rpc_secondary
                or self.rpc_primary == self.rpc_secondary
                or self.wallet is not None
                or self.input_source is not None
                or self.events
                or self.discovery_from_block is not None
                or (self.kind == "archive" and self.cycles > 1000)
            ):
                raise ValueError(
                    "Market research requires two RPC profiles, observe mode and no wallet"
                )
        elif self.kind == "launch":
            if (
                self.mode != "observe"
                or not self.wallet
                or not self.rpc_primary
                or not self.rpc_secondary
            ):
                raise ValueError(
                    "Launch requires observation mode, public wallet and two RPC profiles"
                )
            if self.rpc_primary == self.rpc_secondary or self.input_source is not None:
                raise ValueError("distinct Launch RPC profiles and no LP input source required")
        elif (
            self.mode != "shadow"
            or not self.input_source
            or any(
                (
                    self.wallet,
                    self.rpc_primary,
                    self.rpc_secondary,
                    self.events,
                    self.discovery_from_block is not None,
                )
            )
        ):
            raise ValueError("LP requires shadow mode and an input source, without Launch fields")
        return self


def normalize_spec(document: str) -> CampaignSpec:
    spec = CampaignSpec.from_json(document)
    if spec.kind == "archive":
        policy = ArchiveConfig.from_json(spec.policy).model_dump(mode="json")
    elif spec.kind == "market":
        parsed = ObserverConfig.from_json(spec.policy)
        if parsed.market is None:
            raise ValueError("market research requires a market window configuration")
        policy = parsed.model_dump(mode="json")
    elif spec.kind == "launch":
        policy = ExecutionConfig.from_json(spec.policy).model_dump(mode="json", by_alias=True)
    else:
        policy = serializable(parse_policy(spec.policy.encode()))
    return CampaignSpec.model_validate(
        spec.model_dump()
        | {
            "policy": json.dumps(policy, indent=2, sort_keys=True, allow_nan=False) + "\n",
        }
    )


def _name(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise CampaignError("invalid campaign or RPC profile name")
    return value


def validate_rpc_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            0 < len(value) <= 4096
            and not any(c.isspace() for c in value)
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise CampaignError("invalid HTTP(S) RPC endpoint")
    return value


def _copy_inputs(source: ServerCampaignInput, folder: Path) -> dict[str, str]:
    """Copy a bounded input stream and a coherent SQLite backup off the API loop."""
    digest_parts: dict[str, str] = {}
    with source.input.open("rb") as incoming, (folder / "input.jsonl").open("xb") as outgoing:
        total = 0
        while chunk := incoming.read(64 * 1024):
            total += len(chunk)
            if total > MAX_ASSET_BYTES:
                raise CampaignError("LP input exceeds managed run size limit")
            outgoing.write(chunk)
        if total == 0:
            raise CampaignError("LP input stream is empty")
    deadline = time.monotonic() + 30
    with (
        closing(sqlite3.connect(source.evidence.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(folder / "evidence.db")) as dst,
    ):
        page_size = src.execute("PRAGMA page_size").fetchone()[0]

        def progress(status: int, remaining: int, total: int) -> None:
            if total * page_size > MAX_ASSET_BYTES or time.monotonic() > deadline:
                raise CampaignError("LP evidence backup exceeds size or time limit")

        src.backup(dst, pages=256, progress=progress)
    for name in ("input.jsonl", "evidence.db"):
        with (folder / name).open("rb") as stream:
            digest_parts[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest_parts


class CampaignManager:
    def __init__(
        self, config: ServerConfig, journal: ServerOpsJournal, environ: Mapping[str, str]
    ) -> None:
        self.root = (config.databases.ops.parent / "campaigns").resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_fd = os.open(self.root / "owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._lock_fd)
            raise ConfigurationError(
                "campaign controller or orphan worker already owns this store"
            ) from exc
        try:
            self._initialize(config, journal, environ)
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            os.close(self._lock_fd)
            raise

    def _initialize(
        self, config: ServerConfig, journal: ServerOpsJournal, environ: Mapping[str, str]
    ) -> None:
        self._journal = journal
        self._sources = {source.name: source for source in config.campaign_inputs}
        self._rpc: dict[str, tuple[str, str]] = {}
        for env_name in config.campaign_rpc_envs:
            value = environ.get(env_name)
            if value:
                name = env_name.lower().replace("_", "-")
                self._rpc[name] = (validate_rpc_url(value), "environment")
        self._running: dict[str, tuple[str, ShadowRunCoordinator]] = {}
        self._starting: set[str] = set()
        self._stop_preparing: set[str] = set()
        self._preparations: set[asyncio.Task[dict[str, str]]] = set()
        self._closed = False
        self._db = sqlite3.connect(self.root / "control.db")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS campaigns (
                name TEXT PRIMARY KEY, desired INTEGER NOT NULL, applied INTEGER);
            CREATE TABLE IF NOT EXISTS revisions (
                name TEXT NOT NULL, revision INTEGER NOT NULL, payload BLOB NOT NULL,
                digest TEXT NOT NULL, PRIMARY KEY(name, revision));
            CREATE TABLE IF NOT EXISTS campaign_runs (
                run_id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL,
                folder TEXT NOT NULL, manifest BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS start_requests (
                request_id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL,
                run_id TEXT);
        """)
        # The inherited lifetime lock proves no old managed process still owns
        # this store. Never automatically resume an interrupted campaign.
        for (run_id,) in self._db.execute("SELECT run_id FROM campaign_runs"):
            run = journal.get_run(run_id)
            if run is not None and run.status in ACTIVE:
                journal.mark_finished(
                    run_id,
                    exit_code=-1,
                    stdout="",
                    stderr="controller restarted; observation interrupted; explicit start required",
                )

    def rpc_profiles(self) -> list[dict[str, str]]:
        return [
            {"name": name, "storage": storage} for name, (_, storage) in sorted(self._rpc.items())
        ]

    def set_rpc(self, name: str, url: str, actor: str) -> None:
        _name(name)
        if len(self._rpc) >= 64 and name not in self._rpc:
            raise CampaignError("RPC profile limit reached")
        self._rpc[name] = (validate_rpc_url(url), "session")
        self._journal.record(
            action="set_campaign_rpc",
            actor=actor,
            params={"name": name},
            result="saved",
            detail="RPC endpoint available only in this controller session",
        )

    def _revision(self, name: str, revision: int) -> tuple[CampaignSpec, str]:
        try:
            return self._read_revision(name, revision)
        except CampaignError:
            raise
        except (ValueError, TypeError, KeyError, ArithmeticError, sqlite3.Error) as exc:
            raise CampaignIntegrityError("campaign configuration unavailable or corrupt") from exc

    def _read_revision(self, name: str, revision: int) -> tuple[CampaignSpec, str]:
        rows = self._db.execute(
            "SELECT revision, payload, digest FROM revisions "
            "WHERE name=? AND revision<=? ORDER BY revision",
            (name, revision),
        )
        previous = "0" * 64
        spec: CampaignSpec | None = None
        count = 0
        for count, (number, payload, digest) in enumerate(rows, start=1):
            value = json.loads(payload)
            if (
                number != count
                or value["name"] != name
                or value["revision"] != number
                or value["previous"] != previous
                or canonical_json(value) != payload
                or sha256_hex(payload) != digest
            ):
                raise CampaignIntegrityError("campaign configuration chain corrupted")
            spec = normalize_spec(value["document"])
            previous = digest
        if count != revision or spec is None:
            raise CampaignError("campaign revision unavailable")
        return spec, previous

    def head(self, name: str) -> dict[str, Any]:
        _name(name)
        row = self._db.execute(
            "SELECT desired, applied FROM campaigns WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise CampaignError("campaign not found")
        desired, applied = row
        spec, digest = self._revision(name, desired)
        applied_spec = self._revision(name, applied)[0] if applied is not None else spec
        last = self._db.execute(
            "SELECT run_id, revision FROM campaign_runs WHERE name=? ORDER BY rowid DESC LIMIT 1",
            (name,),
        ).fetchone()
        run = self._journal.get_run(last[0]) if last else None
        return {
            "name": name,
            "kind": spec.kind,
            "mode": spec.mode,
            "desired_revision": desired,
            "applied_revision": applied,
            "digest": digest,
            "document": json.dumps(spec.model_dump(mode="json"), indent=2),
            "status": "preparing" if name in self._starting else run.status if run else "idle",
            "run": None if run is None else asdict(run) | {"revision": last[1]},
            "configuration_plan": self.configuration_plan(applied_spec, applied or desired),
            "execution_eligible": False,
        }

    def configuration_plan(self, spec: CampaignSpec, revision: int) -> dict[str, Any]:
        plan: dict[str, Any] = {"revision": revision, "execution_eligible": False}
        if spec.kind in {"market", "archive", "launch"}:
            primary = self._rpc.get(str(spec.rpc_primary))
            secondary = self._rpc.get(str(spec.rpc_secondary))
            plan["distinct_rpc_profiles_configured"] = bool(
                primary and secondary and primary[0] != secondary[0]
            )
        if spec.kind == "archive":
            config = ArchiveConfig.from_json(spec.policy)
            minimum = config.requests_to_finish(0)
            plan.update(
                start_block=config.start_block,
                end_block=config.end_block,
                chunks_total=len(config.ranges),
                max_chunks_this_start=spec.cycles,
                minimum_total_requests_per_provider=minimum,
                configured_total_requests_per_provider=config.max_requests_per_provider,
                configured_budget_covers_range=config.max_requests_per_provider >= minimum,
                budget_scope="whole archive; failures spent; remaining budget checked by worker",
                resume_scope="same campaign and applied revision",
            )
        return plan

    def listing(self) -> dict[str, Any]:
        return {
            "items": [
                {key: value for key, value in self.head(name).items() if key != "document"}
                for (name,) in self._db.execute("SELECT name FROM campaigns ORDER BY name")
            ],
            "rpc_profiles": self.rpc_profiles(),
            "input_sources": sorted(self._sources),
            "max_active": 4,
            "supported_modes": {
                "launch": ["observe"],
                "lp": ["shadow"],
                "market": ["observe"],
                "archive": ["observe"],
            },
            "live_blockers": [
                "isolated_policy_signer_not_released",
                "target_release_gates_not_passed",
            ],
            "execution_eligible": False,
        }

    def save(self, name: str, document: str, expected: int, actor: str) -> dict[str, Any]:
        _name(name)
        spec = normalize_spec(document)
        if spec.kind == "lp" and spec.input_source not in self._sources:
            raise CampaignError("LP input source is not registered on this server")
        row = self._db.execute("SELECT desired FROM campaigns WHERE name=?", (name,)).fetchone()
        current = row[0] if row else 0
        if current != expected:
            raise CampaignError("configuration revision conflict; reload before saving")
        if current >= 10000:
            raise CampaignError("campaign revision limit reached")
        if not row and self._db.execute("SELECT count(*) FROM campaigns").fetchone()[0] >= 256:
            raise CampaignError("campaign limit reached")
        previous = self._revision(name, current)[1] if row else "0" * 64
        if row and self._revision(name, current)[0].kind != spec.kind:
            raise CampaignError("campaign kind cannot change; create a separate campaign")
        payload = canonical_json(
            {
                "name": name,
                "revision": current + 1,
                "previous": previous,
                "document": json.dumps(spec.model_dump(mode="json"), sort_keys=True),
                "actor": actor,
            }
        )
        with self._db:
            self._db.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?)",
                (name, current + 1, payload, sha256_hex(payload)),
            )
            self._db.execute(
                "INSERT INTO campaigns VALUES (?, ?, NULL) ON CONFLICT(name) "
                "DO UPDATE SET desired=excluded.desired",
                (name, current + 1),
            )
        self._journal.record(
            action="save_campaign",
            actor=actor,
            params={"name": name, "revision": current + 1},
            result="saved",
            detail="versioned configuration saved; apply required",
        )
        return self.head(name)

    def _busy(self, name: str) -> bool:
        owned = self._running.get(name)
        run = self._journal.get_run(owned[0]) if owned else None
        return name in self._starting or (run is not None and run.status in ACTIVE)

    def apply(self, name: str, expected: int, revision: int, actor: str) -> dict[str, Any]:
        head = self.head(name)
        if head["desired_revision"] != expected or self._busy(name):
            raise CampaignError("stop the campaign and reload its revision before applying")
        self._revision(name, revision)
        with self._db:
            self._db.execute("UPDATE campaigns SET applied=? WHERE name=?", (revision, name))
        self._journal.record(
            action="apply_campaign",
            actor=actor,
            params={"name": name, "revision": revision},
            result="applied",
            detail="configuration applied for next explicit start; no running policy changed",
        )
        return self.head(name)

    async def start(self, name: str, revision: int, actor: str, request_id: str) -> dict[str, Any]:
        head = self.head(name)
        prior_request = self._db.execute(
            "SELECT name, revision, run_id FROM start_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior_request:
            if prior_request[:2] != (name, revision):
                raise CampaignError("start request ID already belongs to another intent")
            if prior_request[2] is None:
                raise CampaignError(
                    "previous start is preparing or failed; inspect state before retry"
                )
            return head | {"requested_run_id": prior_request[2]}
        if self._closed or self._busy(name):
            raise CampaignError("campaign is busy or controller is closing")
        if head["applied_revision"] != revision:
            raise CampaignError("apply the requested revision before starting")
        active = sum(self._busy(item) for item in set(self._running) | self._starting)
        if active >= 4:
            raise CampaignError("managed campaign concurrency limit reached")
        spec, digest = self._revision(name, revision)
        env = {"PATH": os.defpath, "PYTHONUNBUFFERED": "1"}
        rpc_binding: dict[str, str] = {}
        if spec.kind in {"launch", "market", "archive"}:
            primary = self._rpc.get(str(spec.rpc_primary))
            secondary = self._rpc.get(str(spec.rpc_secondary))
            if primary is None or secondary is None or primary[0] == secondary[0]:
                raise CampaignError("two available distinct RPC endpoints required")
            env.update(BASE_RPC_PRIMARY=primary[0], BASE_RPC_SECONDARY=secondary[0])
            rpc_binding = {
                "primary": sha256_hex(primary[0].encode()),
                "secondary": sha256_hex(secondary[0].encode()),
            }
        self._starting.add(name)
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO start_requests VALUES (?, ?, ?, NULL)",
                    (request_id, name, revision),
                )
            folder = self.root / uuid.uuid4().hex
            folder.mkdir(mode=0o700)
            (folder / "policy.json").write_text(spec.policy)
            root = _REPOSITORY
            assets: dict[str, str] = {}
            command: tuple[str, ...]
            if spec.kind == "lp":
                source = self._sources.get(str(spec.input_source))
                if source is None:
                    raise CampaignError("LP input source is unavailable")
                task = asyncio.create_task(asyncio.to_thread(_copy_inputs, source, folder))
                self._preparations.add(task)
                try:
                    assets = await asyncio.shield(task)
                finally:
                    if task.done():
                        self._preparations.discard(task)
                command = (
                    sys.executable,
                    str(root / "scripts/run_lp_shadow.py"),
                    "--policy",
                    str(folder / "policy.json"),
                    "--input",
                    str(folder / "input.jsonl"),
                    "--evidence",
                    str(folder / "evidence.db"),
                    "--journal",
                    str(folder / "journal.db"),
                    "--cycles",
                    str(spec.cycles),
                )
            elif spec.kind == "archive":
                command = (
                    sys.executable,
                    "-m",
                    "scripts.capture_lp_market_archive",
                    "--config",
                    str(folder / "policy.json"),
                    "--output",
                    str(self.archive_path(name, revision)),
                    "--max-chunks",
                    str(spec.cycles),
                )
            elif spec.kind == "market":
                command = (
                    sys.executable,
                    "-m",
                    "scripts.run_lp_research",
                    "--config",
                    str(folder / "policy.json"),
                    "--output",
                    str(folder / "research"),
                )
            else:
                command = (
                    sys.executable,
                    str(root / "scripts/run_launch_monitor.py"),
                    "--dry-run",
                    "--config",
                    str(folder / "policy.json"),
                    "--journal",
                    str(folder / "journal.db"),
                    "--stop-file",
                    str(folder / "stop"),
                    "--env",
                    os.devnull,
                    "--wallet",
                    str(spec.wallet),
                )
                if spec.discovery_from_block is not None:
                    command += (
                        "--multi-venue",
                        "--discovery-from-block",
                        str(spec.discovery_from_block),
                    )
                if spec.events:
                    command += ("--events",)
            if self._closed or name in self._stop_preparing:
                raise CampaignError("start cancelled before process creation")
            prior = self._running.get(name)
            if prior:
                await prior[1].aclose()
                prior[1].close()
            coordinator = ShadowRunCoordinator(
                ServerShadowRunnerConfig(command, spec.timeout_seconds, 1),
                self._journal,
                cwd=root,
                loop_factory=asyncio.get_running_loop,
                environ=env,
                pass_fds=(self._lock_fd,),
            )
            run = coordinator.trigger()
            self._running[name] = (run.run_id, coordinator)
            manifest = canonical_json(
                {
                    "configuration_digest": digest,
                    "assets": assets,
                    "rpc_endpoint_hashes": rpc_binding,
                    "execution_eligible": False,
                }
            )
            try:
                with self._db:
                    self._db.execute(
                        "INSERT INTO campaign_runs VALUES (?, ?, ?, ?, ?)",
                        (run.run_id, name, revision, folder.name, manifest),
                    )
                    self._db.execute(
                        "UPDATE start_requests SET run_id=? WHERE request_id=?",
                        (run.run_id, request_id),
                    )
                self._journal.record(
                    action="start_campaign",
                    actor=actor,
                    params={"name": name, "revision": revision, "run_id": run.run_id},
                    result="queued",
                    detail="isolated observation run queued with immutable input bindings",
                )
            except BaseException:
                coordinator.stop(run.run_id)
                await coordinator.aclose()
                coordinator.close()
                self._running.pop(name, None)
                raise
            self._starting.discard(name)
            return self.head(name)
        finally:
            self._starting.discard(name)
            self._stop_preparing.discard(name)

    def stop(self, name: str, actor: str) -> dict[str, Any]:
        self.head(name)
        if name in self._starting:
            self._stop_preparing.add(name)
        owned = self._running.get(name)
        if owned:
            owned[1].stop(owned[0])
        self._journal.record(
            action="stop_campaign",
            actor=actor,
            params={"name": name},
            result="accepted",
            detail="managed observation stop; no on-chain unwind requested",
        )
        return self.head(name)

    def archive_path(self, name: str, revision: int) -> Path:
        _name(name)
        if type(revision) is not int or not 1 <= revision <= 10000:
            raise CampaignError("invalid archive revision")
        return self.root / "archives" / name / str(revision)

    def journal_path(self, name: str) -> tuple[str, Path | None]:
        head = self.head(name)
        run = head["run"]
        if not run:
            return head["kind"], None
        row = self._db.execute(
            "SELECT folder FROM campaign_runs WHERE run_id=?", (run["run_id"],)
        ).fetchone()
        folder = row[0]
        if not re.fullmatch(r"[0-9a-f]{32}", folder):
            raise CampaignError("invalid retained run path")
        if head["kind"] == "archive":
            return "archive", self.archive_path(name, run["revision"]) / "status.json"
        return head["kind"], self.root / folder / (
            "research/status.json" if head["kind"] == "market" else "journal.db"
        )

    async def aclose(self) -> None:
        self._closed = True
        await asyncio.gather(*tuple(self._preparations), return_exceptions=True)
        self._preparations.clear()
        for _, coordinator in self._running.values():
            await coordinator.aclose()
            coordinator.close()
        self._db.close()
        os.close(self._lock_fd)
