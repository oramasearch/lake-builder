# Contributing to lake-builder

lake-builder is the image a lake in-cluster build Job runs: rootless BuildKit in daemonless mode,
building a lake repository at a pinned revision and pushing to lake's registry with a scoped token.
The contract is [`docs/builder/contract.md`](docs/builder/contract.md); how it is verified is
[`docs/builder/verification.md`](docs/builder/verification.md).

## Build and test

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
make proof
```

The native suite runs against a local fake lake, registry and BuildKit, and does not build an
image. Say in your pull request which tests you ran.

## Pull requests

- Branch from `dev` and open the pull request against `dev`; `main` holds the last release.
- Update `README.md` and any page under `docs/` in the same pull request as the code.
- Reference the issue a pull request works on with `Refs #N`.
- Do not run a formatter over files you did not otherwise change.

## Security

Report a vulnerability privately, as [`SECURITY.md`](SECURITY.md) describes, never in a public issue.
