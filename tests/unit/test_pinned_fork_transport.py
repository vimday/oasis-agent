import subprocess
from unittest.mock import patch

import pytest

from scripts.run_pinned_fork import PublicPoster


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(28, "sensitive-command"),
        subprocess.TimeoutExpired("secret", 25),
    ],
)
def test_curl_failure_uses_redacted_retryable_transport_error(error):
    with (
        patch("scripts.run_pinned_fork.subprocess.run", side_effect=error),
        pytest.raises(OSError, match=r"^public RPC transport failed$"),
    ):
        PublicPoster("https://example.invalid").post(b"{}")
