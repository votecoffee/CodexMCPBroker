"""Publish the synthetic app configuration; does not install a plugin or start a worker."""

import argparse
import hashlib
import sys
from pathlib import Path

from universal_tools.cli import initialize
from universal_tools.models import atomic_json


def publish(state: Path, generation: int, port: int):
    initialize(state, port)
    worker = Path(__file__).with_name("framework_worker.py").resolve()
    publication = state / "example-application.json"
    atomic_json(
        publication,
        {
            "schema_version": 1,
            "id": "example-text-app",
            "display_name": "Example Text App",
            "instructions": "Use search_tools to discover this app's current text operations.",
            "backends": [
                {
                    "revision": f"text-{generation}",
                    "artifact": {
                        "path": str(worker),
                        "sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
                    },
                    "service": {
                        "id": "text-engine",
                        "display_name": "Example text engine",
                        "transport": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": [str(worker), "--generation", str(generation)],
                        },
                        "session_mode": "exclusive",
                        "allow_writes": False,
                        "timeout_seconds": 30,
                    },
                }
            ],
        },
    )
    return publication


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--generation", type=int, choices=[1, 2], required=True)
    parser.add_argument("--port", type=int, default=8890)
    args = parser.parse_args()
    print(publish(args.state.resolve(), args.generation, args.port))
