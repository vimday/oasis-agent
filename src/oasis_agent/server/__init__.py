"""Local observation API server for Oasis Agent.

The server is an observe-first process: it opens the evidence, shadow and
execution journals strictly read-only, exposes canonical payloads with their
digests, and never imports signing or broadcast capability. Optional
operations (triggering an observation run, gate approvals) require an
explicit token configured through the environment and are journaled in a
separate append-only operations database.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
