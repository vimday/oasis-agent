"""Operation authorization for the observation server."""

from __future__ import annotations

import pytest

from oasis_agent.errors import ConfigurationError
from oasis_agent.server.auth import OpsAuthorizer, OpsDisabledError


def authorizer(environ, *, required=True, token_env="OASIS_SERVER_OPS_TOKEN"):
    return OpsAuthorizer(token_env, required=required, environ=environ)


def test_correct_token_passes():
    auth = authorizer({"OASIS_SERVER_OPS_TOKEN": "s3cret-token"})
    assert auth.enabled is True
    assert auth.check("s3cret-token") is True


def test_wrong_or_missing_token_fails_closed():
    auth = authorizer({"OASIS_SERVER_OPS_TOKEN": "s3cret-token"})
    assert auth.check("wrong") is False
    assert auth.check(None) is False
    assert auth.check("") is False


def test_whitespace_only_token_is_disabled():
    auth = authorizer({"OASIS_SERVER_OPS_TOKEN": "   "})
    assert auth.enabled is False


def test_require_enabled_raises_when_required_and_unset():
    auth = authorizer({}, required=True)
    with pytest.raises(ConfigurationError, match="not set"):
        auth.require_enabled()


def test_require_enabled_raises_disabled_error_when_optional():
    auth = authorizer({}, required=False)
    with pytest.raises(OpsDisabledError, match="operations disabled"):
        auth.require_enabled()


def test_empty_token_env_name_rejected():
    with pytest.raises(ConfigurationError, match="must not be empty"):
        OpsAuthorizer("  ", required=True, environ={})


def test_comma_separated_tokens_allow_second_operator():
    auth = authorizer(
        {"OASIS_SERVER_OPS_TOKEN": "ops-token-alpha, ops-token-beta"},
        required=False,
    )
    assert auth.enabled is True
    assert auth.check("ops-token-alpha") is True
    assert auth.check("ops-token-beta") is True
    assert auth.check("ops-token-gamma") is False


def test_check_is_constant_time_for_different_lengths():
    # hmac.compare_digest must not short-circuit on length.
    auth = authorizer({"OASIS_SERVER_OPS_TOKEN": "a" * 32})
    assert auth.check("a" * 16) is False
