# SPDX-License-Identifier: MIT

"""Best-effort lake events and bounded, credential-redacted log batches."""

import base64
import json
import sys
import time

from .config import BuildError, build_id, lake_url, required
from .http import request_bytes


class Events:
    def __init__(self, env):
        self.url = lake_url(required(env, "LAKE_URL")) + "/v1/publish_build_event"
        self.identifier = build_id(env)
        self.token = required(env, "EVENTS_TOKEN")
        secrets = [env.get(name, "") for name in ("SOURCE_TOKEN", "PUSH_TOKEN", "EVENTS_TOKEN")]
        push_auth = "lake:" + env.get("PUSH_TOKEN", "")
        secrets.append(base64.b64encode(push_auth.encode()).decode())
        self.secrets = []
        self.add_secrets(secrets)
        self.lines = []
        self.line_bytes = 0
        self.dropped = 0
        self.last_log = float("-inf")

    def add_secrets(self, secrets):
        values = {*self.secrets, *(value for value in secrets if value)}
        self.secrets = sorted(values, key=len, reverse=True)

    def redact(self, message):
        for secret in self.secrets:
            message = message.replace(secret, "[redacted]")
        return message

    def emit(self, event_type, **fields):
        payload = {"build_id": self.identifier, "type": event_type, **fields}
        encoded = json.dumps(payload, ensure_ascii=True).encode()
        if len(encoded) > 64 * 1024:
            return
        try:
            request_bytes(self.url, f"Bearer {self.token}", encoded, timeout=2)
        except (BuildError, ValueError):
            # Progress delivery is never part of build success or failure.
            pass

    def phase(self, phase):
        self.emit("phase", phase=phase)

    def log(self, message):
        # A single malicious BuildKit line cannot grow the buffer without a bound.
        message = self.redact(message)[:4096]
        print(message, file=sys.stderr, flush=True)
        length = len(json.dumps(message, ensure_ascii=True).encode()) + 2
        if self.line_bytes + length <= 56 * 1024:
            self.lines.append(message)
            self.line_bytes += length
        else:
            self.dropped += 1

    def flush(self, final=False):
        if not self.lines and not self.dropped:
            return
        remaining = self.last_log + 1 - time.monotonic()
        if remaining > 0:
            if not final:
                return
            time.sleep(remaining)
        lines = self.lines
        if self.dropped:
            lines.append(f"[{self.dropped} log lines omitted from events; see Job stderr]")
        self.last_log = time.monotonic()
        self.emit("log", lines=lines)
        self.lines = []
        self.line_bytes = 0
        self.dropped = 0
