"""Opt-in diagnostic adapter for an explicitly investigated router recovery.

No CLI, budget creation, automatic recovery or request is performed by importing
this module. A recovery must still use the original scope, lock and BudgetedPoster.
The sealed legacy attempt journal remains intact; safe diagnostic fields use a
separate versioned stream at the same scope directory.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.curl_diagnostics import ClassifiedCurlError, ClassifiedCurlPoster
from scripts.capture_quote_grid import Source
from scripts.capture_router_activity import Scope, ScopedPoster


class DiagnosticRouterPoster:
    def __init__(self, source: Source, scope: Scope, config: Path, raw: bytes, root: Path):
        if (
            root.resolve() != Path(scope.output_directory).resolve()
            or source not in scope.sources
            or config.read_bytes() != raw
            or Scope.from_json(raw) != scope
        ):
            raise ValueError("diagnostic adapter scope differs")
        self.source, self.root, self.digest = source.alias, root, sha256_hex(raw)
        self.scoped = ScopedPoster(source, scope, config, raw, root)
        self.scoped.http = ClassifiedCurlPoster(self.scoped.http)

    def post(self, payload: bytes) -> bytes:
        try:
            return self.scoped.post(payload)
        except ClassifiedCurlError as error:
            item = {
                "schema": "oasis.router-transport-diagnostic.v1",
                "source": self.source,
                "scope_sha256": self.digest,
                "request_sha256": sha256_hex(payload),
                "completed_at": datetime.now(UTC).isoformat(),
                "category": error.failure.category,
                "curl_exit_code": error.failure.exit_code,
            }
            with (self.root / f"{self.source}-transport-diagnostics.jsonl").open("ab") as log:
                log.write(canonical_json(item) + b"\n")
                log.flush()
                os.fsync(log.fileno())
            raise
