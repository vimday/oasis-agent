"""Repo-wide code-standard gates.

These tests enforce repository conventions that ruff cannot express:

1. The future-annotations import is banned. The project targets
   Python 3.12, where forward references are expressed with ``typing.Self``,
   quoted annotations, or definition order. The lazy-annotation import hides
   real import-time evaluation errors, so its presence is treated as a defect.
2. Module-level import cycles are banned. ``oasis_agent``, ``scripts``, and
   ``tests`` form a single import graph; any strongly connected component with
   more than one module is a structural defect, even when CPython happens to
   tolerate the cycle. ``TYPE_CHECKING``-guarded imports are type-only and are
   excluded because they never execute at runtime.
3. Every module in the scan scope imports cleanly at runtime, proving no
   dangling forward reference survived the future-import removal.
4. Intra-repo imports nested in function bodies are banned: they mask
   dependency cycles and defeat import-time validation (ruff PLC0415 enforces
   the same rule as a lint).
"""

import ast
import subprocess
import sys
from collections import defaultdict
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


def _module_imports(path: Path, module: str) -> set[str]:
    """Return real module names imported at module scope (runtime only)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = module.split(".")
    is_package = path.name == "__init__.py"
    imports: set[str] = set()

    def resolve(level: int, base: str | None) -> str:
        if level == 0 and base:
            return base
        # level 1 = the module's own package; each extra level walks one up.
        # A package __init__ resolves from itself; a plain module from its parent.
        keep = len(parts) - level + (1 if is_package else 0)
        prefix = parts[:keep]
        return ".".join((*prefix, base)) if base else ".".join(prefix)

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                imports.add(resolve(node.level, node.module))
            elif node.module:
                imports.add(node.module)
        elif isinstance(node, ast.If) and _is_type_checking_guard(node):
            continue  # type-only imports never execute at runtime
    return imports


def _is_type_checking_guard(node: ast.If) -> bool:
    return (
        isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
        and bool(node.body)
        and isinstance(node.body[0], (ast.Import, ast.ImportFrom))
    )


def _strong_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC over the module graph (iterative-safe depth handling)."""
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in sorted(graph.get(node, ())):
            if successor not in indices:
                strongconnect(successor)
                lowlink[node] = min(lowlink[node], lowlink[successor])
            elif successor in on_stack:
                lowlink[node] = min(lowlink[node], indices[successor])
        if lowlink[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            strongconnect(node)
    return components


def test_no_module_level_import_cycles() -> None:
    graph: dict[str, set[str]] = defaultdict(set)
    for path, module in _python_files():
        for imported in _module_imports(path, module):
            if imported.startswith(_REPO_PREFIXES):
                graph[module].add(imported)

    cyclic = [c for c in _strong_components(graph) if len(c) > 1]
    assert not cyclic, (
        "module-level import cycles are banned; break the dependency with "
        f"definition order or a shared leaf module: {cyclic}"
    )


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
