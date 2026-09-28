FROM moby/buildkit:v0.33.0-rootless

USER 0
RUN apk add --no-cache python3 ca-certificates
COPY src/ /opt/lake-builder/
ENV PYTHONPATH=/opt/lake-builder
USER 1000:1000
ENTRYPOINT ["python3", "-m", "lake_builder"]
