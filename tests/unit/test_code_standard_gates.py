"""Repo-wide code-standard gates.

These tests enforce repository conventions that ruff cannot express:

1. The future-annotations import is banned. The project targets
   Python 3.12, where forward references are expressed with ``typing.Self``,
   quoted annotations, or definition order. The lazy-annotation import hides
   real import-time evaluation errors, so its presence is treated as a defect.
2. Every module in the scan scope imports cleanly at runtime, proving no
   dangling forward reference survived the future-import removal.
3. Intra-repo imports nested in function bodies are banned: they mask
   dependency cycles and defeat import-time validation (ruff PLC0415 enforces
   the same rule as a lint).
"""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCAN_DIRS = ("src", "tests", "scripts")
_REPO_PREFIXES = ("oasis_agent", "tests", "scripts")
# Built dynamically so this gate does not trip on its own source text.
_BANNED_IMPORT = f"from __future__ import {'annotations'}"


def _python_files() -> list[tuple[Path, str]]:
    """Return (path, module_name) for every Python file in the scan scope."""
    files: list[tuple[Path, str]] = []
    for relative in SCAN_DIRS:
        directory = ROOT / relative
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            parts = path.relative_to(directory).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if relative != "src":
                parts = (relative, *parts)
            module = ".".join(parts)
            files.append((path, module))
    return files


def test_no_future_annotations_imports() -> None:
    offenders = [
        path for path, _ in _python_files() if _BANNED_IMPORT in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "the future-annotations import is banned on Python 3.12; "
        f"use typing.Self or quoted annotations instead: {offenders}"
    )


def test_every_module_imports_cleanly() -> None:
    """Import every module in the scan scope in a fresh interpreter."""
    modules = [module for _, module in _python_files()]
    script = "\n".join(f"import {module}" for module in modules)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        "one or more modules failed to import; check for dangling forward "
        f"references or missing dependencies:\n{result.stderr[-2000:]}"
    )


def test_no_lazy_intra_repo_imports() -> None:
    """Flag intra-repo imports nested inside function bodies."""
    offenders: list[tuple[str, int]] = []
    for path, _ in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    names = {alias.name for alias in child.names}
                elif isinstance(child, ast.ImportFrom):
                    names = {child.module or ""} if child.level == 0 else set()
                else:
                    continue
                if any(name.split(".")[0] in _REPO_PREFIXES for name in names):
                    offenders.append((str(path.relative_to(ROOT)), child.lineno))
    assert not offenders, (
        "lazy intra-repo imports mask dependency cycles; move them to module "
        f"scope (ruff PLC0415): {sorted(set(offenders))}"
    )
