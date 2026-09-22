"""Collect explicit EVM token identity from two configured RPCs; never transact."""

import argparse
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.models import Target, parse_target
from oasis_agent.launch.recon import acquire_identity, identity_report, retain_identity
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--target", type=Path, help="explicit v2 chain/asset research policy")
    args = parser.parse_args()
    target = Target() if args.target is None else parse_target(args.target.read_bytes())
    prefix = "BASE" if args.target is None else "OASIS_LAUNCH"
    urls = (
        os.environ.get(prefix + "_RPC_PRIMARY") or "",
        os.environ.get(prefix + "_RPC_SECONDARY") or "",
    )
    if not all(urls) or urls[0] == urls[1]:
        parser.error(
            f"two distinct endpoints required: {prefix}_RPC_PRIMARY and {prefix}_RPC_SECONDARY"
        )
    recorders = (BoundedExchangeRecorder(), BoundedExchangeRecorder())
    transports = tuple(
        JsonRpcTransport(url, timeout=10, retries=0, recorder=recorder)
        for url, recorder in zip(urls, recorders, strict=True)
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                acquire_identity,
                transport,
                recorder,
                source=source,
                block_number=args.block,
                target=target,
            )
            for transport, recorder, source in zip(
                transports, recorders, ("primary", "secondary"), strict=True
            )
        ]
        try:
            samples = (futures[0].result(), futures[1].result())
        except Exception:  # noqa: BLE001 - external failure details may contain credentials
            parser.exit(1, "Token identity acquisition failed; no eligibility granted.\n")
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
