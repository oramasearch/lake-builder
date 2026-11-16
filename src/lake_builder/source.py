# SPDX-License-Identifier: MIT

"""Materialize only the requested source, with credentials kept elsewhere."""

import base64
import binascii
from urllib.parse import urlencode

from .config import BuildError, relative_path
from .http import lake_get


def source_url(config, operation, **params):
    query = {"repository": config.repository, "revision": config.revision, **params}
    return f"{config.lake}/v1/{operation}?{urlencode(query)}"


def file_bytes(config, path):
    value = lake_get(source_url(config, "repository_file", path=str(path)), config.source_token)
    if not isinstance(value, dict):
        raise BuildError("repository_file must return a value object")
    if "content_base64" in value:
        try:
            return base64.b64decode(value["content_base64"], validate=True)
        except (binascii.Error, ValueError, TypeError) as error:
            raise BuildError("repository_file contains invalid base64") from error
    contents = value.get("contents")
    if not isinstance(contents, str):
        raise BuildError("repository_file must return contents or content_base64")
    return contents.encode("utf-8")


def executable(entry):
    mode = entry.get("mode", "100644")
    if isinstance(mode, str):
        try:
            mode = int(mode, 8)
        except ValueError as error:
            raise BuildError("source has an invalid file mode") from error
    if type(mode) is not int or mode not in (0o644, 0o755, 0o100644, 0o100755):
        raise BuildError("source contains a symlink, special file, or unsupported mode")
    return bool(mode & 0o111)


def write_file(destination, content, is_executable=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as output:
            output.write(content)
        destination.chmod(0o755 if is_executable else 0o644)
    except (FileExistsError, NotADirectoryError, IsADirectoryError) as error:
        raise BuildError("source contains duplicate or conflicting file paths") from error


def fetch_source(config, root):
    tree = lake_get(source_url(config, "repository_tree", recursive="true"), config.source_token)
    if not isinstance(tree, dict) or not isinstance(tree.get("items"), list):
        raise BuildError("repository_tree must return value.items")
    context = root / "source"
    context.mkdir()
    selected = {}
    for item in tree["items"]:
        if not isinstance(item, dict):
            raise BuildError("repository_tree contains an invalid entry")
        path = relative_path(item.get("path"), "source path")
        if not path.is_relative_to(config.context):
            continue
        kind = item.get("kind", "file")
        if kind in ("directory", "tree"):
            continue
        if kind not in ("file", "blob"):
            raise BuildError("source contains an unsupported entry kind")
        if path in selected:
            raise BuildError("source contains duplicate file paths")
        selected[path] = executable(item)
    for path, is_executable in selected.items():
        relative = path.relative_to(config.context)
        if str(relative) == ".":
            raise BuildError("CONTEXT must be a directory")
        write_file(context / relative, file_bytes(config, path), is_executable)
    dockerfiles = root / "dockerfile"
    dockerfiles.mkdir()
    if config.inline_dockerfile is not None:
        contents = config.inline_dockerfile.encode("utf-8")
    elif config.dockerfile in selected:
        contents = (context / config.dockerfile.relative_to(config.context)).read_bytes()
    else:
        contents = file_bytes(config, config.dockerfile)
    write_file(dockerfiles / "Dockerfile", contents)
    # A Dockerfile-specific ignore file overrides the context's .dockerignore in BuildKit.
    if config.dockerfile is not None:
        ignore = config.dockerfile.with_name(config.dockerfile.name + ".dockerignore")
        if ignore in selected:
            write_file(
                dockerfiles / "Dockerfile.dockerignore",
                (context / ignore.relative_to(config.context)).read_bytes(),
            )
    return context, dockerfiles
