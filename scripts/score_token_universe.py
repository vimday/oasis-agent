"""Score a dynamic discovered universe with retained enrichment; no execution capability."""

import argparse
from pathlib import Path

from oasis_agent.launch.batch_scoring import BatchRequest, score_batch
from oasis_agent.models.common import canonical_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > 10_000_000:
            raise ValueError("batch input too large")
        result = score_batch(BatchRequest.from_json(args.input.read_bytes()))
        with args.output.open("xb") as handle:
            handle.write(canonical_json(result.model_dump(mode="json")) + b"\n")
    except Exception:  # noqa: BLE001 - sanitize imported data failures
        parser.exit(1, "Batch scoring failed; execution disabled.\n")
    print("Research scores retained; missing enrichment remains unscored; no orders submitted.")


if __name__ == "__main__":
    main()
