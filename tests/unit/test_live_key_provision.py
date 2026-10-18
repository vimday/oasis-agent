"""Provisioning validates identity before writing and never replaces a key."""

import json
import os
from pathlib import Path

import pytest
from eth_account import Account

from scripts import provision_live_key as provisioner
from scripts.import_live_key import import_key


def fixture_key(tmp_path):
    key = Account.create()
    source = tmp_path / "source.key"
    source.write_text(key.key.hex())
    source.chmod(0o600)
    target = tmp_path / "private"
    target.mkdir()
    return key, source, target / "canary.key"


def install(source, target, address):
    return import_key(source, target, address, os.getuid(), os.getuid(), os.getgid())


def test_private_identity_verified_before_exclusive_install(tmp_path):
    key, source, target = fixture_key(tmp_path)
    original = source.read_bytes()
    assert install(source, target, key.address) == key.address.lower()
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert Account.from_key(target.read_text()).address == key.address
    assert source.read_bytes() == original
    with pytest.raises(FileExistsError):
        install(source, target, key.address)
    assert Account.from_key(target.read_text()).address == key.address


@pytest.mark.parametrize(
    "fault", ["account", "permissions", "owner", "symlink", "fifo", "oversized"]
)
def test_bad_source_never_creates_destination(tmp_path, fault):
    key, source, target = fixture_key(tmp_path)
    address = key.address
    uid = os.getuid()
    if fault == "account":
        address = Account.create().address
    elif fault == "permissions":
        source.chmod(0o644)
    elif fault == "owner":
        uid += 1
    elif fault == "symlink":
        link = source.with_name("link")
        link.symlink_to(source)
        source = link
    elif fault == "fifo":
        source.unlink()
        os.mkfifo(source, 0o600)
    else:
        source.write_text("a" * 4096)
    with pytest.raises((ValueError, OSError)):
        import_key(source, target, address, uid, os.getuid(), os.getgid())
    assert not target.exists()


def test_destination_symlink_preserves_unrelated_file(tmp_path):
    key, source, target = fixture_key(tmp_path)
    other = tmp_path / "unrelated"
    other.write_text("preserve")
    target.symlink_to(other)
    with pytest.raises(FileExistsError):
        install(source, target, key.address)
    assert other.read_text() == "preserve"


def test_host_wrapper_refuses_existing_volume_without_reading_key(tmp_path, monkeypatch):
    _, source, _ = fixture_key(tmp_path)
    calls = []
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        if path == source:
            raise AssertionError("host must not read the key")
        return original(path, *args, **kwargs)

    def docker(*args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return "sha256:" + "a" * 64
        if args[:2] == ("volume", "ls"):
            return "oasis-canary-keys"
        raise AssertionError("no mutation or key container")

    monkeypatch.setattr(Path, "read_text", guarded)
    monkeypatch.setattr(provisioner, "docker", docker)
    with pytest.raises(ValueError, match="already exists"):
        provisioner.provision(source.resolve(), Path("configs/live.base-eth-usdc.json"), "image")
    assert len(calls) == 2


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_dotenv_extracts_only_named_key_without_interpolation(tmp_path, quote):
    key, source, target = fixture_key(tmp_path)
    source.write_text(
        "OTHER_KEY=not-the-key\n" + f"export OASIS_PRIVATE_KEY={quote}{key.key.hex()}{quote}\n"
    )
    assert (
        import_key(source, target, key.address, os.getuid(), os.getuid(), os.getgid(), dotenv=True)
        == key.address.lower()
    )


@pytest.mark.parametrize(
    "value",
    [
        "OTHER_KEY=abc",
        "OASIS_PRIVATE_KEY=${OTHER_KEY}",
        "OASIS_PRIVATE_KEY=abc\nOASIS_PRIVATE_KEY=def",
    ],
)
def test_dotenv_missing_duplicate_or_expansion_refused(tmp_path, value):
    key, source, target = fixture_key(tmp_path)
    source.write_text(value)
    with pytest.raises(ValueError):
        import_key(source, target, key.address, os.getuid(), os.getuid(), os.getgid(), dotenv=True)
    assert not target.exists()


@pytest.mark.parametrize("bad_metadata", [False, True])
def test_host_wrapper_checks_mapped_uid_before_creating_volume(tmp_path, monkeypatch, bad_metadata):
    key, source, _ = fixture_key(tmp_path)
    config = json.loads(Path("configs/live.base-eth-usdc.json").read_text())
    config["account"] = key.address.lower()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    calls = []

    def docker(*args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return "sha256:" + "a" * 64
        if args[:2] == ("volume", "ls"):
            return ""
        if args[:2] == ("volume", "create"):
            return "oasis-canary-keys"
        if "scripts.import_live_key" in args:
            assert args[args.index("--input-uid") + 1] == "0"
            assert "--dotenv" in args
            return json.dumps({"account": config["account"], "installed": True, "signed": False})
        if "os.lstat" in args[-1]:
            return json.dumps(
                {
                    "uid": 0,
                    "mode": 0o644 if bad_metadata else 0o600,
                    "size": source.stat().st_size,
                    "regular": True,
                }
            )
        assert "load_private_key" in args[-1]
        return config["account"]

    monkeypatch.setattr(provisioner, "docker", docker)
    if bad_metadata:
        with pytest.raises(ValueError, match="metadata"):
            provisioner.provision(source.resolve(), config_path, "image", dotenv=True)
        assert not any(call[:2] == ("volume", "create") for call in calls)
    else:
        report = provisioner.provision(source.resolve(), config_path, "image", dotenv=True)
        assert report["account"] == config["account"] and not report["signed"]
