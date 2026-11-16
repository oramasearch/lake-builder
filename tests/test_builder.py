# SPDX-License-Identifier: MIT

"""Exercise the CLI against real local HTTP and a controllable BuildKit process."""

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from lake_builder.config import BuildError, Config, build_args
from lake_builder.events import Events
from lake_builder.registry import encoded_auth, image_size

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TOKEN = "source-credential-private"
PUSH_TOKEN = "tenant-alpha-credential"
EVENTS_TOKEN = "build-credential-private"
REVISION = "a" * 64


def encode_manifest(value):
    content = json.dumps(value, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    return digest, content


def credential_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class FakeLake:
    def __init__(self):
        self.files = {
            "app/Dockerfile": b"FROM scratch\nCOPY hello /hello\n",
            "app/hello": b"hello from lake\n",
            "app/.dockerignore": b"ignored\n",
            "outside/private": b"must not be fetched",
        }
        self.tree_override = None
        self.events = []
        self.reads = []
        self.pushes = []
        self.manifest_reads = []
        self.event_status = 200
        self.source_status = 200
        self.redirect = False
        self.bad_json = False
        self.binary = set()
        self.file_modes = {}
        self.manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:" + "c" * 64, "size": 71},
            "layers": [{"digest": "sha256:" + "d" * 64, "size": 97}],
        }
        self.digest, self.manifest_bytes = encode_manifest(self.manifest)
        self.manifests = {self.digest: self.manifest_bytes}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def reply(self, code, payload):
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                fixture.get(self)

            def do_POST(self):
                fixture.post(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.url = f"http://{self.host}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def authorized(self, handler, expected):
        if handler.headers.get("Authorization") != expected:
            handler.reply(401, {"error": "wrong credential"})
            return False
        return True

    def get(self, handler):
        parsed = urlsplit(handler.path)
        if parsed.path.startswith("/v2/"):
            self.get_manifest(handler, parsed.path)
            return
        if not self.authorized(handler, "Bearer " + SOURCE_TOKEN):
            return
        query = parse_qs(parsed.query)
        self.reads.append((parsed.path, query))
        if self.redirect:
            handler.send_response(302)
            handler.send_header("Location", self.url + "/credential-leak")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        if self.source_status != 200:
            handler.reply(self.source_status, {"ok": False})
            return
        if self.bad_json:
            handler.reply(200, b"not JSON")
            return
        if query.get("repository") != ["repo/with space"] or query.get("revision") != [REVISION]:
            handler.reply(400, {"ok": False})
            return
        if parsed.path == "/v1/repository_tree":
            if query.get("recursive") != ["true"]:
                handler.reply(400, {"ok": False})
                return
            items = self.tree_override
            if items is None:
                items = [
                    {"path": path, "kind": "file", "mode": self.file_modes.get(path, "100644")}
                    for path in self.files
                ]
            handler.reply(200, {"ok": True, "value": {"items": items}})
        elif parsed.path == "/v1/repository_file":
            path = query.get("path", [""])[0]
            if path not in self.files:
                handler.reply(404, {"ok": False})
                return
            if path in self.binary:
                value = {"content_base64": base64.b64encode(self.files[path]).decode()}
            else:
                value = {"contents": self.files[path].decode()}
            handler.reply(200, {"ok": True, "value": value})
        else:
            handler.reply(404, {"ok": False})

    def get_manifest(self, handler, path):
        if not self.authorized(handler, "Basic " + encoded_auth(PUSH_TOKEN)):
            return
        self.manifest_reads.append(path)
        digest = path.rsplit("/", 1)[1]
        handler.reply(200 if digest in self.manifests else 404, self.manifests.get(digest, b""))

    def post(self, handler):
        raw = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        if handler.path == "/v1/publish_build_event":
            if not self.authorized(handler, "Bearer " + EVENTS_TOKEN):
                return
            self.events.append((time.monotonic(), raw, json.loads(raw)))
            handler.reply(self.event_status, {"ok": self.event_status == 200})
        elif handler.path.startswith("/v2/"):
            if not self.authorized(handler, "Basic " + encoded_auth(PUSH_TOKEN)):
                return
            self.pushes.append(handler.path)
            if not handler.path.startswith("/v2/alpha/"):
                handler.reply(403, {"errors": [{"code": "DENIED"}]})
                return
            handler.reply(202, {})
        else:
            handler.reply(404, {})


class BuilderContractTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".proof").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".proof")
        self.root = Path(self.temporary.name)
        self.lake = FakeLake()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("buildctl", "buildkitd", "rootlesskit"):
            executable = self.bin / name
            fixture = (ROOT / f"tests/fixtures/{name}.py").read_text()
            executable.write_text(f"#!{sys.executable}\n" + fixture)
            executable.chmod(0o755)
        self.runtime = self.root / "run"
        self.runtime.mkdir()
        self.settings = {
            "trace": str(self.root / "trace.json"),
            "daemon_trace": str(self.root / "daemon.json"),
            "daemon_stopped": str(self.root / "daemon-stopped"),
            "probes": str(self.root / "probes.log"),
            "digest": self.lake.digest,
        }
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(ROOT / "src"),
            "TMPDIR": str(self.root),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "LAKE_URL": self.lake.url,
            "SOURCE_TOKEN": SOURCE_TOKEN,
            "REPOSITORY": "repo/with space",
            "REVISION": REVISION,
            "CONTEXT": "app",
            "TARGET": self.lake.host + "/alpha/demo:v1",
            "CACHE_REF": self.lake.host + "/alpha/cache/demo",
            "PUSH_TOKEN": PUSH_TOKEN,
            "EVENTS_TOKEN": EVENTS_TOKEN,
            "EVENTS_TOPIC": "build-events.test-build",
            "BUILD_ARGS": '{"PYTHON_VERSION":"3.13"}',
            "BUILD_ARG_ALLOW": "PYTHON_VERSION,APP_DEPS,APP_RUNTIME_IMAGE",
        }
        for key in ("DOCKERFILE", "BUILD_ID", "BASE_REGISTRY_AUTH", "BUILDKITD_FLAGS"):
            self.env.pop(key, None)

    def tearDown(self):
        self.lake.close()
        self.temporary.cleanup()

    def command(self):
        (self.bin / "fixture.json").write_text(json.dumps(self.settings))
        return [sys.executable, "-m", "lake_builder"]

    def run_builder(self):
        return subprocess.run(
            self.command(), env=self.env, capture_output=True, text=True, timeout=15
        )

    def trace(self):
        return json.loads((self.root / "trace.json").read_text())

    def daemon_trace(self):
        return json.loads((self.root / "daemon.json").read_text())

    def daemon_stopped(self):
        return (self.root / "daemon-stopped").exists()

    def event_values(self, kind):
        return [value for _, _, value in self.lake.events if value["type"] == kind]

    def assert_failed_before_build(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(message, result.stderr)
        self.assertFalse((self.root / "trace.json").exists())
        self.assertFalse((self.root / "daemon.json").exists())
        self.assertTrue(self.event_values("error"))
        self.assertFalse(self.lake.pushes)

    def test_success_authentication_context_cache_events_and_final_json(self):
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        final = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(
            final, {"digest": self.lake.digest, "size": len(self.lake.manifest_bytes) + 168}
        )
        self.assertEqual(self.event_values("done")[0]["digest"], final["digest"])
        phases = [value["phase"] for value in self.event_values("phase")]
        self.assertEqual(phases, ["fetching", "building", "pushing", "done"])
        self.assertEqual(
            self.lake.pushes,
            ["/v2/alpha/demo/blobs/uploads/", "/v2/alpha/cache/demo/blobs/uploads/"],
        )
        trace = self.trace()
        self.assertEqual(set(trace["files"]), {"Dockerfile", "hello", ".dockerignore"})
        self.assertIn("build-arg:PYTHON_VERSION=3.13", trace["args"])
        self.assertIn("platform=linux/amd64", trace["args"])
        self.assertIn("--import-cache", trace["args"])
        self.assertIn("--export-cache", trace["args"])
        self.assertTrue(any("mode=max" in arg for arg in trace["args"]))
        self.assertEqual(trace["credential_mode"], 0o600)
        self.assertNotIn("SOURCE_TOKEN", trace["env_keys"])
        self.assertNotIn("PUSH_TOKEN", trace["env_keys"])
        self.assertNotIn("EVENTS_TOKEN", trace["env_keys"])
        self.assertFalse(Path(trace["credential_path"]).is_relative_to(trace["context"]))
        self.assertFalse(Path(trace["credential_path"]).exists())
        self.assertFalse(Path(trace["context"]).exists())
        self.assertFalse(
            any(query.get("path") == ["outside/private"] for _, query in self.lake.reads)
        )
        for secret in (SOURCE_TOKEN, PUSH_TOKEN, EVENTS_TOKEN):
            self.assertNotIn(secret, result.stdout + result.stderr + json.dumps(trace["files"]))

    def test_outside_token_prefix_is_refused_by_registry(self):
        self.env["TARGET"] = self.lake.host + "/beta/demo:v1"
        self.env["CACHE_REF"] = self.lake.host + "/beta/cache/demo"
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP 403 DENIED", result.stderr)
        self.assertEqual(self.lake.pushes, ["/v2/beta/demo/blobs/uploads/"])
        self.assertEqual(result.stdout, "")
        self.assertTrue(self.event_values("error"))
        self.assertFalse(self.event_values("done"))
        self.assertFalse(Path(self.trace()["credential_path"]).exists())

    def test_wrong_push_credential_is_refused(self):
        self.env["PUSH_TOKEN"] = "wrong-token"
        result = self.run_builder()
        self.assertIn("HTTP 401", result.stderr)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_base_registry_auth_is_merged_with_push_auth_authoritative(self):
        pull_auth = base64.b64encode(b"pull-user:pull-password-private").decode()
        pull_credentials = {
            "auth": pull_auth,
            "identitytoken": "pull-identity-token-private",
        }
        supplied_push_credentials = {"auth": base64.b64encode(b"wrong:push-token").decode()}
        source = self.root / ".dockerconfigjson"
        source.write_text(
            json.dumps(
                {
                    "auths": {
                        "https://index.docker.io/v1/": pull_credentials,
                        self.lake.host: supplied_push_credentials,
                    },
                    "credsStore": "must-not-be-copied",
                }
            )
        )
        self.env["BASE_REGISTRY_AUTH"] = str(source)
        self.settings["logs"] = [
            f"{pull_auth} pull-user:pull-password-private pull-password-private",
            "pull-identity-token-private",
        ]

        result = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.trace()["credential_auth_hashes"],
            {
                "https://index.docker.io/v1/": credential_hash(pull_credentials),
                self.lake.host: credential_hash({"auth": encoded_auth(PUSH_TOKEN)}),
            },
        )
        self.assertEqual(self.trace()["credential_keys"], ["auths"])
        self.assertNotIn("BASE_REGISTRY_AUTH", self.trace()["env_keys"])
        output = result.stdout + result.stderr + str(self.lake.events)
        for secret in (
            pull_auth,
            "pull-user:pull-password-private",
            "pull-password-private",
            "pull-identity-token-private",
        ):
            self.assertNotIn(secret, output)

    def test_missing_base_registry_auth_file_keeps_push_only_config(self):
        self.env["BASE_REGISTRY_AUTH"] = str(self.root / "missing-.dockerconfigjson")

        result = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.trace()["credential_auth_hashes"],
            {self.lake.host: credential_hash({"auth": encoded_auth(PUSH_TOKEN)})},
        )

    def test_invalid_base_registry_auth_fails_without_logging_contents(self):
        source = self.root / ".dockerconfigjson"
        source.write_text('{"auths":"base-auth-private"}')
        self.env["BASE_REGISTRY_AUTH"] = str(source)

        result = self.run_builder()

        self.assert_failed_before_build(result, "must contain an auths object")
        self.assertNotIn("base-auth-private", result.stderr + str(self.lake.events))

    def test_disallowed_build_arg_fails_before_source_or_buildkit(self):
        self.env["BUILD_ARGS"] = '{"API_KEY":"must-not-be-logged"}'
        result = self.run_builder()
        self.assert_failed_before_build(result, "not in BUILD_ARG_ALLOW")
        self.assertEqual(self.lake.reads, [])
        self.assertNotIn("must-not-be-logged", result.stderr + str(self.lake.events))

    def test_custom_allow_list_is_applied(self):
        self.env["BUILD_ARG_ALLOW"] = "VERSION"
        self.env["BUILD_ARGS"] = '{"VERSION":"value with spaces, and = punctuation"}'
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "build-arg:VERSION=value with spaces, and = punctuation", self.trace()["args"]
        )

    def test_build_argument_types_are_rejected(self):
        for value in ("[]", "null", '{"PYTHON_VERSION":3}', "{", '{"PYTHON_VERSION":"\\u0000"}'):
            with self.subTest(value=value):
                self.env["BUILD_ARGS"] = value
                self.assert_failed_before_build(self.run_builder(), "must be")

    def test_cache_cannot_cross_registry_or_space(self):
        for cache in (self.lake.host + "/beta/cache/demo", "other.test/alpha/cache/demo"):
            with self.subTest(cache=cache):
                self.env["CACHE_REF"] = cache
                self.assert_failed_before_build(self.run_builder(), "TARGET's registry and space")

    def test_image_reference_cannot_inject_buildkit_options(self):
        self.env["TARGET"] += ",push=false"
        self.assert_failed_before_build(self.run_builder(), "TARGET must be")

    def test_path_traversal_and_symlinks_are_rejected(self):
        for path, mode in (
            ("app/../../escape", "100644"),
            ("/tmp/escape", "100644"),
            ("app/link", "120000"),
        ):
            with self.subTest(path=path):
                self.lake.tree_override = [{"path": path, "kind": "file", "mode": mode}]
                result = self.run_builder()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((self.root / "trace.json").exists())
                self.assertTrue(self.event_values("error"))

    def test_source_authentication_failure_does_not_start_buildkit(self):
        self.env["SOURCE_TOKEN"] = "wrong-token"
        self.assert_failed_before_build(self.run_builder(), "HTTP 401")

    def test_source_redirect_is_not_followed(self):
        self.lake.redirect = True
        self.assert_failed_before_build(self.run_builder(), "HTTP 302")
        self.assertEqual(len(self.lake.reads), 1)

    def test_malformed_source_is_rejected(self):
        self.lake.bad_json = True
        self.assert_failed_before_build(self.run_builder(), "invalid JSON")

    def test_binary_source_and_executable_mode_are_preserved(self):
        self.lake.files["app/binary"] = b"\x00\xff\x80"
        self.lake.binary.add("app/binary")
        self.lake.file_modes["app/hello"] = "100755"
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(base64.b64decode(self.trace()["files"]["binary"]), b"\x00\xff\x80")
        self.assertEqual(self.trace()["modes"]["hello"], 0o755)

    def test_inline_dockerfile_is_outside_context(self):
        self.env["DOCKERFILE"] = "inline:FROM scratch\nCOPY hello /hello\n"
        del self.lake.files["app/Dockerfile"]
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace()["dockerfile"], "FROM scratch\nCOPY hello /hello\n")
        self.assertNotIn("Dockerfile", self.trace()["files"])

    def test_dockerfile_outside_context_is_fetched_explicitly(self):
        self.env["DOCKERFILE"] = "build/Dockerfile"
        self.lake.files["build/Dockerfile"] = b"FROM scratch\n"
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace()["dockerfile"], "FROM scratch\n")
        self.assertNotIn("build/Dockerfile", self.trace()["files"])

    def test_event_server_failure_does_not_fail_build(self):
        self.lake.event_status = 503
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["digest"], self.lake.digest)

    def test_log_batch_limits_and_redaction(self):
        self.settings["logs"] = [
            SOURCE_TOKEN + " " + PUSH_TOKEN + " " + EVENTS_TOKEN + " " + encoded_auth(PUSH_TOKEN),
            *["☃" * 4096 for _ in range(20)],
        ]
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        logs = [(stamp, raw) for stamp, raw, event in self.lake.events if event["type"] == "log"]
        self.assertTrue(logs)
        self.assertTrue(all(len(raw) <= 65536 for _, raw in logs))
        # The builder spaces batches by its own send time; these stamps are when the fake lake
        # received them, so request latency can shave a little off a gap. Without the limit the
        # batches would arrive milliseconds apart.
        for first, second in zip(logs, logs[1:]):
            self.assertGreaterEqual(second[0] - first[0], 0.9)
        combined = result.stderr + str(self.lake.events)
        for secret in (SOURCE_TOKEN, PUSH_TOKEN, EVENTS_TOKEN, encoded_auth(PUSH_TOKEN)):
            self.assertNotIn(secret, combined)
        self.assertIn("[redacted]", combined)

    def test_build_failure_never_prints_success_json(self):
        self.settings["exit_code"] = 17
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("status 17", result.stderr)
        self.assertFalse(self.event_values("done"))

    def test_bad_metadata_fails_without_success_json(self):
        self.settings["bad_metadata"] = True
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid sha256 digest", result.stderr)

    def test_manifest_digest_mismatch_fails(self):
        self.lake.manifests[self.lake.digest] = b"tampered"
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("does not match", result.stderr)

    def test_manifest_index_counts_shared_blobs_once(self):
        index_digest, index = encode_manifest(
            {
                "schemaVersion": 2,
                "manifests": [
                    {"digest": self.lake.digest, "size": len(self.lake.manifest_bytes)},
                    {"digest": self.lake.digest, "size": len(self.lake.manifest_bytes)},
                ],
            }
        )
        self.lake.manifests[index_digest] = index
        size = image_size(Config.from_env(self.env), index_digest)
        self.assertEqual(size, len(index) + len(self.lake.manifest_bytes) + 168)
        self.assertEqual(len(self.lake.manifest_reads), 2)

    def test_rootless_daemon_lifecycle(self):
        self.env["BUILDKITD_FLAGS"] = "--oci-worker-no-process-sandbox  --debug"
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        address = f"unix://{self.runtime}/buildkit/buildkitd.sock"
        daemon = self.daemon_trace()
        self.assertTrue(daemon["rootlesskit"])
        self.assertEqual(daemon["args"][:2], ["--oci-worker-no-process-sandbox", "--debug"])
        self.assertEqual(daemon["args"][2], "--config")
        self.assertTrue(daemon["args"][3].endswith("/buildkitd.toml"))
        self.assertEqual(daemon["args"][-1], f"--addr={address}")
        for name in ("SOURCE_TOKEN", "PUSH_TOKEN", "EVENTS_TOKEN", "BASE_REGISTRY_AUTH"):
            self.assertNotIn(name, daemon["env_keys"])
        self.assertEqual(self.trace()["address"], address)
        probes = (self.root / "probes.log").read_text().splitlines()
        self.assertTrue(probes)
        self.assertEqual(set(probes), {address})
        self.assertTrue(self.daemon_stopped())
        self.assertFalse(Path(address.removeprefix("unix://")).exists())

    def test_daemon_that_exits_fails_before_the_build(self):
        self.settings["daemon_exit"] = 3
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("BuildKit daemon exited with status 3", result.stderr)
        self.assertIn("buildkitd: fake buildkitd: cannot start the OCI worker", result.stderr)
        self.assertFalse((self.root / "trace.json").exists())
        logged = [line for value in self.event_values("log") for line in value["lines"]]
        self.assertIn("buildkitd: fake buildkitd: cannot start the OCI worker", logged)
        self.assertTrue(self.event_values("error"))
        self.assertFalse(self.lake.pushes)

    def test_daemon_that_never_listens_is_stopped(self):
        self.settings["daemon_never_ready"] = True
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("BuildKit daemon was not ready after 10 retries", result.stderr)
        self.assertEqual(len((self.root / "probes.log").read_text().splitlines()), 12)
        self.assertFalse((self.root / "trace.json").exists())
        self.assertTrue(self.daemon_stopped())

    def test_build_failure_still_stops_the_daemon(self):
        self.settings["exit_code"] = 5
        result = self.run_builder()
        self.assertIn("BuildKit exited with status 5", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertTrue(self.daemon_stopped())

    def test_relative_runtime_directory_is_rejected(self):
        self.env["XDG_RUNTIME_DIR"] = "run"
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("XDG_RUNTIME_DIR must be an absolute path", result.stderr)
        self.assertFalse((self.root / "daemon.json").exists())

    def test_signal_stops_child_and_removes_credentials(self):
        self.settings["hang"] = True
        process = subprocess.Popen(
            self.command(), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "trace.json").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / "trace.json").exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(stdout, "")
            self.assertIn("build interrupted", stderr)
            self.assertTrue(self.daemon_stopped())
            self.assertFalse(Path(self.trace()["credential_path"]).exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_empty_allow_list_rejects_all_arguments(self):
        with self.assertRaises(BuildError):
            build_args({"BUILD_ARG_ALLOW": "", "BUILD_ARGS": '{"APP_DEPS":"requests"}'})

    def test_event_transport_failure_is_best_effort(self):
        events = Events(self.env)
        with patch("lake_builder.events.request_bytes", side_effect=BuildError("offline")):
            events.phase("building")


if __name__ == "__main__":
    unittest.main()


class PlainHttpRegistryConfigTest(unittest.TestCase):
    def test_http_lake_registry_is_declared_plain_http_to_buildkitd(self):
        from types import SimpleNamespace

        from lake_builder.build import child_environment

        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(lake="http://lake.example.svc:80", registry="lake.example.svc")
            env = child_environment(
                {"PATH": "/bin", "BUILDKITD_FLAGS": "--oci-worker-no-process-sandbox"},
                directory,
                config,
            )
            flags = env["BUILDKITD_FLAGS"]
            self.assertTrue(flags.startswith("--oci-worker-no-process-sandbox --config "))
            written = open(os.path.join(directory, "buildkitd.toml")).read()
            self.assertIn('[registry."lake.example.svc"]', written)
            self.assertIn("http = true", written)

    def test_https_lake_registry_adds_no_config(self):
        from types import SimpleNamespace

        from lake_builder.build import child_environment

        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(lake="https://lake.example.test")
            env = child_environment({"PATH": "/bin"}, directory, config)
            self.assertNotIn("BUILDKITD_FLAGS", env)
            self.assertFalse(os.path.exists(os.path.join(directory, "buildkitd.toml")))
