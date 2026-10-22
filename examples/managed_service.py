"""Disposable management demo. Restarts only its synthetic in-memory worker."""

import argparse
import signal
import threading

from universal_tools.management import (
    ComponentVersion,
    ManagedService,
    ManagedStatus,
    RestartResult,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    args = parser.parse_args()
    stopped = threading.Event()
    guard = threading.Lock()
    generation = 1

    def status():
        with guard:
            return ManagedStatus(
                message=f"Synthetic worker generation {generation}",
                versions={"app": ComponentVersion(installed="1.0.0", running="1.0.0")},
                restart_allowed=True,
                restart_reason="Ready to restart the synthetic worker",
            )

    def restart(_):
        nonlocal generation
        # Real apps fence admission, preserve pending outcomes and enqueue their supervisor here.
        with guard:
            generation += 1
        return RestartResult(outcome="accepted", message="Synthetic worker restarted")

    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    with ManagedService(
        "example", "Example MCP service", status, restart=restart, registry=args.registry
    ):
        stopped.wait()


if __name__ == "__main__":
    main()
