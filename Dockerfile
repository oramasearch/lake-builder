# syntax=docker/dockerfile:1.7
#
# The image is scratch plus an allow-list of files; nothing else from the base reaches it.
# docs/builder/image.md lists every file, where it comes from, its licence, and how to move a pin.

# Assembly stage only. Pinned by the multi-platform index digest of v0.33.0-rootless: the builder
# holds every build's tokens, so a moved tag must never change what runs. This stage's BusyBox,
# git, fuse-overlayfs, QEMU and other Alpine packages are not copied out.
FROM moby/buildkit:v0.33.0-rootless@sha256:80b15f0735e87bab7bf59ec4d695dfb4a7cfb25521cf56dc75d6f256285b63ef AS rootfs

# python-build-standalone CPython on musl (no readline, no gdbm), pinned by release and SHA-256
# from the release's SHA256SUMS.
ARG PYTHON_RELEASE=20261110
ARG PYTHON_VERSION=3.12.16
ARG PYTHON_SHA256_AMD64=e32685cf248d2f33b249816120774143b4b57578cb30de5b8dba3e54963765c3
ARG PYTHON_SHA256_ARM64=64bb304e59d5d6ed5eaae24e95e743a2420a8897d2f6c854b1438ff9cb47e432
ARG TARGETARCH

USER 0
RUN <<'EOT'
set -eu
case "$TARGETARCH" in
  amd64) triple=x86_64; sum=$PYTHON_SHA256_AMD64 ;;
  arm64) triple=aarch64; sum=$PYTHON_SHA256_ARM64 ;;
  *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;;
esac
base=https://github.com/astral-sh/python-build-standalone/releases/download/$PYTHON_RELEASE
archive="cpython-$PYTHON_VERSION%2B$PYTHON_RELEASE-$triple-unknown-linux-musl-install_only_stripped.tar.gz"
wget -q -O /tmp/python.tar.gz "$base/$archive"
echo "$sum  /tmp/python.tar.gz" | sha256sum -c -

mkdir -p /out/opt /out/usr/bin /out/usr/lib /out/lib /out/etc/ssl/certs /out/tmp \
  /out/run/user/1000 /out/home/user/.local/tmp /out/home/user/.local/share/buildkit
tar -xzf /tmp/python.tar.gz -C /out/opt

# Keep the interpreter and the standard library; drop Tcl/Tk, IDLE, tests, pip and headers, and
# _dbm (it links Berkeley DB under the Sleepycat licence; the dbm package falls back without it).
cd /out/opt/python
minor=${PYTHON_VERSION%.*}
rm -rf include share lib/pkgconfig lib/*.a lib/itcl* lib/tcl* lib/tk* lib/thread* \
  lib/libtcl* bin/2to3* bin/idle3* bin/pip* bin/pydoc3* bin/python*-config \
  lib/python$minor/tkinter lib/python$minor/idlelib lib/python$minor/turtledemo \
  lib/python$minor/test lib/python$minor/ensurepip lib/python$minor/lib2to3 \
  lib/python$minor/config-$minor-* lib/python$minor/site-packages/pip* \
  lib/python$minor/lib-dynload/_tkinter* lib/python$minor/lib-dynload/_dbm.*
find . -name __pycache__ -prune -exec rm -rf {} +

# BuildKit, its runc and RootlessKit are static Go binaries (Apache-2.0).
cp /usr/bin/buildkitd /usr/bin/buildctl /usr/bin/buildkit-runc /usr/bin/rootlesskit /out/usr/bin/
# The musl loader serves Python and the uid-map helpers (MIT); libbsd and libmd serve the helpers.
cp /lib/ld-musl-$triple.so.1 /out/lib/
ln -s ld-musl-$triple.so.1 /out/lib/libc.musl-$triple.so.1
cp -P /usr/lib/libbsd.so.0* /usr/lib/libmd.so.0* /out/usr/lib/
cp /etc/ssl/certs/ca-certificates.crt /out/etc/ssl/certs/
ln -s certs/ca-certificates.crt /out/etc/ssl/cert.pem

printf 'root:x:0:0:root:/root:/sbin/nologin\nuser:x:1000:1000:user:/home/user:/sbin/nologin\n' \
  >/out/etc/passwd
printf 'root:x:0:\nuser:x:1000:\n' >/out/etc/group
echo user:100000:65536 >/out/etc/subuid
echo user:100000:65536 >/out/etc/subgid
chmod 1777 /out/tmp
chown -R 1000:1000 /out/run/user/1000 /out/home/user
chmod 0700 /out/run/user/1000
EOT

FROM scratch
COPY --from=rootfs /out/ /
# newuidmap/newgidmap carry file capabilities (cap_setuid / cap_setgid); COPY keeps them.
COPY --from=rootfs /usr/bin/newuidmap /usr/bin/newgidmap /usr/bin/
COPY src/ /opt/lake-builder/
ENV PATH=/usr/bin:/opt/python/bin \
    HOME=/home/user \
    USER=user \
    XDG_RUNTIME_DIR=/run/user/1000 \
    TMPDIR=/home/user/.local/tmp \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PYTHONPATH=/opt/lake-builder \
    PYTHONDONTWRITEBYTECODE=1
# Kubernetes runAsNonRoot requires a numeric user.
USER 1000:1000
VOLUME /home/user/.local/share/buildkit
ENTRYPOINT ["/opt/python/bin/python3", "-m", "lake_builder"]
