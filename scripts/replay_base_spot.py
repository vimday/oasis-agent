"""Replay Base spot decisions from retained frames into one paper trading report."""

import argparse
from pathlib import Path

from oasis_agent.launch.spot_replay import SpotReplay, replay_spot
from oasis_agent.models.common import canonical_json

MAX_INPUT_BYTES = 16_000_000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("spot replay input exceeds bound")
        result = replay_spot(SpotReplay.from_json(raw))
        with args.output.open("xb") as handle:
            handle.write(canonical_json(result.model_dump(mode="json")) + b"\n")
    except Exception:  # noqa: BLE001 - imported source data can include private diagnostics
        parser.exit(1, "Base spot replay refused.\n")
    print(f"Retained {len(result.decisions)} Base spot paper decisions.")


if __name__ == "__main__":
    main()
