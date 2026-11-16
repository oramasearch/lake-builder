# SPDX-License-Identifier: MIT

"""Validate operator input before fetching source or starting BuildKit."""

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit


class BuildError(Exception):
    """A build failure safe to report after credential redaction."""


def required(env, name):
    value = env.get(name, "")
    if not value or "\x00" in value:
        raise BuildError(f"{name} is required and must not contain NUL")
    return value


def relative_path(value, name, allow_root=False):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise BuildError(f"{name} must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise BuildError(f"{name} must not escape the repository or include .git")
    if path == PurePosixPath(".") and not allow_root:
        raise BuildError(f"{name} must name a file")
    return path


def lake_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise BuildError("LAKE_URL must be an HTTP(S) URL without credentials, query or fragment")
    return value.rstrip("/")


def image_ref(value, name, require_tag=False):
    # Reject BuildKit CSV option injection as well as ambiguous registry/path syntax.
    match = re.fullmatch(
        r"(?P<host>[a-z0-9.-]+(?::[0-9]+)?)/"
        r"(?P<path>[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+)"
        r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?",
        value,
    )
    if not match or (require_tag and not match["tag"]):
        raise BuildError(
            f"{name} must be <registry-host>/<space>/<name>" + (":<tag>" if require_tag else "")
        )
    return match["host"], match["path"]


def build_args(env):
    try:
        values = json.loads(env.get("BUILD_ARGS", "{}"))
    except (ValueError, TypeError) as error:
        raise BuildError("BUILD_ARGS must be a JSON object of string values") from error
    allowed = {
        name.strip()
        for name in env.get("BUILD_ARG_ALLOW", "PYTHON_VERSION,APP_DEPS,APP_RUNTIME_IMAGE").split(
            ","
        )
        if name.strip()
    }
    if not isinstance(values, dict):
        raise BuildError("BUILD_ARGS must be a JSON object of string values")
    for name, value in values.items():
        if name not in allowed or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise BuildError(f"build argument {name!r} is not in BUILD_ARG_ALLOW")
        if not isinstance(value, str) or "\x00" in value:
            raise BuildError(f"build argument {name!r} must be a string without NUL")
    return values


def build_id(env):
    identifier = env.get("BUILD_ID", "")
    topic = env.get("EVENTS_TOPIC", "")
    if topic:
        if not topic.startswith("build-events.") or not topic.removeprefix("build-events."):
            raise BuildError("EVENTS_TOPIC must be build-events.<build_id>")
        from_topic = topic.removeprefix("build-events.")
        if identifier and identifier != from_topic:
            raise BuildError("BUILD_ID and EVENTS_TOPIC disagree")
        identifier = from_topic
    if not identifier:
        raise BuildError("BUILD_ID or EVENTS_TOPIC is required")
    return identifier


@dataclass(frozen=True)
class Config:
    lake: str
    source_token: str
    repository: str
    revision: str
    context: PurePosixPath
    dockerfile: PurePosixPath | None
    inline_dockerfile: str | None
    target: str
    registry: str
    image: str
    push_token: str
    cache: str
    arguments: dict[str, str]

    @classmethod
    def from_env(cls, env):
        arguments = build_args(env)
        target = required(env, "TARGET")
        registry, image = image_ref(target, "TARGET", require_tag=True)
        cache = required(env, "CACHE_REF")
        cache_registry, cache_image = image_ref(cache, "CACHE_REF")
        if cache_registry != registry or cache_image.split("/")[0] != image.split("/")[0]:
            raise BuildError("CACHE_REF must use TARGET's registry and space")
        if cache == target or (cache_image == image and ":" not in cache.rsplit("/", 1)[1]):
            raise BuildError("CACHE_REF must not overwrite TARGET")
        context = relative_path(env.get("CONTEXT", "."), "CONTEXT", allow_root=True)
        dockerfile = env.get("DOCKERFILE", str(context / "Dockerfile"))
        inline = None
        if dockerfile.startswith("inline:"):
            inline = dockerfile.removeprefix("inline:")
        elif "\n" in dockerfile:
            inline = dockerfile
        if inline is not None and (not inline.strip() or "\x00" in inline):
            raise BuildError("inline DOCKERFILE must be nonempty text without NUL")
        return cls(
            lake=lake_url(required(env, "LAKE_URL")),
            source_token=required(env, "SOURCE_TOKEN"),
            repository=required(env, "REPOSITORY"),
            revision=required(env, "REVISION"),
            context=context,
            dockerfile=relative_path(dockerfile, "DOCKERFILE") if inline is None else None,
            inline_dockerfile=inline,
            target=target,
            registry=registry,
            image=image,
            push_token=required(env, "PUSH_TOKEN"),
            cache=cache,
            arguments=arguments,
        )
