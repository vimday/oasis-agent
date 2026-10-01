"""Bounded authenticated discovery/enrichment; never signs transactions or buys tokens."""

import argparse
import base64
import hashlib
import hmac
import os
import re
import selectors
import signal
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import certifi

from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.launch.discovery import DiscoveryPolicy, DiscoveryRequest, discover
from oasis_agent.launch.discovery_import import import_ranking_discovery
from oasis_agent.launch.provider_normalization import normalize
from oasis_agent.launch.provider_observation import FIELDS, observation, response_object
from oasis_agent.launch.trend_import import GMGN_EVM_CHAINS
from oasis_agent.models.common import canonical_json, sha256_hex

MAX_BYTES = 5_000_000
OKX_ROUTES = {
    "pools": "/api/v6/dex/market/token/top-liquidity",
    "trending": "/api/v6/dex/market/token/hot-token",
    "advanced": "/api/v6/dex/market/token/advanced-info",
    "holders": "/api/v6/dex/market/token/holder",
    "clusters": "/api/v6/dex/market/token/cluster/overview",
}


class QueryFailure(ValueError):
    """Fixed diagnostic category, never external exception content."""

    def __init__(self, category: str):
        super().__init__("query failed")
        self.category = category


def credentials(path: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Load only query credential names; never export the project's wallet environment."""
    result = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*", line)
        if match and match[1] in names:
            key, value = match.groups()
            if key in result:
                raise ValueError("duplicate query credential")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if not value or any(c in value for c in "\r\n") or "在这里" in value:
                raise ValueError("missing query credential")
            result[key] = value
    if set(result) != set(names):
        raise ValueError("query credentials incomplete")
    return result


def cli(args: list[str]) -> bytes:
    """Drain bounded stdout/stderr together; kill the process group on timeout/overflow."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    with (
        tempfile.TemporaryDirectory(prefix="oasis-query-") as directory,
        subprocess.Popen(
            ["gmgn-cli", *args],
            cwd=directory,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as process,
    ):
        chunks = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + 35
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise QueryFailure("timeout")
                    for key, _ in selector.select(timeout=0.2):
                        raw = os.read(key.fd, 65536)
                        if not raw:
                            selector.unregister(key.fileobj)
                        else:
                            chunks[key.data].extend(raw)
                        if sum(map(len, chunks.values())) > MAX_BYTES:
                            raise QueryFailure("response_too_large")
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode or chunks["stderr"]:
                # Do not silently ignore provider warnings, including neutralized metadata.
                text = bytes(chunks["stderr"]).lower()
                category = "cli_exit" if process.returncode else "cli_warning"
                if b"429" in text or b"rate_limit" in text:
                    category = "rate_limit"
                elif b"401" in text or b"403" in text:
                    category = "access_denied"
                elif b"neutralized" in text:
                    category = "suspicious_metadata"
                raise QueryFailure(category)
            return bytes(chunks["stdout"])
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    process.kill()
            process.wait(timeout=5)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ValueError("authenticated redirects forbidden")


def okx_query(kind: str, chain: int, token: str | None, limit: int, creds: dict[str, str]):
    params = {"chainIndex": str(chain)}
    if kind == "trending":
        params.update(rankingType="4", rankingTimeFrame="1", limit=str(limit))
    else:
        params["tokenContractAddress"] = token
    path = OKX_ROUTES[kind] + "?" + urllib.parse.urlencode(params)
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    signature = base64.b64encode(
        hmac.new(
            creds["OKX_SECRET_KEY"].encode(), (timestamp + "GET" + path).encode(), hashlib.sha256
        ).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": creds["OKX_API_KEY"],
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-PASSPHRASE": creds["OKX_PASSPHRASE"],
        "OK-ACCESS-TIMESTAMP": timestamp,
        "Accept": "application/json",
        "User-Agent": "OasisAgent/0.1",
    }
    opener = urllib.request.build_opener(
        NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
    )
    with opener.open(
        urllib.request.Request("https://web3.okx.com" + path, headers=headers), timeout=20
    ) as response:
        raw = response.read(MAX_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_BYTES:
            raise ValueError("invalid bounded provider response")
    return raw


def run(output: Path, chains: list[int], limit: int, enrich: int, env_file: Path) -> None:
    if (
        not 1 <= limit <= 25
        or not 0 <= enrich <= 5
        or not 1 <= len(chains) <= 2
        or len(set(chains)) != len(chains)
        or any(c not in (8453, 4663) for c in chains)
    ):
        raise ValueError("unsupported research scope")
    okx = credentials(env_file, ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"))
    gmgn = credentials(Path.home() / ".config/gmgn/.env", ("GMGN_API_KEY",))
    cli(["config", "--check"])
    output.mkdir(parents=True, exist_ok=False)
    secrets = [v.encode() for v in (*okx.values(), *gmgn.values())]
    records, signals, claims, halted = [], [], [], set()

    def capture(provider, kind, chain, token=None):
        record = {
            "provider": provider,
            "kind": kind,
            "chain_id": chain,
            "token": token,
            "started_at": int(time.time()),
            "source_event_time": None,
            "execution_eligible": False,
        }
        if provider in halted:
            record.update(status="skipped_provider_halted", retrieved_at=int(time.time()))
            records.append(record)
            return None
        try:
            time.sleep(1.1)  # Conservative pacing, including weight-5 holder routes.
            if provider == "gmgn":
                args = (
                    ["market", "trending", "--interval", "5m", "--limit", str(limit)]
                    if kind == "trending"
                    else ["token", kind, "--address", token]
                )
                if kind == "holders":
                    args.extend(["--limit", "20"])
                args.extend(["--chain", GMGN_EVM_CHAINS[chain], "--raw"])
                raw = cli(args)
            else:
                raw = okx_query(kind, chain, token, limit, okx)
            if any(secret in raw for secret in secrets) or b"PRIVATE KEY-----" in raw:
                raise ValueError("sensitive response rejected")
            root = response_object(raw, cli_unwrapped=provider == "gmgn" and kind != "trending")
            digest = sha256_hex(raw)
            raw_path = output / (digest + ".raw.json")
            if not raw_path.exists():
                raw_path.write_bytes(raw)
            record.update(
                status="captured",
                retrieved_at=int(time.time()),
                raw_sha256=digest,
                encoding="gmgn_cli_stdout" if provider == "gmgn" else "http_body",
                cli_unwrapped=provider == "gmgn" and kind != "trending",
            )
            records.append(record)
            return raw, root, record["retrieved_at"]
        except Exception as exc:  # noqa: BLE001 - never expose provider/credential errors
            record.update(
                status="failed",
                retrieved_at=int(time.time()),
                error_type=type(exc).__name__,
                error_category=exc.category
                if isinstance(exc, QueryFailure)
                else "response_or_transport",
                http_status=exc.code if isinstance(exc, urllib.error.HTTPError) else None,
            )
            records.append(record)
            halted.add(provider)  # No repeated probes after any provider failure this run.
            return None
        finally:
            (output / "captures.json").write_bytes(canonical_json(records))

    for chain in chains:
        for provider in ("gmgn", "okx"):
            captured = capture(provider, "trending", chain)
            if captured is None:
                continue
            raw, root, retrieved = captured
            try:
                rows = root["data"].get("rank") if provider == "gmgn" else root["data"]
                if rows == []:
                    continue
                imported = import_ranking_discovery(
                    raw,
                    provider=provider,
                    chain_id=chain,
                    window_seconds=300,
                    observed_at=retrieved,
                    retrieved_at=retrieved,
                )
                parsed = [
                    normalize(
                        raw,
                        provider=provider,
                        kind="trending",
                        chain_id=chain,
                        token=s.token,
                        retrieved_at=retrieved,
                        row=row,
                    )
                    for s, row in zip(imported, rows, strict=True)
                ]
                signals.extend(imported)
                claims.extend(x.model_dump(mode="json") for x in parsed)
            except Exception:  # noqa: BLE001 - retained bytes remain available for schema repair
                records[-1]["normalization"] = "failed"
    request = DiscoveryRequest(
        evaluated_at=int(time.time()),
        policy=DiscoveryPolicy(chains=tuple(chains)),
        signals=tuple(signals),
    )
    result = discover(request)
    selected = result.items[:enrich]
    for item in selected:
        for provider, kinds in (
            ("gmgn", ("info", "security", "pool", "holders")),
            ("okx", ("advanced", "holders", "clusters")),
        ):
            for kind in kinds:
                captured = capture(provider, kind, item.chain_id, item.token)
                if captured is None or ((provider, kind) not in FIELDS and kind != "holders"):
                    continue
                raw, _, retrieved = captured
                try:
                    claims.append(
                        normalize(
                            raw,
                            provider=provider,
                            kind=kind,
                            chain_id=item.chain_id,
                            token=item.token,
                            retrieved_at=retrieved,
                            cli_unwrapped=provider == "gmgn",
                        ).model_dump(mode="json")
                    )
                except Exception:  # noqa: BLE001 - hostile detail errors stay sanitized
                    records[-1]["normalization"] = "failed"
    payload = {
        "schema_version": "oasis.provider-research.v2",
        "finished_at": int(time.time()),
        "request": request.model_dump(mode="json"),
        "discovery": result.model_dump(mode="json"),
        "selected_for_enrichment": [x.model_dump(mode="json") for x in selected],
        "provider_observations": claims,
        "captures": records,
        "gmgn_cli_version": cli(["--version"]).decode().strip(),
        "scope": {
            "chains": chains,
            "limit": limit,
            "enrich": enrich,
            "ranking_window_seconds": 300,
            "provider_default_filters": True,
        },
        "field_units": {f"{p}/{k}": v for (p, k), v in FIELDS.items()},
        "fomo": "authorization_required",
        "execution_eligible": False,
        "limitations": [
            "provider_claims_not_chain_attestations",
            "source_event_time_unknown",
            "holder_lists_not_complete_census",
            "no_profitability_or_execution_gate",
        ],
    }
    (output / "captures.json").write_bytes(canonical_json(records))
    (output / "request.json").write_bytes(canonical_json(request.model_dump(mode="json")))
    (output / "result.json").write_bytes(canonical_json(payload))
    print(
        f"Retained {len(records)} request records; {len(result.items)} discovery candidates; "
        f"{len(claims)} provider observations; execution disabled."
    )


def replay(directory: Path) -> dict[str, object]:
    """Verify hashes and recompute imported claims/discovery without credentials or network."""
    path = directory / "result.json"
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("oversized research result")
    payload = strict_json_object(path.read_bytes())
    if payload.get("schema_version") not in (
        "oasis.provider-research.v1",
        "oasis.provider-research.v2",
    ):
        raise ValueError("unknown research schema")
    decoder = normalize if payload["schema_version"].endswith(".v2") else observation
    request = DiscoveryRequest.from_json(canonical_json(payload["request"]))
    if canonical_json(discover(request).model_dump(mode="json")) != canonical_json(
        payload["discovery"]
    ):
        raise ValueError("discovery replay mismatch")
    records = payload["captures"]
    if not isinstance(records, list) or len(records) > 40:
        raise ValueError("invalid capture records")
    scope = payload["scope"]
    chains, limit, enrich = scope["chains"], scope["limit"], scope["enrich"]
    if (
        not isinstance(chains, list)
        or not 1 <= len(chains) <= 2
        or any(type(c) is not int or c not in (8453, 4663) for c in chains)
        or len(set(chains)) != len(chains)
        or tuple(chains) != request.policy.chains
        or type(limit) is not int
        or not 1 <= limit <= 25
        or type(enrich) is not int
        or not 0 <= enrich <= 5
        or scope["ranking_window_seconds"] != 300
        or scope["provider_default_filters"] is not True
    ):
        raise ValueError("invalid capture scope")
    selected = discover(request).items[:enrich]
    if canonical_json([x.model_dump(mode="json") for x in selected]) != canonical_json(
        payload["selected_for_enrichment"]
    ):
        raise ValueError("enrichment selection mismatch")
    if payload["field_units"] != {f"{p}/{k}": v for (p, k), v in FIELDS.items()}:
        raise ValueError("field units mismatch")
    expected = [(p, "trending", c, None) for c in chains for p in ("gmgn", "okx")]
    for item in selected:
        expected.extend(
            (p, k, item.chain_id, item.token)
            for p, kinds in (
                ("gmgn", ("info", "security", "pool", "holders")),
                ("okx", ("advanced", "holders", "clusters")),
            )
            for k in kinds
        )
    if [(r["provider"], r["kind"], r["chain_id"], r["token"]) for r in records] != expected:
        raise ValueError("capture scope coverage mismatch")
    claims, signals, halted = [], [], set()
    for record in records:
        provider = record["provider"]
        if (
            record.get("execution_eligible") is not False
            or record.get("source_event_time") is not None
        ):
            raise ValueError("invalid observation authority")
        if (record["status"] == "skipped_provider_halted") != (provider in halted):
            raise ValueError("provider halt order mismatch")
        if record["status"] == "failed":
            halted.add(provider)
        if record["status"] not in ("captured", "failed", "skipped_provider_halted"):
            raise ValueError("unknown capture status")
        if record["status"] != "captured":
            continue
        digest = record["raw_sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid response identity")
        raw_path = directory / (digest + ".raw.json")
        if raw_path.stat().st_size > MAX_BYTES:
            raise ValueError("oversized retained response")
        raw = raw_path.read_bytes()
        if sha256_hex(raw) != digest:
            raise ValueError("retained response hash mismatch")
        provider, kind, chain = record["provider"], record["kind"], record["chain_id"]
        retrieved = record["retrieved_at"]
        if not record["started_at"] <= retrieved <= payload["finished_at"]:
            raise ValueError("invalid capture time")
        if record.get("normalization") == "failed":
            raise ValueError("incomplete normalization requires repair before replay")
        if kind == "trending":
            root = response_object(raw)
            rows = root["data"].get("rank") if provider == "gmgn" else root["data"]
            if rows == []:
                continue
            imported = import_ranking_discovery(
                raw,
                provider=provider,
                chain_id=chain,
                window_seconds=300,
                observed_at=retrieved,
                retrieved_at=retrieved,
            )
            signals.extend(imported)
            claims.extend(
                decoder(
                    raw,
                    provider=provider,
                    kind=kind,
                    chain_id=chain,
                    token=s.token,
                    retrieved_at=retrieved,
                    row=row,
                ).model_dump(mode="json")
                for s, row in zip(imported, rows, strict=True)
            )
        elif (provider, kind) in FIELDS or (decoder is normalize and kind == "holders"):
            claims.append(
                decoder(
                    raw,
                    provider=provider,
                    kind=kind,
                    chain_id=chain,
                    token=record["token"],
                    retrieved_at=retrieved,
                    cli_unwrapped=provider == "gmgn",
                ).model_dump(mode="json")
            )
    if tuple(signals) != request.signals:
        raise ValueError("imported discovery input mismatch")
    if canonical_json(claims) != canonical_json(payload["provider_observations"]):
        raise ValueError("provider claim replay mismatch")
    if payload.get("execution_eligible") is not False:
        raise ValueError("research cannot enable execution")
    return {
        "result_sha256": sha256_hex(path.read_bytes()),
        "replayed_observations": len(claims),
        "replayed_signals": len(signals),
        "execution_eligible": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--replay", type=Path, help="offline response/claim/discovery verification")
    parser.add_argument("--chains", type=int, nargs="+", default=[8453])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--enrich", type=int, default=2)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        if args.replay:
            print(canonical_json(replay(args.replay)).decode())
        else:
            run(args.output, args.chains, args.limit, args.enrich, args.env_file)
    except Exception:  # noqa: BLE001 - no secrets or attacker-controlled errors
        parser.exit(1, "Research capture failed; review sanitized records if present.\n")


if __name__ == "__main__":
    main()
