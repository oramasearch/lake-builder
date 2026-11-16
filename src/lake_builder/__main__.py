# SPDX-License-Identifier: MIT

"""One build per process; only the final result is written to stdout."""

import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from .build import run_build
from .config import BuildError, Config
from .events import Events
from .registry import image_size, metadata_digest, write_credentials
from .source import fetch_source


def interrupted(signum, frame):
    raise KeyboardInterrupt


def execute(config, env, events):
    with tempfile.TemporaryDirectory(prefix="lake-builder-") as temporary:
        root = Path(temporary)
        events.phase("fetching")
        context, dockerfiles = fetch_source(config, root)
        credentials, credential_secrets = write_credentials(config, root, env)
        events.add_secrets(credential_secrets)
        metadata = root / "metadata.json"
        events.phase("building")
        run_build(config, context, dockerfiles, credentials, metadata, env, events, root)
        digest = metadata_digest(metadata)
        size = image_size(config, digest)
    result = {"digest": digest, "size": size}
    events.phase("done")
    events.emit("done", **result)
    print(json.dumps(result), flush=True)


def main():
    events = None
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        events = Events(os.environ)
        if os.getuid() == 0:
            raise BuildError("lake-builder must run as a non-root user")
        config = Config.from_env(os.environ)
        execute(config, os.environ, events)
    except (BuildError, OSError, ValueError, KeyboardInterrupt) as error:
        if isinstance(error, BuildError):
            message = str(error)
        elif isinstance(error, KeyboardInterrupt):
            message = "build interrupted"
        else:
            message = "builder encountered invalid data or an I/O failure"
        if events is not None:
            message = events.redact(message)
            events.flush(final=True)
            events.emit("error", message=message)
        print(message, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
