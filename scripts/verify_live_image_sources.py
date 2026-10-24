"""Compare a stopped runtime image's /app with tracked local runtime files.

No container process, network, production volume or credential is used. This
checks application bytes only, not installed packages or production readiness.
"""

import argparse
import json
import subprocess
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from tempfile import TemporaryFile
from typing import BinaryIO

from oasis_agent.models.common import canonical_json, sha256_hex
from scripts.verify_live_isolation import docker, inspect_base, inspect_image, runtime_contract

ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    "src",
    "scripts",
    "requirements.lock",
    "configs/live.base-eth-usdc.json",
    "configs/live.rpc.canary.json",
)
MAX_BYTES = 32_000_000


def source_manifest(root: Path = ROOT) -> dict[str, str]:
    raw = subprocess.run(  # nosec B603 B607 - fixed read-only git inventory
        ["git", "ls-files", "-z", "--", *INPUTS],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=15,
    ).stdout
    names = raw.decode().split("\0")[:-1]
    if not names or len(names) != len(set(names)) or len(names) > 4096:
        raise ValueError("bounded tracked runtime inventory required")
    result = {}
    total = 0
    for name in names:
        path = root / name
        if path.resolve() != path.absolute() or not path.is_file():
            raise ValueError("tracked runtime input must be a regular file")
        size = path.stat().st_size
        total += size
        if size > 2_000_000 or total > MAX_BYTES:
            raise ValueError("tracked runtime input exceeds size limit")
        result[name] = sha256_hex(path.read_bytes())
    if not set(INPUTS[2:]) <= set(result):
        raise ValueError("runtime lockfile or configuration missing")
    return result


def verify_archive(stream: BinaryIO, expected: dict[str, str]) -> dict[str, str]:
    allowed_dirs = {"."}
    for name in expected:
        allowed_dirs.update(str(p) for p in PurePosixPath(name).parents)
    seen: set[str] = set()
    observed = {}
    total = 0
    with tarfile.open(fileobj=stream, mode="r:") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            name = str(path)
            if path.is_absolute() or ".." in path.parts or name in seen or len(seen) >= 4096:
                raise ValueError("ambiguous image archive path")
            seen.add(name)
            if member.isdir() and name in allowed_dirs:
                continue
            if not member.isfile() or name not in expected or member.size > 2_000_000:
                raise ValueError("unexpected image file, type or size")
            total += member.size
            if total > MAX_BYTES:
                raise ValueError("image application exceeds size limit")
            data = archive.extractfile(member)
            if data is None:
                raise ValueError("image file bytes missing")
            observed[name] = sha256_hex(data.read())
    if observed != expected:
        raise ValueError("image application differs from tracked runtime sources")
    return observed


def verify(image: str, output: Path) -> dict:
    if output.exists() or output.is_symlink():
        raise ValueError("output exists; preserve previous evidence")
    expected = source_manifest()
    contract = runtime_contract(inspect_image(image), inspect_base())
    name = "oasis-source-check-" + uuid.uuid4().hex
    created = False
    try:
        docker("create", "--name", name, "--network=none", "--read-only", contract["image"])
        created = True
        with TemporaryFile() as archive:
            subprocess.run(  # nosec B603 B607 - fixed Docker copy from stopped temporary container
                ["docker", "cp", name + ":/app/.", "-"],
                check=True,
                stdout=archive,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            if archive.tell() > MAX_BYTES:
                raise ValueError("image archive exceeds size limit")
            archive.seek(0)
            observed = verify_archive(archive, expected)
        if source_manifest() != expected:
            raise ValueError("local runtime sources changed during verification")
        report = {
            "schema": "oasis.live-image-sources.v1",
            "image": contract["image"],
            "runtime_contract": contract,
            "application_sources_verified": True,
            "files": observed,
            "manifest_hash": sha256_hex(canonical_json(observed)),
            "verifier_hash": sha256_hex(Path(__file__).read_bytes()),
            "container_started": False,
            "execution_eligible": False,
            "scope": (
                "tracked application bytes only; "
                "dependencies, base runtime and daemon remain trusted"
            ),
        }
    finally:
        if created:
            docker("rm", name)
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(report, handle, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="local immutable image ID or resolvable tag")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = verify(args.image, args.out)
    except Exception:  # noqa: BLE001 - do not disclose image/provider contents
        parser.exit(3, "Image source verification refused; no execution authorized.\n")
    print(json.dumps({k: report[k] for k in ("image", "manifest_hash", "execution_eligible")}))


if __name__ == "__main__":
    main()
