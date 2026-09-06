"""Fail CI on new secret candidates; retain only reviewed non-secret fingerprints."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCAN_PATHS = (
    "src",
    "scripts",
    "tests",
    "configs",
    "contracts/src",
    "contracts/fork",
    "contracts/test",
    ".github",
    "pyproject.toml",
    "Makefile",
)


def unreviewed_candidates(current: dict, baseline: dict) -> list[tuple[str, int, str]]:
    if current.get("version") != baseline.get("scanner_version"):
        raise ValueError("secret scanner version differs from reviewed baseline")
    approved = set()
    for item in baseline["reviewed_nonsecrets"]:
        if not item.get("reason") or item.get("classification") != "public_or_synthetic_nonsecret":
            raise ValueError("invalid non-secret baseline entry")
        approved.add((item["path"], item["type"], item["hashed_secret"]))
    return [
        (path, x["line_number"], x["type"])
        for path, items in current["results"].items()
        for x in items
        if (path, x["type"], x["hashed_secret"]) not in approved
    ]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = subprocess.run(
        [
            sys.executable,
            "-m",
            "detect_secrets",
            "scan",
            "--no-verify",
            "--all-files",
            *SCAN_PATHS,
            "--exclude-files",
            r"(__pycache__|\.pyc$|^configs/secret-scan-baseline\.json$)",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=120,
    )
    current = json.loads(output.stdout)
    baseline = json.loads((root / "configs/secret-scan-baseline.json").read_text())
    findings = unreviewed_candidates(current, baseline)
    for path, line, kind in findings:
        print(f"{path}:{line}: unreviewed {kind} candidate (value redacted)")
    if findings:
        raise SystemExit(1)
    print(
        "Secret gate passed: no unreviewed candidates in code/config/test scope; no credential verification requests"
    )


if __name__ == "__main__":
    main()
