"""Protect the BSC evidence replay from altered transitive sources and cached bytecode."""

import py_compile
from hashlib import sha256
from importlib.util import cache_from_source
from pathlib import Path

import pytest

from docs.verification.fixtures.replay_bsc_pool_expansion import PinnedEvidenceScripts


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("remove", [False, True])
def test_unavailable_or_altered_transitive_file_blocks_all_scripts(tmp_path, remove):
    marker = tmp_path / "executed"
    entry = tmp_path / "entry.py"
    entry.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    child = tmp_path / "child.py"
    child.write_text("result = 17\n")
    hashes = {p.name: digest(p) for p in (entry, child)}
    if remove:
        child.unlink()
    else:
        child.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    with pytest.raises(ValueError, match=r"required evidence script unavailable|SHA-256 differs"):
        PinnedEvidenceScripts(tmp_path, hashes).run_path(entry)
    assert not marker.exists()


def test_nested_load_executes_only_verified_source_and_ignores_bytecode(tmp_path):
    entry = tmp_path / "entry.py"
    child = tmp_path / "child.py"
    marker = tmp_path / "unverified-code"
    entry.write_text(
        "from importlib.util import module_from_spec, spec_from_file_location\n"
        "from pathlib import Path\n"
        "spec = spec_from_file_location('child', Path(__file__).with_name('child.py'))\n"
        "module = module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "result = module.result\n"
    )
    child.write_text("result = 17\n")
    unverified = tmp_path / "forged.py"
    unverified.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nresult = 23\n")
    py_compile.compile(
        str(unverified),
        cfile=cache_from_source(str(child)),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        doraise=True,
    )
    runner = PinnedEvidenceScripts(tmp_path, {p.name: digest(p) for p in (entry, child)})
    assert runner.run_path(entry)["result"] == 17
    assert not marker.exists()


def test_registered_entry_cannot_run_unregistered_nested_path(tmp_path):
    marker = tmp_path / "unverified-code"
    entry = tmp_path / "entry.py"
    unverified = tmp_path / "other.py"
    entry.write_text(f"import runpy\nrunpy.run_path({str(unverified)!r})\n")
    unverified.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    runner = PinnedEvidenceScripts(tmp_path, {entry.name: digest(entry)})
    with pytest.raises(ValueError, match="unregistered evidence script"):
        runner.run_path(entry)
    assert not marker.exists()
