"""Validate local console modules, assets and script syntax without a bundler."""

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path


class DashboardScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.sources: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            source = dict(attrs).get("src")
            if source is not None:
                if not re.fullmatch(r"/static/[a-z-]+\.js", source):
                    raise ValueError("dashboard scripts must be local static modules")
                self.sources.append(source)
                return
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def dashboard_sources() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1] / "src/oasis_agent/server/static"
    html = (root / "index.html").read_text()
    parser = DashboardScripts()
    parser.feed(html)
    parser.close()
    if parser.scripts:
        raise ValueError("inline scripts are forbidden by the console CSP")
    if not parser.sources:
        raise ValueError("dashboard has no module entrypoint")
    sources = {path.name: path.read_text() for path in sorted(root.glob("*.js"))}
    for source in parser.sources:
        if source.removeprefix("/static/") not in sources:
            raise ValueError("dashboard entrypoint is missing")
    for content in sources.values():
        for dependency in re.findall(r'from\s+[\'"]([^\'"]+)[\'"]', content):
            if not dependency.startswith("./") or dependency[2:] not in sources:
                raise ValueError(f"missing or external console module: {dependency}")
    return sources


def dashboard_source() -> str:
    """Full source for architectural and behavior tests across module boundaries."""
    return "\n".join(dashboard_sources().values())


def main() -> int:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("frontend gate failed: Node.js is required")
    sources = dashboard_sources()
    for script in sources.values():
        result = subprocess.run(
            [node, "--input-type=module", "--check"],
            input=script,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise SystemExit(f"frontend syntax gate failed:\n{result.stderr}")
    print(f"Frontend syntax gate passed: {len(sources)} local modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
