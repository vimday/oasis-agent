"""Read-only OKX onchainos CLI adapter for live opportunity discovery.

Only an allowlist of read-only subcommands can be built. Wallet, swap execution,
strategy (limit orders), payment and any other state-changing command is refused
before a process starts. Responses are provider claims, not chain attestations.
"""

import json
import os
import random
import selectors
import signal
import subprocess  # nosec B404 - fixed argv, no shell, allowlisted read-only commands
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.common import normalize_address, sha256_hex

MAX_BYTES = 4_000_000
TIMEOUT_SECONDS = 30.0
READ_ONLY = frozenset(
    {
        ("token", "hot-tokens"),
        ("token", "price-info"),
        ("token", "advanced-info"),
        ("token", "liquidity"),
        ("signal", "list"),
        ("security", "token-scan"),
        ("swap", "quote"),
        ("market", "prices"),
    }
)
CHAINS = {8453: "base"}
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


class SourceFailure(ValueError):
    """Fixed diagnostic category; never carries provider or process output."""

    def __init__(self, category: str):
        super().__init__(f"onchainos query failed: {category}")
        self.category = category


@dataclass(frozen=True, slots=True)
class Capture:
    command: tuple[str, ...]
    retrieved_at: int
    raw_sha256: str
    data: Any


Runner = Callable[[Sequence[str]], bytes]


def default_binary() -> Path:
    return Path.home() / ".local/bin/onchainos"


def run_cli(
    binary: Path, *, interpreter_dir: Path | None = None, timeout: float = TIMEOUT_SECONDS
) -> Runner:
    """Bounded subprocess runner with a minimal environment and process-group kill.

    `interpreter_dir` is prepended to PATH for script CLIs (e.g. a node shebang). No
    wallet, RPC or project environment variable is forwarded to the child.
    """

    def run(argv: Sequence[str]) -> bytes:
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
        if interpreter_dir is not None:
            env["PATH"] = f"{interpreter_dir}{os.pathsep}{env.get('PATH', '')}"
        with (
            tempfile.TemporaryDirectory(prefix="oasis-onchainos-") as directory,
            subprocess.Popen(  # nosec B603 - allowlisted argv validated by build_argv
                [str(binary), *argv],
                cwd=directory,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            ) as process,
        ):
            out, size = bytearray(), 0
            deadline = time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    assert process.stdout is not None and process.stderr is not None  # nosec B101
                    selector.register(process.stdout, selectors.EVENT_READ, True)
                    selector.register(process.stderr, selectors.EVENT_READ, False)
                    while selector.get_map():
                        if time.monotonic() >= deadline:
                            raise SourceFailure("timeout")
                        for key, _ in selector.select(timeout=0.2):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            size += len(chunk)
                            if key.data:
                                out.extend(chunk)
                            if size > MAX_BYTES:
                                raise SourceFailure("response_too_large")
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
                if process.returncode:
                    raise SourceFailure("cli_exit")
                return bytes(out)
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                process.wait(timeout=5)

    return run


def build_argv(group: str, command: str, chain: int, **options: str) -> tuple[str, ...]:
    if (group, command) not in READ_ONLY:
        raise SourceFailure("command_not_read_only")
    if chain not in CHAINS:
        raise SourceFailure("chain_not_supported")
    argv = [group, command]
    if (group, command) not in (("security", "token-scan"), ("market", "prices")):
        argv += ["--chain", CHAINS[chain]]
    for key, value in sorted(options.items()):
        limit = 2_200 if key == "tokens" else 100
        if not key.replace("_", "").isalnum() or not value or len(value) > limit:
            raise SourceFailure("invalid_option")
        if any(c in value for c in " \t\r\n;|&$`'\"\\") or value.startswith("-"):
            raise SourceFailure("invalid_option")
        argv += ["--" + key.replace("_", "-"), value]
    return tuple(argv)


class OnchainosSource:
    """Typed read-only queries; every response is hashed and validated as `{ok, data}`."""

    def __init__(
        self,
        runner: Runner,
        clock: Callable[[], float] = time.time,
        retain: Path | None = None,
    ):
        self._run = runner
        self._clock = clock
        self._retain = retain
        self.calls = 0

    def _query(self, group: str, command: str, chain: int, **options: str) -> Capture:
        argv = build_argv(group, command, chain, **options)
        self.calls += 1
        raw = self._run(argv)
        digest = sha256_hex(raw)
        if self._retain is not None:
            # Raw bytes are stored apart from interpretation, content-addressed and write-once.
            path = self._retain / (digest + ".json")
            if not path.exists():
                path.write_bytes(raw)
        try:
            root = json.loads(raw, parse_float=str, parse_int=str)
        except (ValueError, RecursionError) as error:
            raise SourceFailure("invalid_json") from error
        if not isinstance(root, dict) or root.get("ok") is not True or "data" not in root:
            raise SourceFailure("provider_error")
        return Capture(argv, int(self._clock()), digest, root["data"])

    def hot_tokens(self, chain: int) -> Capture:
        return self._query("token", "hot-tokens", chain)

    def signals(self, chain: int) -> Capture:
        return self._query("signal", "list", chain)

    def price_info(self, chain: int, token: str) -> Capture:
        return self._query("token", "price-info", chain, address=normalize_address(token))

    def advanced_info(self, chain: int, token: str) -> Capture:
        return self._query("token", "advanced-info", chain, address=normalize_address(token))

    def security(self, chain: int, token: str) -> Capture:
        return self._query(
            "security", "token-scan", chain, tokens=f"{chain}:{normalize_address(token)}"
        )

    def quote(self, chain: int, src: str, dst: str, amount: int) -> Capture:
        if not 0 < amount < 2**256:
            raise SourceFailure("invalid_amount")
        return self._query(
            "swap",
            "quote",
            chain,
            **{
                "from": normalize_address(src),
                "to": normalize_address(dst),
                "amount": str(amount),
            },
        )

    def prices(self, chain: int, tokens: Sequence[str]) -> Capture:
        """Batch spot price claims for at most 40 tokens in one request."""
        unique = sorted({normalize_address(t) for t in tokens})
        if not 0 < len(unique) <= 40:
            raise SourceFailure("invalid_token_batch")
        return self._query(
            "market", "prices", chain, tokens=",".join(f"{chain}:{t}" for t in unique)
        )


RETRYABLE = frozenset({"timeout", "cli_exit"})


def paced(
    runner: Runner,
    *,
    min_interval: float = 1.1,
    attempts: int = 3,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    jitter: Callable[[], float] | None = None,
) -> Runner:
    """Space calls at least `min_interval` apart and retry transient failures.

    Only read-only, idempotent queries pass through here, so a retry cannot duplicate
    an effect. Backoff is exponential with jitter and bounded by `attempts`; a
    non-transient category (oversized/invalid response) is never retried.
    """
    draw = jitter or random.random  # nosec B311 - scheduling jitter, not cryptography
    last = [-1e18]

    def run(argv: Sequence[str]) -> bytes:
        for attempt in range(attempts):
            wait = last[0] + min_interval - monotonic()
            if wait > 0:
                sleep(wait)
            last[0] = monotonic()
            try:
                return runner(argv)
            except SourceFailure as error:
                if error.category not in RETRYABLE or attempt == attempts - 1:
                    raise
                sleep(base_delay * 2**attempt * (1 + draw()))
        raise SourceFailure("retries_exhausted")  # pragma: no cover - loop always returns/raises

    return run
