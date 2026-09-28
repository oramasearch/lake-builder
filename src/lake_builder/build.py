"""Invoke the base image's daemonless helper and drain its progress output."""

import os
import selectors
import signal
import subprocess

from .config import BuildError
from .registry import registry_scheme


def build_command(config, context, dockerfiles, metadata):
    insecure = ",registry.insecure=true" if registry_scheme(config) == "http" else ""
    command = [
        "buildctl-daemonless.sh",
        "build",
        "--frontend",
        "dockerfile.v0",
        "--local",
        f"context={context}",
        "--local",
        f"dockerfile={dockerfiles}",
        "--opt",
        "filename=Dockerfile",
        "--opt",
        "platform=linux/amd64",
        "--output",
        f"type=image,name={config.target},push=true{insecure}",
        "--import-cache",
        f"type=registry,ref={config.cache}{insecure}",
        "--export-cache",
        f"type=registry,ref={config.cache},mode=max{insecure}",
        "--metadata-file",
        str(metadata),
        "--progress",
        "plain",
    ]
    for name, value in sorted(config.arguments.items()):
        command.extend(["--opt", f"build-arg:{name}={value}"])
    return command


def child_environment(env, credentials):
    # Do not inherit tokens, arbitrary frontend options, proxy credentials, or build args.
    names = ("PATH", "HOME", "USER", "XDG_RUNTIME_DIR", "TMPDIR", "BUILDKITD_FLAGS")
    result = {name: env[name] for name in names if name in env}
    result["DOCKER_CONFIG"] = str(credentials)
    return result


def progress_line(raw, events, pushing):
    line = raw.decode("utf-8", errors="replace").rstrip("\r")
    if not pushing and ("pushing layers" in line or "pushing manifest" in line):
        events.phase("pushing")
        pushing = True
    events.log(line)
    return pushing


def drain_output(process, events):
    pending = b""
    pushing = False
    discarding = False
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fd)
                    break
                if discarding:
                    if b"\n" not in chunk:
                        continue
                    _, chunk = chunk.split(b"\n", 1)
                    discarding = False
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    pushing = progress_line(line, events, pushing)
                if len(pending) > 16384:
                    events.log("[oversized progress line omitted]")
                    pending = b""
                    discarding = True
            events.flush()
    if pending:
        progress_line(pending, events, pushing)
    events.flush(final=True)


def terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_build(config, context, dockerfiles, credentials, metadata, env, events):
    command = build_command(config, context, dockerfiles, metadata)
    try:
        process = subprocess.Popen(
            command,
            env=child_environment(env, credentials),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        raise BuildError("cannot start buildctl-daemonless.sh") from error
    try:
        drain_output(process, events)
        code = process.wait()
        if code:
            raise BuildError(f"BuildKit exited with status {code}")
    finally:
        if process.poll() is None:
            terminate_group(process)
        process.stdout.close()
