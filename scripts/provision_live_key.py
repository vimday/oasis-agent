"""Install an existing dedicated canary key through a no-network Docker process.

The host wrapper reads metadata only. The key is never passed in argv, stdin,
environment variables, image layers or tool output. The source is preserved.
"""

import argparse
import json
import os
import stat
import subprocess
from pathlib import Path

from oasis_agent.live.config import LiveConfig


def docker(*args: str) -> str:
    result = subprocess.run(  # nosec B603 B607 - fixed CLI, no shell or key bytes
        ["docker", *args], check=True, capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip()


def provision(source: Path, config: Path, image: str, *, dotenv: bool = False) -> dict[str, object]:
    if not source.is_absolute() or source.resolve() != source or "," in str(source):
        raise ValueError("absolute non-symlink private source required")
    info = source.lstat()  # Metadata only. Private bytes are read exclusively in the container.
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ValueError("source must be an owned 0600 regular file")
    c = LiveConfig.load(config)
    image_id = docker("image", "inspect", image, "--format", "{{.Id}}")
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError("immutable local image required")
    volume = "oasis-canary-keys"
    existing = docker("volume", "ls", "--format", "{{.Name}}").splitlines()
    if volume in existing:
        raise ValueError("key volume already exists; refusing replacement")
    # Docker Desktop maps a host-owned bind to root inside its Linux VM. Check
    # host ownership above, then discover only the mapped metadata, never bytes.
    mapped = json.loads(
        docker(
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--user=0:0",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--memory=256m",
            "--mount",
            f"type=bind,src={source},dst=/input/canary.key,readonly",
            image_id,
            "python",
            "-c",
            "import json,os,stat; s=os.lstat('/input/canary.key'); "
            "print(json.dumps({'uid':s.st_uid,'mode':stat.S_IMODE(s.st_mode),"
            "'size':s.st_size,'regular':stat.S_ISREG(s.st_mode)}))",
        )
    )
    if (
        not isinstance(mapped, dict)
        or set(mapped) != {"uid", "mode", "size", "regular"}
        or type(mapped["uid"]) is not int
        or mapped["uid"] < 0
        or mapped["mode"] != 0o600
        or mapped["size"] != info.st_size
        or mapped["regular"] is not True
    ):
        raise ValueError("container source metadata differs from the owned host file")
    docker("volume", "create", volume)
    # On refusal leave the dedicated volume for operator inspection; never delete key storage.
    public = json.loads(
        docker(
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--user=0:0",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--cap-add=FOWNER",
            "--cap-add=DAC_OVERRIDE",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--memory=256m",
            "--mount",
            f"type=bind,src={source},dst=/input/canary.key,readonly",
            "--mount",
            f"type=volume,src={volume},dst=/secrets",
            image_id,
            "python",
            "-m",
            "scripts.import_live_key",
            "--account",
            c.account,
            "--input-uid",
            str(mapped["uid"]),
            *(["--dotenv"] if dotenv else []),
        )
    )
    if public != {"account": c.account, "installed": True, "signed": False}:
        raise ValueError("unexpected provisioning result")
    # A separate production UID verifies it can load the installed file; no signing call.
    verified = docker(
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--user=10001:10001",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=64",
        "--memory=256m",
        "--mount",
        f"type=volume,src={volume},dst=/secrets,readonly",
        image_id,
        "python",
        "-c",
        "from pathlib import Path; from oasis_agent.execution.lp_signer_daemon import "
        "load_private_key,address_of; print(address_of(load_private_key("
        "key_file=Path('/secrets/canary.key'),key_env=None,environ={})))",
    )
    if verified != c.account:
        raise ValueError("production identity verification refused")
    return {
        "schema": "oasis.canary-key-provision.v1",
        "account": c.account,
        "image": image_id,
        "volume": volume,
        "owner_uid": 10001,
        "mode": "0600",
        "network": "none",
        "execution_eligible": False,
        "signed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/live.base-eth-usdc.json"))
    parser.add_argument("--image", default="oasis-live:local")
    parser.add_argument(
        "--dotenv",
        action="store_true",
        help="extract only OASIS_PRIVATE_KEY inside the no-network container",
    )
    args = parser.parse_args()
    print(json.dumps(provision(args.source, args.config, args.image, dotenv=args.dotenv), indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never echo a Docker error or private source contents
        raise SystemExit("Canary provisioning incomplete; existing storage preserved.") from None
