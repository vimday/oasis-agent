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


def docker(*args: str) -> str:
    result = subprocess.run(  # nosec B603 B607 - fixed Docker CLI; no shell or credentials
        ["docker", *args], check=True, capture_output=True, text=True, timeout=60
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
    image_id = docker("image", "inspect", image, "--format", "{{.Id}}")
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError("immutable local image identity required")
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
            "schema": "oasis.live-isolation-drill.v1",
            "image": image_id,
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
