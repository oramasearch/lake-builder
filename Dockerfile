# Pinned by digest (lake-builder#10): the builder holds every build's tokens, so a moved tag must
# never change what runs. v0.33.0-rootless, the multi-platform index digest.
FROM moby/buildkit:v0.33.0-rootless@sha256:80b15f0735e87bab7bf59ec4d695dfb4a7cfb25521cf56dc75d6f256285b63ef

USER 0
# The base ships Alpine 3.23.5 (its os-release; apk reads the v3.23 main and community
# repositories). The packages are pinned to the versions in that branch's APKINDEX (lake-builder#10),
# so a changed package fails the build instead of entering it. When v3.23 publishes a newer
# revision the old one leaves the mirror and this line must be moved, in a reviewed commit.
RUN apk add --no-cache python3=3.12.16-r0 ca-certificates=20261020-r0
COPY src/ /opt/lake-builder/
ENV PYTHONPATH=/opt/lake-builder
USER 1000:1000
ENTRYPOINT ["python3", "-m", "lake_builder"]
