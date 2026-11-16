# Image contents and licences

The lake-builder image is `scratch` plus an allow-list of files. No shell, package manager,
BusyBox, git or other Alpine userland reaches it. This page lists every component, where it
comes from, its licence, and how to move each pin. `tools/image-files.py` checks a built image
against the same list.

## Build

The `Dockerfile` has two stages:

1. `rootfs` (assembly only, never shipped) starts from
   `moby/buildkit:v0.33.0-rootless@sha256:80b15f0735e87bab7bf59ec4d695dfb4a7cfb25521cf56dc75d6f256285b63ef`.
   It downloads the pinned CPython tarball and checks its SHA-256. It then copies the files
   listed below into `/out` and writes the account files.
2. The final stage is `FROM scratch`. It copies `/out`, then `newuidmap`/`newgidmap` (so their
   file capabilities `cap_setuid`/`cap_setgid` are kept), then `src/`.

The image supports `linux/amd64` and `linux/arm64`. The builds it runs always target
`linux/amd64` (see the [contract](contract.md)).

## Components

| Path | Component | Source | Licence |
| --- | --- | --- | --- |
| `/opt/lake-builder/` | lake-builder entrypoint | this repository | MIT |
| `/etc/passwd`, `/etc/group`, `/etc/subuid`, `/etc/subgid`, directories | image accounts (`user` 1000:1000, subordinate IDs `100000:65536`) | written by the `Dockerfile` | MIT |
| `/opt/python/` | CPython 3.12.16, python-build-standalone `20261110`, `*-unknown-linux-musl-install_only_stripped` | [release](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110), SHA-256 in the `Dockerfile` | Python-2.0 AND CNRI-Python |
| (linked into `libpython`) | OpenSSL, libffi, zlib, bzip2, xz, mpdecimal, expat, libedit, ncurses, libuuid, SQLite | same release (`PYTHON.json` licence metadata) | Apache-2.0, MIT, Zlib, bzip2-1.0.6, 0BSD, BSD-2-Clause, MIT, BSD-3-Clause, X11, BSD-3-Clause, public domain |
| `/usr/bin/buildkitd`, `/usr/bin/buildctl` | BuildKit v0.33.0, static | pinned BuildKit image | Apache-2.0 |
| `/usr/bin/buildkit-runc` | runc 1.4.3, static | pinned BuildKit image | Apache-2.0, **statically links libseccomp 2.6.0 (LGPL-2.1)**: see below |
| `/usr/bin/rootlesskit` | RootlessKit 3.0.1, static | pinned BuildKit image | Apache-2.0 |
| `/usr/bin/newuidmap`, `/usr/bin/newgidmap` | shadow 4.18.0 (Alpine `shadow-subids`) | pinned BuildKit image | BSD-3-Clause per Alpine metadata; **per-file audit pending**: see below |
| `/usr/lib/libbsd.so.0*` | libbsd 0.12.2 (needed by the uid-map helpers) | pinned BuildKit image | BSD-3-Clause |
| `/usr/lib/libmd.so.0*` | libmd 1.1.0 (needed by libbsd) | pinned BuildKit image | BSD-3-Clause AND BSD-2-Clause AND ISC AND Beerware AND public domain |
| `/lib/ld-musl-*.so.1`, `/lib/libc.musl-*.so.1` | musl 1.2.5 (loader and libc for Python and the uid-map helpers) | pinned BuildKit image | MIT |
| `/etc/ssl/certs/ca-certificates.crt`, `/etc/ssl/cert.pem` | Mozilla CA bundle (Alpine `ca-certificates-bundle`) | pinned BuildKit image | MPL-2.0 AND MIT (certificate data) |

The `Dockerfile` removes these parts of the Python tarball: Tcl/Tk (`tkinter`, `_tkinter`,
the Tcl libraries), IDLE, `turtledemo`, the test suite, `ensurepip`, `lib2to3`, pip, headers,
static libraries and `_dbm`. `_dbm` links Berkeley DB under the Sleepycat licence; without it,
Python's `dbm` falls back to `dbm.dumb`. The entrypoint uses none of these modules.

These upstream files are not in the image: BusyBox and `buildctl-daemonless.sh`, git, OpenSSH,
fuse-overlayfs and fuse3, pigz, xz, the BuildKit QEMU emulators (GPL-2.0) and the BuildKit CNI
plugins.

### Open licence items

- **libseccomp in `buildkit-runc` (LGPL-2.1).** The decision was to keep seccomp: without
  it, the untrusted Dockerfile `RUN` steps run with no syscall filter. runc's own source is
  Apache-2.0, and libseccomp is linked statically. This is recorded as an exception for
  review. The alternative is runc built with `BUILDTAGS=""` (no libseccomp, no seccomp).
- **`newuidmap`/`newgidmap`.** Alpine's metadata marks `shadow-subids` BSD-3-Clause, but the
  shadow source tree also contains GPL-2.0-or-later files. These two binaries still need a
  check of which source files they compile from. A follow-up issue tracks it.
- **CA bundle (MPL-2.0).** This is certificate data under a weak, file-level copyleft. It is
  not GPL or LGPL.

## Behaviour that changed with scratch

- The entrypoint starts the daemon itself (`rootlesskit buildkitd --addr=unix://$XDG_RUNTIME_DIR/buildkit/buildkitd.sock`).
  It waits until `buildctl debug workers` answers, with the same retries and back-off as
  upstream's `buildctl-daemonless.sh`. It runs the build and then stops the daemon's process
  group. See the [contract](contract.md#execution-and-result).
- There is no git binary, so a Dockerfile `ADD` of a git URL fails. Lake builds use local
  contexts fetched through lake REST.
- There is no QEMU, so a stage for a foreign platform cannot `RUN`. Builds target
  `linux/amd64`, so the Job must run on amd64 nodes.
- There is no fuse-overlayfs. BuildKit's `auto` snapshotter uses kernel overlayfs in rootless
  mode (Linux 5.11 or newer) and otherwise falls back to `native`.
- There is no pigz or xz. BuildKit uses its built-in Go compression.

## Check a built image

```sh
docker buildx build --load -t lake-builder:check .
python3 tools/image-files.py lake-builder:check          # summary per component
python3 tools/image-files.py lake-builder:check --list   # every file and its component
```

The tool exports the image filesystem with `docker create` and `docker export`, so nothing
runs. Every entry must match a listed component, and an unknown file makes it exit 1. The
upstream BuildKit image fails this check with more than 900 unknown files, BusyBox among them.
`make image-check` builds the image and runs the tool.

## Moving a pin

- **BuildKit:** read the new tag's multi-platform index digest with
  `docker buildx imagetools inspect moby/buildkit:<tag>-rootless`. Update the `FROM` line
  and the versions on this page. Then rebuild, run `make image-check`, and run the manual
  Linux check in [verification](verification.md). Also check that the copied file names
  (`buildkit-runc`, `rootlesskit`, `libbsd`, `libmd`, the musl loader) still exist.
- **Python:** pick a python-build-standalone release and take both `*-musl-install_only_stripped`
  checksums from its `SHA256SUMS`. Update `PYTHON_RELEASE`, `PYTHON_VERSION` and the two
  `PYTHON_SHA256_*` arguments. Then read the release's `PYTHON.json` licences: any new linked
  library goes on this page and in `tools/image-files.py`.
