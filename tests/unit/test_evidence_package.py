import hashlib
from pathlib import Path

PACKAGE = Path(__file__).parents[2] / "reports" / "evidence" / "lp-modules-2026-09-07"


def _manifest_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in (PACKAGE / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, relative = line.split("  ", 1)
        entries[relative] = digest
    return entries


def test_evidence_package_declares_distinct_code_and_evidence_commits() -> None:
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")

    assert "代码基线" in readme
    assert "HEAD" not in readme.splitlines()[2]
    assert "manifest 自身不在清单内" in readme


def test_evidence_manifest_covers_all_non_manifest_files_and_hashes() -> None:
    entries = _manifest_entries()
    actual = {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.name != "MANIFEST.sha256"
    }

    assert set(entries) == actual
    for relative, expected in entries.items():
        assert hashlib.sha256((PACKAGE / relative).read_bytes()).hexdigest() == expected


def test_archived_gate_log_has_no_extra_blank_line_at_eof() -> None:
    payload = (PACKAGE / "gates" / "contract-check.txt").read_bytes()

    assert payload.endswith(b"\n")
    assert not payload.endswith(b"\n\n")
