"""Fake BuildKit daemon for the native contract tests; it serves nothing and builds nothing."""

import json
import os
import signal
import sys
import time
from pathlib import Path


def main():
    settings = json.loads(Path(__file__).with_name("fixture.json").read_text())
    address = next(arg for arg in sys.argv[1:] if arg.startswith("--addr="))
    socket = Path(address.removeprefix("--addr=unix://"))
    Path(settings["daemon_trace"]).write_text(
        json.dumps(
            {
                "args": sys.argv[1:],
                "env_keys": sorted(os.environ),
                "rootlesskit": os.environ.get("FAKE_ROOTLESSKIT") == "1",
            }
        )
    )
    print("fake buildkitd starting", flush=True)
    if settings.get("daemon_exit"):
        print("fake buildkitd: cannot start the OCI worker", flush=True)
        return settings["daemon_exit"]

    def stop(signum, frame):
        Path(settings["daemon_stopped"]).write_text(str(signum))
        socket.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    if not settings.get("daemon_never_ready"):
        socket.parent.mkdir(parents=True, exist_ok=True)
        socket.write_text("")
    while True:
        time.sleep(0.05)


if __name__ == "__main__":
    sys.exit(main())
