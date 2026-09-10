"""Fail the frontend gate when an inline dashboard script cannot be parsed by Node."""

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path


class DashboardScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            if any(name == "src" for name, _ in attrs):
                raise ValueError("external dashboard scripts require an explicit validation gate")
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def main() -> int:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("frontend gate failed: Node.js is required")
    root = Path(__file__).resolve().parents[1]
    parser = DashboardScripts()
    parser.feed((root / "src/oasis_agent/server/static/index.html").read_text())
    parser.close()
    if not parser.scripts:
        raise SystemExit("frontend gate failed: no dashboard scripts found")
    for script in parser.scripts:
        result = subprocess.run(
            [node, "--check"], input=script, text=True, capture_output=True, timeout=10, check=False
        )
        if result.returncode:
            raise SystemExit(f"frontend syntax gate failed:\n{result.stderr}")
    print(f"Frontend syntax gate passed: {len(parser.scripts)} script(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
