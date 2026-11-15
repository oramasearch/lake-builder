# lake-builder

The image a lake in-cluster build Job runs: rootless BuildKit in daemonless mode, building a lake repository at a pinned revision and pushing to lake's registry with a scoped token. Epic: oramasearch/lake#297.

Private. Its image is published **private** as `oramasearch/lake-builder` on Docker Hub.

The entrypoint fetches a pinned repository revision through lake REST, builds for
`linux/amd64` using `moby/buildkit:v0.33.0-rootless`, pinned by digest in the Dockerfile, and pushes the image and registry
cache with a space-scoped credential. It runs as UID/GID `1000:1000`, publishes
best-effort progress through lake, and prints `{ "digest": "sha256:...", "size": N }`
as its final stdout line. `size` counts the unique compressed registry content reachable
from that digest, including manifests. An optional mounted Docker config supplies private
base-image pull credentials without overriding the lake registry push credential.

See the [builder contract](docs/builder/contract.md) for the environment, security
boundaries and operator requirements, and [verification](docs/builder/verification.md)
for native tests and the manual image-build check. Image publication is performed by
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
GitHub Actions runs this contract suite on macOS/Python 3.14 and Linux/Python 3.11;
it does not build or publish the private image.
