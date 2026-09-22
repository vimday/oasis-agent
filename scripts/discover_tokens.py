"""Discover many research candidates from retained signals or one public profile request."""

import argparse
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

import certifi

from oasis_agent.launch.discovery import DiscoveryPolicy, DiscoveryRequest, discover
from oasis_agent.launch.discovery_import import import_profiles
from oasis_agent.models.common import canonical_json, sha256_hex

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ValueError("discovery redirects forbidden")


def capture_profiles(output: Path, policy: DiscoveryPolicy) -> None:
    """One bounded credential-free request. No retries or automatic purchases."""
    output.mkdir(parents=True, exist_ok=False)
    opener = urllib.request.build_opener(
        NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
    )
    # Fixed HTTPS endpoint; no user-controlled URL and redirects disabled.
    request = urllib.request.Request(PROFILES_URL, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(5_000_001)
            status = response.status
    except (OSError, ValueError) as exc:
        (output / "failure.json").write_bytes(
            canonical_json(
                {
                    "source": "dexscreener_public_profiles",
                    "retrieved_at": int(time.time()),
                    "error_type": type(exc).__name__,
                    "http_status": exc.code if isinstance(exc, urllib.error.HTTPError) else None,
                    "execution_eligible": False,
                }
            )
        )
        raise ValueError("public profile acquisition failed") from None
    retrieved = int(time.time())
    if len(raw) > 5_000_000 or status != 200:
        raise ValueError("invalid profile response")
    (output / "profiles.raw.json").write_bytes(raw)
    (output / "capture.json").write_bytes(
        canonical_json(
            {
                "source": "dexscreener_public_profiles",
                "url": PROFILES_URL,
                "retrieved_at": retrieved,
                "status": status,
                "raw_sha256": sha256_hex(raw),
                "source_event_time_available": False,
                "execution_eligible": False,
            }
        )
    )
    value = DiscoveryRequest(
        evaluated_at=retrieved, policy=policy, signals=import_profiles(raw, retrieved_at=retrieved)
    )
    (output / "request.json").write_bytes(canonical_json(value.model_dump(mode="json")))
    (output / "result.json").write_bytes(canonical_json(discover(value).model_dump(mode="json")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", type=Path, help="retained DiscoveryRequest JSON")
    mode.add_argument("--public-profiles", action="store_true")
    parser.add_argument("--chains", type=int, nargs="+", default=[8453])
    parser.add_argument(
        "--output", type=Path, required=True, help="new file, or public capture directory"
    )
    args = parser.parse_args()
    try:
        if args.public_profiles:
            capture_profiles(args.output, DiscoveryPolicy(chains=tuple(args.chains)))
        else:
            if args.input.stat().st_size > 5_000_000:
                raise ValueError("oversized discovery request")
            request = DiscoveryRequest.from_json(args.input.read_bytes())
            with args.output.open("xb") as handle:
                handle.write(canonical_json(discover(request).model_dump(mode="json")) + b"\n")
    except Exception:  # noqa: BLE001 - no hostile response/credential details in console errors
        parser.exit(1, "Discovery failed; no trading qualification granted.\n")
    print("Discovery retained; evidence enrichment required; execution disabled.")


if __name__ == "__main__":
    main()
