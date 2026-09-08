"""Typed dependency access to the application state.

Every route in the observation API receives an :class:`AppState` value object
through FastAPI's dependency injection instead of reaching into
``request.app.state`` by hand. The value object gives static analysis a
concrete type for each service, keeps route signatures self-documenting, and
makes the services trivially replaceable in tests without touching route
code. It is assembled per request from the lifespan-owned singletons, so the
dependency itself is stateless and cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from .audit import ServerOpsJournal
from .auth import OpsAuthorizer
from .chains import ChainRegistry
from .config import ServerConfig
from .databases import ReadOnlyDatabases
from .runner import ShadowRunCoordinator


@dataclass(frozen=True, slots=True)
class AppState:
    """Read-only view of the services owned by the application lifespan."""

    config: ServerConfig
    databases: ReadOnlyDatabases
    journal: ServerOpsJournal
    authorizer: OpsAuthorizer
    registry: ChainRegistry
    runner: ShadowRunCoordinator | None


def get_state(request: Request) -> AppState:
    """Assemble the state object from the lifespan-owned singletons."""
    state = request.app.state
    return AppState(
        config=state.config,
        databases=state.databases,
        journal=state.journal,
        authorizer=state.authorizer,
        registry=state.registry,
        runner=state.runner,
    )


State = Annotated[AppState, Depends(get_state)]
"""Route annotation for the injected application state."""
