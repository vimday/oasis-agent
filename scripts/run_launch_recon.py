"""Collect basic Base token identity from two configured RPCs; never transact."""

import argparse
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.recon import acquire_identity, identity_report, retain_identity
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    urls = (
        os.environ.get("BASE_RPC_PRIMARY") or os.environ.get("BASE_ALCHEMY_RPC_URL") or "",
        os.environ.get("BASE_RPC_SECONDARY") or os.environ.get("BASE_RPC_URL") or "",
    )
    if not all(urls) or urls[0] == urls[1]:
        parser.error(
            "two distinct endpoints required: BASE_RPC_PRIMARY/BASE_ALCHEMY_RPC_URL and "
            "BASE_RPC_SECONDARY/BASE_RPC_URL"
        )
    recorders = (BoundedExchangeRecorder(), BoundedExchangeRecorder())
    transports = tuple(
        JsonRpcTransport(url, timeout=10, retries=0, recorder=recorder)
        for url, recorder in zip(urls, recorders, strict=True)
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                acquire_identity, transport, recorder, source=source, block_number=args.block
            )
            for transport, recorder, source in zip(
                transports, recorders, ("base_primary", "base_secondary"), strict=True
            )
        ]
        try:
            samples = (futures[0].result(), futures[1].result())
        except Exception:  # noqa: BLE001 - external failure details may contain credentials
            parser.exit(1, "Base identity acquisition failed; no eligibility granted.\n")
    store = EvidenceStore(args.evidence)
    try:
        references = (retain_identity(store, samples[0]), retain_identity(store, samples[1]))
        report = identity_report(samples, references)
        # Never overwrite a previous evidence report.
        with args.report.open("xb") as output:
            output.write(canonical_json(report) + b"\n")
        print("identity agreement:", report["identity_agreement"], "; execution_eligible: false")
    finally:
        store.close()


if __name__ == "__main__":
    main()
