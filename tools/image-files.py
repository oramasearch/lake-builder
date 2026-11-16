#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check that every file in a built lake-builder image belongs to a known, licensed component.

Usage: python3 tools/image-files.py IMAGE [--list]

The image's filesystem is exported with `docker create` + `docker export` (nothing runs).
Each entry must match one component below; an unknown file fails the check, so a change to
the Dockerfile that lets an unlisted file into the image is caught before release. The
inventory mirrors docs/builder/image.md; update both together.
"""

import argparse
import fnmatch
import subprocess
import sys
import tarfile
import tempfile

# (component, licence, path patterns). The first matching component wins.
COMPONENTS = [
    ("lake-builder", "MIT", ["opt/lake-builder", "opt/lake-builder/*"]),
    (
        "image accounts and directories (lake-builder)",
        "MIT",
        [
            "etc",
            "etc/passwd",
            "etc/group",
            "etc/subuid",
            "etc/subgid",
            "home",
            "home/user",
            "home/user/*",
            "run",
            "run/user",
            "run/user/1000",
            "tmp",
            "opt",
            "usr",
            "usr/bin",
            "usr/lib",
            "lib",
        ],
    ),
    (
        "CPython (python-build-standalone, musl)",
        "Python-2.0 AND CNRI-Python; linked in: OpenSSL Apache-2.0, libffi MIT, zlib Zlib, "
        "bzip2 bzip2-1.0.6, xz 0BSD, mpdecimal BSD-2-Clause, expat MIT, libedit BSD-3-Clause, "
        "ncurses X11, libuuid BSD-3-Clause, SQLite public domain",
        ["opt/python", "opt/python/*"],
    ),
    ("BuildKit buildkitd, buildctl", "Apache-2.0", ["usr/bin/buildkitd", "usr/bin/buildctl"]),
    (
        "runc (buildkit-runc, static)",
        "Apache-2.0; statically links libseccomp LGPL-2.1 (accepted exception)",
        ["usr/bin/buildkit-runc"],
    ),
    ("RootlessKit", "Apache-2.0", ["usr/bin/rootlesskit"]),
    (
        "shadow newuidmap, newgidmap (Alpine shadow-subids)",
        "BSD-3-Clause AND ISC AND 0BSD AND Unlicense (per-file audit)",
        ["usr/bin/newuidmap", "usr/bin/newgidmap"],
    ),
    ("libbsd", "BSD-3-Clause", ["usr/lib/libbsd.so.0*"]),
    (
        "libmd",
        "BSD-3-Clause AND BSD-2-Clause AND ISC AND Beerware AND Public Domain",
        ["usr/lib/libmd.so.0*"],
    ),
    ("musl", "MIT", ["lib/ld-musl-*.so.1", "lib/libc.musl-*.so.1"]),
    (
        "Mozilla CA certificate bundle (Alpine ca-certificates-bundle)",
        "MPL-2.0 AND MIT",
        ["etc/ssl", "etc/ssl/*"],
    ),
    (
        "container runtime mount points (not in the image layers)",
        "n/a",
        [
            ".dockerenv",
            "dev",
            "dev/*",
            "proc",
            "sys",
            "etc/hostname",
            "etc/hosts",
            "etc/resolv.conf",
            "etc/mtab",
        ],
    ),
]

FORBIDDEN = ("GPL", "LGPL", "AGPL")


def component_for(name):
    for component, licence, patterns in COMPONENTS:
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
            return component, licence
    return None


def export(image, destination):
    container = subprocess.run(
        ["docker", "create", image], check=True, capture_output=True, text=True
    ).stdout.strip()
    try:
        with open(destination, "wb") as archive:
            subprocess.run(["docker", "export", container], check=True, stdout=archive)
    finally:
        subprocess.run(["docker", "rm", container], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image")
    parser.add_argument("--list", action="store_true", help="print every file and its component")
    options = parser.parse_args()
    counts = {}
    unknown = []
    with tempfile.TemporaryDirectory() as directory:
        archive = f"{directory}/rootfs.tar"
        export(options.image, archive)
        with tarfile.open(archive) as tar:
            for member in tar:
                name = member.name.rstrip("/")
                match = component_for(name)
                if match is None:
                    unknown.append(name)
                    continue
                counts[match] = counts.get(match, 0) + 1
                if options.list:
                    print(f"{name}\t{match[0]}")
    for (component, licence), count in counts.items():
        flagged = " [flagged]" if any(word in licence for word in FORBIDDEN) else ""
        print(f"{count:5d}  {component}: {licence}{flagged}")
    if unknown:
        print(f"\n{len(unknown)} file(s) belong to no listed component:", file=sys.stderr)
        for name in unknown:
            print(f"  {name}", file=sys.stderr)
        return 1
    print(f"\nall {sum(counts.values())} entries belong to a listed component")
    return 0


if __name__ == "__main__":
    sys.exit(main())
