"""Operation authorization for the observation server.

Operations (triggering an observation run, gate approvals) are disabled by
default. Tokens are read from the environment at request time (never from the
configuration file) and compared in constant time. The environment variable
may hold a comma-separated list of tokens so two-person approvals can be
confirmed by a second operator with a distinct token; each distinct token
derives a distinct actor identity. If no token is configured at startup and
``ops_token_required`` is true the server refuses to start; otherwise
operation endpoints fail closed with 503 until a token is present.
"""

import hmac
from collections.abc import Mapping

from ..errors import ConfigurationError


class OpsAuthorizer:
    def __init__(self, token_env: str, *, required: bool, environ: Mapping[str, str]) -> None:
        if not token_env.strip():
            raise ConfigurationError("ops token environment variable name must not be empty")
        self._token_env = token_env
        self._required = required
        self._environ = environ

    def _tokens(self) -> tuple[str, ...]:
        raw = self._environ.get(self._token_env, "")
        return tuple(token.strip() for token in raw.split(",") if token.strip())

    @property
    def enabled(self) -> bool:
        return bool(self._tokens())

    def require_enabled(self) -> None:
        if not self.enabled:
            if self._required:
                raise ConfigurationError(f"operations are enabled but {self._token_env} is not set")
            raise OpsDisabledError(f"operations disabled: {self._token_env} not set")

    def check(self, provided: str | None) -> bool:
        if not provided or not provided.strip():
            return False
        return any(
            hmac.compare_digest(token.encode(), provided.encode()) for token in self._tokens()
        )


class OpsDisabledError(Exception):
    """Operations are not configured; the endpoint fails closed."""
