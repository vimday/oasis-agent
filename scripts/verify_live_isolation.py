"""Local Docker confinement drill with synthetic markers, no real key or network.

Creates temporary named volumes, tests separate private state and secret mounts,
then removes only those generated resources. Never starts a trading service.
"""

import argparse
import json
import subprocess
import uuid
from pathlib import Path

from oasis_agent.models.common import canonical_json, sha256_hex

BASE_IMAGE = (
    "python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928"
)
BASE_REPO_DIGEST = "python@" + BASE_IMAGE.split("@", 1)[1]
RUNTIME_COMMAND = ["python", "-m", "scripts.run_live_lp", "--mode", "live", "--readiness"]


def environment(values: object) -> dict[str, str]:
    if not isinstance(values, list) or any(not isinstance(v, str) or "=" not in v for v in values):
        raise ValueError("image environment malformed")
    result = {}
    for value in values:
        key, content = value.split("=", 1)
        if not key or key in result:
            raise ValueError("image environment ambiguous")
        result[key] = content
    return result


def runtime_contract(runtime: dict, base: dict) -> dict:
    """Validate image defaults before starting even a synthetic confinement drill.

    The daemon and digest-resolved base image are trusted. This is a configuration
    check, not proof of application source identity or installed dependency bytes.
    """
    config = runtime["Config"]
    expected_env = environment(base["Config"]["Env"]) | {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": "/app/src:/app",
    }
    if (
        config.get("Entrypoint") not in (None, [])
        or config.get("Cmd") != RUNTIME_COMMAND
        or config.get("User") != "10001:10001"
        or config.get("WorkingDir") != "/app"
        or config.get("Volumes") not in (None, {})
        or config.get("Healthcheck") is not None
        or environment(config.get("Env")) != expected_env
    ):
        raise ValueError("runtime image configuration differs from deployment contract")
    layers, base_layers = runtime["RootFS"]["Layers"], base["RootFS"]["Layers"]
    if (
        runtime["RootFS"].get("Type") != "layers"
        or base["RootFS"].get("Type") != "layers"
        or not isinstance(layers, list)
        or not isinstance(base_layers, list)
        or not base_layers
        or layers[: len(base_layers)] != base_layers
    ):
        raise ValueError("runtime image does not descend from pinned base layers")
    for identity in (runtime.get("Id"), base.get("Id")):
        if (
            not isinstance(identity, str)
            or not identity.startswith("sha256:")
            or len(identity) != 71
        ):
            raise ValueError("immutable image identity required")
        int(identity[7:], 16)
    return {
        "image": runtime["Id"],
        "base_image": BASE_IMAGE,
        "base_id": base["Id"],
        "configuration_hash": sha256_hex(canonical_json(config)),
        "source_identity_verified": False,
    }


def inspect_image(image: str, *, env: dict[str, str] | None = None) -> dict:
    data = json.loads(docker("image", "inspect", image, env=env))
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("one local image required")
    return data[0]


def inspect_base(*, env: dict[str, str] | None = None) -> dict:
    try:
        base = inspect_image(BASE_IMAGE, env=env)
    except subprocess.CalledProcessError:
        # Some local stores list tags/digests but resolve only image IDs. The
        # tag is a locator only: the inspected object's RepoDigests must still
        # contain the exact pinned digest. Never pull or accept a tag alone.
        identities = set(
            docker(
                "image",
                "ls",
                "--quiet",
                "--no-trunc",
                "--filter",
                "reference=python:3.15-slim",
                env=env,
            ).splitlines()
        )
        if len(identities) != 1:
            raise ValueError("one locally retained pinned base required") from None
        base = inspect_image(identities.pop(), env=env)
    if BASE_REPO_DIGEST not in base.get("RepoDigests", []):
        raise ValueError("local base repository digest differs from pin")
    return base


def docker(*args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(  # nosec B603 B607 - fixed Docker CLI; no shell or credentials
        ["docker", *args], env=env, check=True, capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip()


COMMON = [
    "--rm",
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges:true",
    "--pids-limit=128",
    "--memory=512m",
    "--user=10001:10001",
    "--tmpfs=/tmp:rw,noexec,nosuid,size=16m,mode=1777",
]

CHECK = """
import json, os
from pathlib import Path
assert os.getuid() == 10001
status=Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in status and 'NoNewPrivs:\\t1' in status
for forbidden in ['/app/.env','/var/run/docker.sock','/authority-state','/strategy-state']:
    assert not Path(forbidden).exists(), forbidden
try:
    Path('/app/forbidden-write').write_text('x')
except OSError:
    pass
else:
    raise AssertionError('root filesystem is writable')
assert Path('/state').stat().st_mode & 0o777 == 0o700
assert Path('/run/oasis').stat().st_mode & 0o777 == 0o700
"""


def verify(image: str, output: Path) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        raise ValueError("output exists; preserve previous isolation evidence")
    contract = runtime_contract(inspect_image(image), inspect_base())
    image_id = contract["image"]
    prefix = "oasis-isolation-" + uuid.uuid4().hex
    names = {role: prefix + "-" + role for role in ("authority", "strategy", "socket", "keys")}
    created = []
    try:
        for name in names.values():
            docker("volume", "create", name)
            created.append(name)
        mounts = [
            part
            for role, name in names.items()
            for part in ("--mount", f"type=volume,src={name},dst=/{role}")
        ]
        init = """
import os
from pathlib import Path
for p in ['/authority','/strategy','/socket','/keys']:
    os.chown(p,10001,10001); os.chmod(p,0o700)
p=Path('/keys/canary.key'); p.write_text('SYNTHETIC_MARKER_NOT_A_SIGNING_KEY')
os.chmod(p,0o600); os.chown(p,10001,10001)
Path('/authority/private-journal-marker').write_text('authority-only')
"""
        docker(
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--cap-add=FOWNER",
            "--cap-add=DAC_OVERRIDE",
            "--security-opt=no-new-privileges:true",
            "--user=0:0",
            *mounts,
            image_id,
            "python",
            "-c",
            init,
        )
        common_mount = ["--mount", f"type=volume,src={names['socket']},dst=/run/oasis"]
        authority = (
            CHECK
            + """
assert Path('/secrets/canary.key').read_text() == 'SYNTHETIC_MARKER_NOT_A_SIGNING_KEY'
assert Path('/secrets/canary.key').stat().st_mode & 0o777 == 0o600
assert Path('/secrets/canary.key').stat().st_uid == 10001
try:
    Path('/secrets/canary.key').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('secret volume is writable')
assert Path('/state/private-journal-marker').exists()
print('authority-confined')
"""
        )
        strategy = (
            CHECK
            + """
assert not Path('/secrets').exists()
assert not Path('/state/private-journal-marker').exists()
print('strategy-confined')
"""
        )
        authority_result = docker(
            "run",
            *COMMON,
            *common_mount,
            "--mount",
            f"type=volume,src={names['authority']},dst=/state",
            "--mount",
            f"type=volume,src={names['keys']},dst=/secrets,readonly",
            image_id,
            "python",
            "-c",
            authority,
        )
        strategy_result = docker(
            "run",
            *COMMON,
            *common_mount,
            "--mount",
            f"type=volume,src={names['strategy']},dst=/state",
            image_id,
            "python",
            "-c",
            strategy,
        )
        report = {
            "schema": "oasis.live-isolation-drill.v2",
            "image": image_id,
            "runtime_contract": contract,
            "verifier_hash": sha256_hex(Path(__file__).read_bytes()),
            "authority": authority_result,
            "strategy": strategy_result,
            "network": "none",
            "synthetic_markers_only": True,
            "execution_eligible": False,
            "limitation": "confinement only; key provisioning and RPC acceptance remain separate",
        }
        report["evidence_hash"] = sha256_hex(canonical_json(report))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x") as handle:
            json.dump(report, handle, indent=2)
        return report
    finally:
        for name in reversed(created):
            docker("volume", "rm", name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="oasis-live:local")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.image, args.out), indent=2))


if __name__ == "__main__":
    main()
