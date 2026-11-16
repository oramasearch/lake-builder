# SPDX-License-Identifier: MIT

"""HTTP clients that never forward credentials through redirects or host proxies."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import BuildError


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def request_bytes(url, authorization, body=None, timeout=30, accept="application/json"):
    headers = {"Authorization": authorization, "Accept": accept}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            # Bound malformed or accidentally enormous REST responses.
            payload = response.read(64 * 1024 * 1024 + 1)
    except HTTPError as error:
        raise BuildError(f"HTTP {error.code} from remote service") from error
    except (URLError, TimeoutError, OSError) as error:
        raise BuildError("remote service request failed") from error
    if len(payload) > 64 * 1024 * 1024:
        raise BuildError("remote response exceeds 64 MiB")
    return payload


def lake_get(url, token):
    payload = request_bytes(url, f"Bearer {token}")
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise BuildError("lake returned invalid JSON") from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BuildError("lake refused the source request")
    return result.get("value")
