# Pinned by digest (lake-builder#10): the builder holds every build's tokens, so a moved tag must
# never change what runs. v0.33.0-rootless, the multi-platform index digest.
FROM moby/buildkit:v0.33.0-rootless@sha256:80b15f0735e87bab7bf59ec4d695dfb4a7cfb25521cf56dc75d6f256285b63ef

USER 0
RUN apk add --no-cache python3 ca-certificates
COPY src/ /opt/lake-builder/
ENV PYTHONPATH=/opt/lake-builder
USER 1000:1000
ENTRYPOINT ["python3", "-m", "lake_builder"]
