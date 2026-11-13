"""HTTP client for the router's web surfaces.

Standard library only (``urllib``): the plugin declares ``python_runtime:
external``, so a dependency means the operator has to manage a virtualenv for a
plugin that only needs to POST JSON to their own router.

The router answers BOTH surfaces with a flat object — ``/v1/search`` returns
``{provider, query, results, usage, metrics}`` and ``/v1/web/fetch`` returns
``{provider, url, title, content, metadata, usage, metrics}``. A ``data``
wrapper is still accepted because the published skill example wraps one, and a
shape that only exists in documentation should not turn into a hard failure.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlparse


class RouterError(RuntimeError):
    """A router call that did not produce a usable JSON body."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def unwrap(payload: Any) -> Dict[str, Any]:
    """Accept a flat router object and the documented ``{data: {...}}`` wrapper."""
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data")
    if isinstance(inner, dict) and ("results" in inner or "content" in inner) and "results" not in payload:
        return inner
    return payload


def error_text(payload: Any, fallback: str) -> str:
    """Pull the most specific human sentence out of a router error body."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            for key in ("message", "detail", "code"):
                value = err.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(err, str) and err.strip():
            return err.strip()
        for key in ("message", "detail", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:300]
    return fallback


class RouterClient:
    """Minimal JSON client. One instance per call site is fine; it is stateless."""

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def post(self, path: str, body: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8")
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.api_key:
            headers["authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
        except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a JSON reason
            try:
                raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            finally:
                exc.close()
            parsed = _maybe_json(raw)
            raise RouterError(error_text(parsed, f"router returned HTTP {exc.code}"), status=exc.code, body=raw) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise RouterError(f"cannot reach {urlparse(url).netloc}: {reason}") from exc

        parsed = _maybe_json(raw)
        if not isinstance(parsed, dict):
            raise RouterError(
                f"router returned a body that is not a JSON object (HTTP {status}, {len(raw)} bytes)",
                status=status,
                body=raw[:300],
            )
        if status >= 400:
            raise RouterError(error_text(parsed, f"router returned HTTP {status}"), status=status, body=raw[:300])
        return parsed


def _maybe_json(raw: str) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
