"""Safe classification of the existing curl adapter's sanitized failures.

This adds no transport, retry, endpoint selection or network capability. Only
the adapter's exact public error grammar is interpreted; arbitrary exception
messages, stderr and endpoints never become diagnostic fields.
"""

import re
from dataclasses import dataclass

from .transport import HttpPoster

_EXIT_CATEGORIES = {
    5: "proxy_resolution_failed",
    6: "host_resolution_failed",
    7: "connection_failed",
    22: "http_error_status_unspecified",
    28: "timeout",
    35: "tls_handshake_failed",
    52: "empty_reply",
    55: "send_failed",
    56: "receive_failed",
    60: "tls_certificate_verification_failed",
}
_PUBLIC_FAILURE = re.compile(r"RPC HTTPS request failed \(exit (-?[0-9]{1,3})\)")


@dataclass(frozen=True, slots=True)
class CurlFailure:
    category: str
    exit_code: int | None


class ClassifiedCurlError(OSError):
    def __init__(self, failure: CurlFailure) -> None:
        super().__init__("RPC HTTPS request failed; diagnostic fields retained separately")
        self.failure = failure


def classify_curl_failure(error: OSError) -> CurlFailure:
    """Classify a process outcome, never claim a network root cause."""
    # Do not stringify arbitrary errors: a subclass's __str__ can itself leak.
    if type(error) is not OSError or len(error.args) != 1 or type(error.args[0]) is not str:
        return CurlFailure("unclassified_transport_failure", None)
    match = _PUBLIC_FAILURE.fullmatch(error.args[0])
    if match is None:
        return CurlFailure("unclassified_transport_failure", None)
    code = int(match[1])
    if code == 0 or not -128 <= code <= 255:
        return CurlFailure("unclassified_transport_failure", None)
    category = (
        "process_terminated" if code < 0 else _EXIT_CATEGORIES.get(code, "other_curl_failure")
    )
    return CurlFailure(category, code)


class ClassifiedCurlPoster:
    """Wrap the sealed curl adapter without changing its request behavior."""

    def __init__(self, curl: HttpPoster) -> None:
        self.curl = curl

    def post(self, payload: bytes) -> bytes:
        try:
            return self.curl.post(payload)
        except OSError as error:
            raise ClassifiedCurlError(classify_curl_failure(error)) from None
