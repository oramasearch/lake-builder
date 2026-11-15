# SPDX-License-Identifier: MIT

"""Run one ephemeral rootless BuildKit daemon, build through buildctl, and drain progress.

The image is built on scratch, so there is no shell for upstream's buildctl-daemonless.sh.
This module does the same work: start `rootlesskit buildkitd`, wait until buildctl can reach
it, run the build, then stop the daemon.
"""

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import BuildError
from .registry import registry_scheme

# Upstream's BUILDCTL_CONNECT_RETRIES_MAX default; probes back off by 100 ms + 20 ms per try.
DAEMON_RETRIES = 10
DAEMON_LOG_LINES = 200


def daemon_address(env):
    runtime = env.get("XDG_RUNTIME_DIR", "")
    if not runtime.startswith("/") or "\x00" in runtime or any(c.isspace() for c in runtime):
        raise BuildError("XDG_RUNTIME_DIR must be an absolute path for rootless BuildKit")
    return f"unix://{runtime.rstrip('/')}/buildkit/buildkitd.sock"


def daemon_command(address, child_env):
    # Word-split BUILDKITD_FLAGS exactly as the upstream helper's unquoted $BUILDKITD_FLAGS.
    flags = child_env.get("BUILDKITD_FLAGS", "").split()
    return ["rootlesskit", "buildkitd", *flags, f"--addr={address}"]


def build_command(config, context, dockerfiles, metadata, address):
    insecure = ",registry.insecure=true" if registry_scheme(config) == "http" else ""
    command = [
        "buildctl",
        f"--addr={address}",
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


def child_environment(env, credentials, config=None):
    # Do not inherit tokens, arbitrary frontend options, proxy credentials, or build args.
    names = ("PATH", "HOME", "USER", "XDG_RUNTIME_DIR", "TMPDIR", "BUILDKITD_FLAGS")
    result = {name: env[name] for name in names if name in env}
    result["DOCKER_CONFIG"] = str(credentials)
    if config is not None and registry_scheme(config) == "http":
        # registry.insecure on the exporters is not enough: cache import/export still speaks
        # https to the in-cluster lake registry. Tell buildkitd the host is plain HTTP.
        flags = result.get("BUILDKITD_FLAGS", "")
        result["BUILDKITD_FLAGS"] = (
            f"{flags} --config {plain_http_registry_config(config, credentials)}".strip()
        )
    return result


def plain_http_registry_config(config, credentials):
    # BuildKit matches [registry."<host>"] against the pushed reference's registry host exactly,
    # so key it on TARGET's registry (lake omits the scheme's default port there).
    host = getattr(config, "registry", None) or urlsplit(config.lake).netloc
    path = Path(credentials) / "buildkitd.toml"
    path.write_text(f'[registry."{host}"]\n  http = true\n  insecure = true\n')
    return path


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


def start_daemon(address, child_env, log_path):
    with open(log_path, "wb") as log:
        try:
            return subprocess.Popen(
                daemon_command(address, child_env),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            raise BuildError("cannot start rootlesskit buildkitd") from error


def report_daemon_log(log_path, events):
    try:
        lines = Path(log_path).read_bytes().splitlines()[-DAEMON_LOG_LINES:]
    except OSError:
        return
    for line in lines:
        events.log("buildkitd: " + line.decode("utf-8", errors="replace").rstrip("\r"))
    events.flush(final=True)


def daemon_ready(address, child_env):
    try:
        probe = subprocess.run(
            ["buildctl", f"--addr={address}", "debug", "workers"],
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError as error:
        raise BuildError("cannot start buildctl") from error
    return probe.returncode == 0


def wait_for_daemon(daemon, address, child_env, log_path, events):
    for attempt in range(DAEMON_RETRIES + 2):
        code = daemon.poll()
        if code is not None:
            report_daemon_log(log_path, events)
            raise BuildError(f"BuildKit daemon exited with status {code} before it was ready")
        if daemon_ready(address, child_env):
            return
        time.sleep((100 + attempt * 20) / 1000)
    report_daemon_log(log_path, events)
    raise BuildError(f"BuildKit daemon was not ready after {DAEMON_RETRIES} retries")


def run_client(command, child_env, events):
    try:
        process = subprocess.Popen(
            command,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        raise BuildError("cannot start buildctl") from error
    try:
        drain_output(process, events)
        code = process.wait()
        if code:
            raise BuildError(f"BuildKit exited with status {code}")
    finally:
        if process.poll() is None:
            terminate_group(process)
        process.stdout.close()


def run_build(config, context, dockerfiles, credentials, metadata, env, events, workdir):
    address = daemon_address(env)
    child_env = child_environment(env, credentials, config)
    log_path = Path(workdir) / "buildkitd.log"
    daemon = start_daemon(address, child_env, log_path)
    try:
        wait_for_daemon(daemon, address, child_env, log_path, events)
        command = build_command(config, context, dockerfiles, metadata, address)
        run_client(command, child_env, events)
    finally:
        if daemon.poll() is None:
            terminate_group(daemon)
