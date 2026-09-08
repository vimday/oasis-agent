"""Launch the Oasis Agent observation server.

The server is an observe-first process: it opens the evidence, shadow and
execution journals strictly read-only, serves canonical payloads with their
digests, and never imports signing or broadcast capability. Optional
operations require a bearer token configured through the environment and are
journaled in a separate append-only operations database.

Usage:
    OASIS_SERVER_OPS_TOKEN=<token> .venv/bin/python scripts/run_server.py \
        --config configs/server.toml
"""

import argparse
import sys
from pathlib import Path

import uvicorn

from oasis_agent.errors import ConfigurationError
from oasis_agent.server.app import create_app
from oasis_agent.server.config import load_server_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="server TOML configuration")
    args = parser.parse_args()

    try:
        config = load_server_config(args.config)
    except ConfigurationError as exc:
        parser.error(str(exc))

    application = create_app(config, config_path=args.config)
    uvicorn.run(
        application,
        host=config.host,
        port=config.port,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
