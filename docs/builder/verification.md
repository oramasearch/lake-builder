# Verification

## Native proof

Use Python 3.11 or newer and the pinned Ruff version in `requirements-dev.txt`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Tests run one suite at a time and create temporary state only under `.proof/` in the
worktree. They require an ordinary non-root account. There are no compiled static
or shared-library suites in this Python repository.

`tests/test_builder.py` serves lake's source and event REST endpoints on loopback.
Its registry fake checks the exact Basic `lake:<token>` credential, permits only
the token's `alpha/` prefix, and returns `403 DENIED` for `beta/`. The CLI runs in
a separate process against that server. `tests/fixtures/buildctl-daemonless.py`
records the build inputs, attempts target/cache writes, and emits metadata pointing
to the fixture's OCI manifest. It is a process fixture, not an image builder.

Coverage includes pinned revision propagation, context filtering, Dockerfiles outside
the context, inline Dockerfiles, binary and executable files, source authentication,
redirect refusal, unsafe paths, build-argument policy, cache scope, registry Basic
authentication, out-of-prefix refusal, metadata/digest verification, unique byte
accounting across an index, event failure tolerance, log size/rate limits and
credential redaction, child credential isolation, SIGTERM cleanup and no success
JSON on failure.

## Manual Linux image build

The native fake proves the client contract, not Linux namespaces, rootless mounts,
image construction, the base image packages, or registry interoperability. The
image build and a real rootless push must be checked on an approved Linux/cluster
lane after review. No image is built or published by the native suite.

1. Have cluster BuildKit build this repository's `Dockerfile` at the reviewed commit
   and publish `oramasearch/lake-builder` privately. Verify an anonymous image read
   still returns 404. Do not publish from the local development host.
2. In a disposable lake repository, commit `Dockerfile` containing `FROM scratch`
   and `COPY hello /hello`, plus a text file `hello`. Record its immutable revision.
3. Run the image as UID 1000 with the environment in [the contract](contract.md),
   targeting `<registry-service>/<test-space>/tiny:<tag>` and its per-space cache.
   Use a dedicated Job with the rootless mounts, kernel permissions, deadline and
   network restrictions required by the operator. Test with lake#295's registry,
   or `registry:2` behind an authorization fixture that enforces the same Basic
   credential and prefix rules; plain `registry:2` alone does not enforce the prefix.
4. Require exit 0, fetching/building/pushing/done events, a final digest/size JSON
   line, and readable image and cache manifests. Pull the digest on the test lane
   and confirm `/hello` contains the original text. Run the Job again to exercise
   registry cache import.
5. Keep the same push credential and change target and cache to a different space.
   Require registry `403 DENIED`, nonzero exit, an error event, and no final success
   JSON. Also try a disallowed build argument and confirm BuildKit never starts.

Record the image digest, Job outcome and registry refusal in the review proof.
When this run is unavailable on the local host, report
`deferred: no Docker on this host`. If the shared Linux proof helper/lane is
unavailable, also report `Linux proof deferred`. Neither statement is a passing
image-build result.
