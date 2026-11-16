"""Process fixture for the native contract tests; this does not build an image."""

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def option(arguments, name):
    return arguments[arguments.index(name) + 1]


def csv_fields(value):
    return dict(field.split("=", 1) for field in value.split(","))


def debug_workers(settings, address):
    # The fake daemon marks readiness by creating its socket path as a plain file.
    with open(settings["probes"], "a") as probes:
        probes.write(address + "\n")
    return 0 if Path(address.removeprefix("unix://")).exists() else 1


def main():
    settings = json.loads(Path(__file__).with_name("fixture.json").read_text())
    address = sys.argv[1].removeprefix("--addr=") if sys.argv[1].startswith("--addr=") else ""
    arguments = sys.argv[2:] if address else sys.argv[1:]
    if arguments[:2] == ["debug", "workers"]:
        return debug_workers(settings, address)
    if arguments[:1] != ["build"] or not address:
        print(f"unexpected buildctl invocation: {sys.argv[1:]}", flush=True)
        return 2
    locals_ = [arguments[index + 1] for index, arg in enumerate(arguments) if arg == "--local"]
    directories = dict(value.split("=", 1) for value in locals_)
    context = Path(directories["context"])
    dockerfile = Path(directories["dockerfile"]) / "Dockerfile"
    credential_path = Path(os.environ["DOCKER_CONFIG"]) / "config.json"
    credentials = json.loads(credential_path.read_text())
    trace = {
        "address": address,
        "args": arguments,
        "env_keys": sorted(os.environ),
        "credential_path": str(credential_path),
        "credential_mode": credential_path.stat().st_mode & 0o777,
        "credential_keys": sorted(credentials),
        "credential_auth_hashes": {
            host: hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            for host, value in credentials["auths"].items()
        },
        "context": str(context),
        "files": {
            str(file.relative_to(context)): base64.b64encode(file.read_bytes()).decode()
            for file in context.rglob("*")
            if file.is_file()
        },
        "modes": {
            str(file.relative_to(context)): file.stat().st_mode & 0o777
            for file in context.rglob("*")
            if file.is_file()
        },
        "dockerfile": dockerfile.read_text(),
    }
    Path(settings["trace"]).write_text(json.dumps(trace))
    if settings.get("hang"):
        print("#1 waiting", flush=True)
        time.sleep(60)
    for line in settings.get("logs", ["#1 building", "#2 pushing layers"]):
        print(line, flush=True)
    if settings.get("exit_code"):
        return settings["exit_code"]
    target = csv_fields(option(arguments, "--output"))["name"]
    cache = csv_fields(option(arguments, "--export-cache"))["ref"]
    for ref in (target, cache):
        host, image = ref.split("/", 1)
        image = image.rsplit(":", 1)[0]
        auth = credentials["auths"][host]["auth"]
        request = Request(
            f"http://{host}/v2/{image}/blobs/uploads/",
            data=b"",
            headers={"Authorization": "Basic " + auth},
        )
        try:
            with urlopen(request, timeout=5) as response:
                response.read()
        except HTTPError as error:
            print(f"registry push refused: HTTP {error.code} DENIED", flush=True)
            return 8
    metadata = {"containerimage.digest": settings["digest"]}
    if settings.get("bad_metadata"):
        metadata = {"containerimage.digest": "not-a-digest"}
    Path(option(arguments, "--metadata-file")).write_text(json.dumps(metadata))
    return 0


if __name__ == "__main__":
    sys.exit(main())
