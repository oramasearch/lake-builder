# AGENTS.md: working in lake-builder

The image a lake in-cluster build Job runs: rootless BuildKit in daemonless mode, building a lake repository at a pinned revision and pushing to lake's registry with a scoped token. The contract is [`docs/builder/contract.md`](docs/builder/contract.md); how it is verified is [`docs/builder/verification.md`](docs/builder/verification.md).

## Build and test
```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
make proof   # ruff check, ruff format --check, and the unittest suite
```
The suite runs against a local fake lake, registry and BuildKit, and does not build an image. Say in the pull request which tests you ran.

## Non-negotiables
- **No AI attribution** anywhere: no Co-Authored-By, no "Generated with", no mention of AI in commits, PRs, tags or release notes.
- **No time or size estimates.** Do the work; report concrete results.
- **Docs in the same change:** update README.md (and any docs page) with every code change.
- **Feature branch + PR against `dev`.** Never push to `main`; it holds the last release. PRs reference their issue with `Refs #N`, never an auto-close keyword. Merge commits, never squash.
- Never run a formatter over files you did not otherwise change.
- Never write the word "seam": name the mechanism (interface, extension point, boundary).
- Report vulnerabilities privately, as `SECURITY.md` describes.
