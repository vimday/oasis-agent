"""Observation-only shadow infrastructure. No signing or execution dependencies.

``ShadowJournal`` keeps the health chain; ``LpShadowRunner`` evaluates LP
policies against pinned state and journals hash-linked decisions. Everything
here is read-only with respect to the chain: no signer, no broadcaster, and
``execution_eligible`` is always False.
"""

from .journal import ShadowJournal
from .lp_runner import DECISION_SCHEMA, LpShadowDecision, LpShadowRunner

__all__ = [
    "DECISION_SCHEMA",
    "LpShadowDecision",
    "LpShadowRunner",
    "ShadowJournal",
]
