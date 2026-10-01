"""Registry credentials and the exact compressed bytes reachable from an image digest."""

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .config import BuildError
from .http import request_bytes

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)


def encoded_auth(token):
    return base64.b64encode(("lake:" + token).encode()).decode()


def base_registry_auths(env):
    source = env.get("BASE_REGISTRY_AUTH", "")
    if not source:
        return {}
    try:
        document = json.loads(Path(source).read_bytes())
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise BuildError("BASE_REGISTRY_AUTH cannot be read") from error
    except (ValueError, UnicodeError) as error:
        raise BuildError("BASE_REGISTRY_AUTH must contain valid JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("auths"), dict):
        raise BuildError("BASE_REGISTRY_AUTH must contain an auths object")
    return dict(document["auths"])


def credential_secrets(auths):
    secrets = []
    for credential in auths.values():
        if not isinstance(credential, dict):
            continue
        secrets.extend(value for value in credential.values() if isinstance(value, str) and value)
        encoded = credential.get("auth")
        if not isinstance(encoded, str):
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True).decode()
        except (ValueError, UnicodeError):
            continue
        secrets.append(decoded)
        _, separator, password = decoded.partition(":")
        if separator and password:
            secrets.append(password)
    return secrets


def write_credentials(config, root, env):
    directory = root / "credentials"
    directory.mkdir(mode=0o700)
    path = directory / "config.json"
    auths = base_registry_auths(env)
    auths[config.registry] = {"auth": encoded_auth(config.push_token)}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump({"auths": auths}, output)
    return directory, credential_secrets(auths)


def registry_scheme(config):
    # The in-cluster registry uses the same transport as lake's REST Service.
    return urlsplit(config.lake).scheme


def digest_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise BuildError("BuildKit or registry returned an invalid sha256 digest")
    return value


def metadata_digest(path):
    try:
        metadata = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError) as error:
        raise BuildError("BuildKit did not produce valid image metadata") from error
    if not isinstance(metadata, dict):
        raise BuildError("BuildKit metadata must be an object")
    return digest_value(metadata.get("containerimage.digest"))


def descriptor(value):
    if not isinstance(value, dict):
        raise BuildError("registry returned an invalid descriptor")
    digest = digest_value(value.get("digest"))
    size = value.get("size")
    if type(size) is not int or size < 0:
        raise BuildError("registry returned an invalid descriptor size")
    return digest, size


def image_size(config, digest):
    sizes = {}
    visited = set()
    pending = [(digest, None)]
    while pending:
        current, expected_size = pending.pop()
        if current in visited:
            if expected_size is not None and sizes[current] != expected_size:
                raise BuildError("registry returned conflicting descriptor sizes")
            continue
        if len(visited) >= 128:
            raise BuildError("registry image contains too many manifests")
        url = f"{registry_scheme(config)}://{config.registry}/v2/{config.image}/manifests/{current}"
        raw = request_bytes(url, "Basic " + encoded_auth(config.push_token), accept=ACCEPT)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != current:
            raise BuildError("registry manifest does not match the pushed digest")
        if expected_size is not None and len(raw) != expected_size:
            raise BuildError("registry manifest does not match its descriptor size")
        sizes[current] = len(raw)
        visited.add(current)
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise BuildError("registry returned invalid manifest JSON") from error
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
            raise BuildError("registry returned an unsupported manifest")
        if "manifests" in manifest:
            children = manifest["manifests"]
            if not isinstance(children, list):
                raise BuildError("registry returned an invalid manifest index")
            pending.extend(descriptor(child) for child in children)
        else:
            layers = manifest.get("layers")
            if not isinstance(layers, list):
                raise BuildError("registry returned an invalid image manifest")
            for child in [manifest.get("config"), *layers]:
                child_digest, child_size = descriptor(child)
                if child_digest in sizes and sizes[child_digest] != child_size:
                    raise BuildError("registry returned conflicting descriptor sizes")
                sizes[child_digest] = child_size
    return sum(sizes.values())
