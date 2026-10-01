# Build Job contract

One invocation performs one build. The image derives from
`moby/buildkit:v0.33.0-rootless`, adds Python and CA certificates, and returns to the
base image's numeric UID/GID `1000:1000`. The entrypoint rejects UID 0. Upstream
provides `buildctl-daemonless.sh` and RootlessKit; no Docker daemon or liquid client
is required. Python runtime code has no third-party dependencies.

This is the builder side of lake-builder#1. Lake's token minting, event forwarding,
and registry authorization belong to lake#295; Job creation and environment injection
belong to orama-operator#195.

## Environment

| Name | Contract |
| --- | --- |
| `LAKE_URL` | Lake REST base URL, including `http://` or `https://`; no embedded credentials, query or fragment. Use the in-cluster Service. |
| `SOURCE_TOKEN` | Required short-lived Bearer credential with read access to `REPOSITORY`. |
| `REPOSITORY` | Required repository identifier; URL-encoded on every source request. |
| `REVISION` | Required immutable revision selected by lake/operator. Sent unchanged on every request; the builder never resolves a moving branch. The operator must supply a pinned revision. |
| `CONTEXT` | Repository-relative POSIX directory, default `.`. Only files below this directory become build context. |
| `DOCKERFILE` | Repository-relative path, default `<CONTEXT>/Dockerfile`. A path outside the context is fetched separately. For generated content, use `inline:FROM ...`; a value containing a newline is also treated as inline text. |
| `TARGET` | Required `<registry-host>/<space>/<name>:<tag>`. Commas, schemes, credentials, traversal and BuildKit option injection are rejected. |
| `PUSH_TOKEN` | Required token accepted by the registry as Basic `lake:<token>`. |
| `BASE_REGISTRY_AUTH` | Optional path to a mounted `.dockerconfigjson`. When the file is readable, its `auths` entries supply private base-image pull credentials. An unset value or missing file preserves the push-only behavior. |
| `CACHE_REF` | Required registry cache reference, normally `<registry-host>/<space>/cache/<name>`. It must use the target's registry and space and must not overwrite the image target. |
| `BUILD_ARGS` | JSON object of string values; default `{}`. Values are passed as individual BuildKit arguments without shell evaluation. |
| `BUILD_ARG_ALLOW` | Comma-separated allowed names; default `PYTHON_VERSION,APP_DEPS,APP_RUNTIME_IMAGE`. An explicitly empty value allows no arguments. Unknown names or nonstring values fail before source retrieval and BuildKit startup. This is operator policy, not a field the source repository can modify. |
| `EVENTS_TOKEN` | Required Bearer credential scoped to this build's events. |
| `EVENTS_TOPIC` | `build-events.<build_id>`; the suffix supplies `build_id`. No request is sent to Zetta. |
| `BUILD_ID` | Alternative explicit build identifier. If both this and `EVENTS_TOPIC` are supplied they must agree. |
| `EVENTS_URL` | Legacy input is unused. Events always go to `LAKE_URL`, per the lake-mediated contract. |
| `BUILDKITD_FLAGS` | Optional operator-managed daemon flags understood by the upstream daemonless helper. Never populated from untrusted source. |

The registry transport follows `LAKE_URL`: HTTP enables `registry.insecure=true` for
image and cache operations and also writes a `buildkitd.toml` in the private credentials
directory declaring the lake registry host `http = true`, appended to `BUILDKITD_FLAGS` as
`--config` (the exporter option alone left cache import/export speaking HTTPS to the plain
HTTP in-cluster registry); HTTPS retains certificate verification and adds no config. The operator must
provide REST and registry Service endpoints using the same transport. HTTP credentials
are suitable only inside the isolated cluster network described below.

The builder creates a private per-run `DOCKER_CONFIG` directory regardless of any inherited
value. Its `config.json` has mode `0600`. Without `BASE_REGISTRY_AUTH`, it contains:

```json
{"auths":{"<registry-host>":{"auth":"<base64 of lake:PUSH_TOKEN>"}}}
```

When `BASE_REGISTRY_AUTH` names a readable file, the builder copies only that Docker
config's `auths` object into the private config, then writes the lake registry entry
shown above. The scoped lake push entry therefore replaces any imported entry with the
same registry host. Other top-level settings, including credential-store configuration,
are not copied. A missing file is treated like an unset value. A readable file with
invalid JSON or without an `auths` object fails the build before BuildKit starts.

This directory is outside both BuildKit local inputs. The source token, push token and
event token are not passed in the child process environment or its arguments. BuildKit
receives push and optional base-image pull credentials through the Docker config. The
config path and `BASE_REGISTRY_AUTH` are not passed to BuildKit's child environment.
Imported credential values are added to progress-log redaction and are never written to
events or stderr. No `--secret` or `--ssh` inputs are created. Build arguments are not
secret channels: they may appear in build output, image history and cache, so the operator
must not pass credentials as argument values.

## Source materialization

The builder sends these requests with `Authorization: Bearer SOURCE_TOKEN`:

```text
GET /v1/repository_tree?repository=...&revision=...&recursive=true
GET /v1/repository_file?repository=...&revision=...&path=...
```

Responses use lake's `{ "ok": true, "value": ... }` envelope. The tree is
`value.items`, each entry containing a repository-relative `path`, `kind` (`file`
or `blob`, or omitted), and optional `mode`. Directories are skipped. Text files use
`value.contents`; `value.content_base64` additionally supports exact binary bytes.
Only the chosen context and an explicitly requested Dockerfile are downloaded.

Paths containing `..`, absolute paths, `.git`, symlinks, special files, duplicate
paths and conflicting paths are rejected. Regular modes `100644` and `100755` are
preserved. Redirects are refused and host proxy environment variables are ignored,
so source and registry credentials cannot be forwarded to a redirect destination.
REST responses are limited to 64 MiB each; larger source files fail explicitly.

The context preserves its `.dockerignore`. The Dockerfile is placed in a separate
local input named `Dockerfile`. A corresponding `<original-name>.dockerignore`
inside the selected context is copied as `Dockerfile.dockerignore` beside it.
An external Dockerfile's sibling ignore file is not implicitly fetched; use the
context's `.dockerignore` for that case. Repository files themselves are untrusted;
the builder does not scan committed content for secrets.

## Execution and result

The entrypoint invokes `buildctl-daemonless.sh build` with the `dockerfile.v0`
frontend, `platform=linux/amd64`, `type=image,push=true`, and registry cache import
and `mode=max` export. Each allow-listed build argument becomes one `--opt` value.
The helper starts and tears down BuildKit for this invocation. BuildKit output goes
to stderr, leaving stdout for the result. SIGINT/SIGTERM terminate the child process
group, remove temporary inputs and credentials, and produce an error event.

On success, the builder reads `containerimage.digest` from BuildKit's metadata file,
fetches the pushed registry manifest by digest with the same Basic credential, and
checks its SHA-256. It follows image indexes and verifies child manifest sizes and
digests. `size` is the sum of unique manifest, config and compressed layer descriptor
bytes reachable from the top-level digest, counted once per digest. It is neither
the unpacked filesystem size nor just the top-level manifest size. Blob sizes come
from descriptors; layers are not downloaded again. Invalid metadata, manifest reads
or digest verification fail the build, even if a push has already occurred.

The final stdout line is:

```json
{"digest":"sha256:<64 lowercase hex characters>","size":123}
```

Failures exit nonzero and never print a success result. Lake is the authority for
token scope: the builder checks cache/target consistency, while registry requests
outside the token's `<space>/` prefix must receive `403 DENIED` from lake.

## Progress

Events are POSTed to `/v1/publish_build_event` with `Authorization: Bearer EVENTS_TOKEN`.
Every body contains `build_id` and `type`. Phase events use `fetching`, `building`,
`pushing` (when BuildKit reports a push), and `done`. Log events contain `lines`;
done events contain `digest` and `size`; error events contain `message`. The builder
does not invent step totals from BuildKit's plain text output.

Log batches are at most 64 KiB of encoded JSON and are sent no more than once per
second. Individual lines are bounded and overflowing event buffers are omitted with
a count; regular log lines also remain in Job stderr. Exact token strings and the
encoded registry credential are redacted. Event POSTs have a bounded timeout and
failures never change the build result. The last log batch precedes the terminal
event. A final done event is attempted before printing the stdout result.

## Operator isolation

The Job must use `runAsNonRoot`, UID/GID 1000, a writable ephemeral directory for
`/home/user/.local/share/buildkit`, and the rootless BuildKit permissions required by
its kernel. Do not mount a service-account token. Apply per-space network isolation,
resource limits and a deadline, as specified by orama-operator#195. Builds execute
untrusted Dockerfiles and may need access to approved package indexes.

The image does not enable `--oci-worker-no-process-sandbox` by default. Upstream
documents its process-isolation tradeoff; if the operator requires that flag for
Kubernetes, it must provide the surrounding dedicated Job/namespace isolation.
See upstream's [rootless deployment documentation](https://github.com/moby/buildkit/blob/v0.33.0/docs/rootless.md)
and [daemonless helper](https://github.com/moby/buildkit/blob/v0.33.0/examples/buildctl-daemonless/buildctl-daemonless.sh).
