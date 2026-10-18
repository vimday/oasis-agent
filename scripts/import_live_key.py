"""One-shot container provisioning; emits public address only and never signs."""

import argparse
import json
import os
import re
import stat
from pathlib import Path

from oasis_agent.execution.lp_signer_daemon import _parse_hex_key, address_of
from oasis_agent.models.common import normalize_address


def import_key(
    source: Path,
    destination: Path,
    account: str,
    input_uid: int,
    owner_uid: int,
    owner_gid: int,
    *,
    dotenv: bool = False,
) -> str:
    """Read a bounded private file inside the provisioner, then install exclusively.

    Call only inside the isolated provisioning container for production material.
    Tests use fresh ephemeral files. No private bytes enter the result or errors.
    """
    account = normalize_address(account)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != input_uid
            or not 1 <= info.st_size <= (65_536 if dotenv else 68)
        ):
            raise ValueError("private source permissions or size refused")
        raw = handle.read(65_537 if dotenv else 69).decode("utf-8")
        if dotenv:
            values = []
            for line in raw.splitlines():
                match = re.fullmatch(r"\s*(?:export\s+)?OASIS_PRIVATE_KEY\s*=\s*(.*?)\s*", line)
                if match:
                    value = match[1]
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    values.append(value)
            if len(values) != 1:
                raise ValueError("one dedicated key entry required")
            raw = values[0]
        key = _parse_hex_key(raw)
    if address_of(key) != account:
        raise ValueError("key address differs from the dedicated account")
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise ValueError("private destination directory required")
    # Never overwrite a previously provisioned key, including an existing symlink.
    out = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(out, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        os.fchown(handle.fileno(), owner_uid, owner_gid)
        handle.write(key.hex().encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chown(destination.parent, owner_uid, owner_gid)
    os.chmod(destination.parent, 0o700)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return account


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--input-uid", type=int, required=True)
    parser.add_argument("--dotenv", action="store_true")
    args = parser.parse_args()
    if os.getuid() != 0 or not Path("/proc/self/status").exists():
        raise ValueError("isolated Linux provisioner required")
    address = import_key(
        Path("/input/canary.key"),
        Path("/secrets/canary.key"),
        args.account,
        args.input_uid,
        10001,
        10001,
        dotenv=args.dotenv,
    )
    print(json.dumps({"account": address, "installed": True, "signed": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - secret material must never reach exception output
        raise SystemExit("Canary key provisioning refused; private details omitted.") from None
