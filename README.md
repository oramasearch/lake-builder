# lake-builder

The image a lake in-cluster build Job runs: rootless BuildKit in daemonless mode, building a lake repository at a pinned revision and pushing to lake's registry with a scoped token.

Its image is published as `oramasearch/lake-builder` on Docker Hub.

The entrypoint fetches a pinned repository revision through lake REST. It starts an
ephemeral rootless BuildKit v0.33.0 daemon, builds for `linux/amd64`, and pushes the image
and registry cache with a space-scoped credential. The image is `scratch` with static
BuildKit, RootlessKit and a musl CPython; there is no shell or GPL userland in it (see
[image contents](docs/builder/image.md) for every file and its licence). It runs as UID/GID `1000:1000`, publishes
best-effort progress through lake, and prints `{ "digest": "sha256:...", "size": N }`
as its final stdout line. `size` counts the unique compressed registry content reachable
from that digest, including manifests. An optional mounted Docker config supplies private
base-image pull credentials without overriding the lake registry push credential.

See the [builder contract](docs/builder/contract.md) for the environment, security
boundaries and operator requirements, and [verification](docs/builder/verification.md)
for native tests, the image contents check (`make image-check`) and the manual
image-build check. Image publication is performed by
cluster BuildKit after review; this repository does not publish images from local tests.

Native development requires Python 3.11 or newer. Runtime code uses only the standard
library; Ruff is pinned for development formatting and lint checks.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
make proof
```

The native suite uses a local fake lake/registry HTTP server and a fake BuildKit
process. A real rootless BuildKit image build remains a separate Linux verification.
The suite runs on macOS and Linux with Python 3.11 or newer; it does not build or publish the image.
