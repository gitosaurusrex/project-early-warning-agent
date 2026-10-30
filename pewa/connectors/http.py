"""Small JSON-over-HTTP client shared by the live connectors: retries, rate limits, no secrets in logs.

Only GET is offered: connectors are read-only. Responses are retried on 429, 5xx and network
errors, honouring Retry-After and GitHub's x-ratelimit-reset. Credentials go in headers and
never appear in log lines or error messages.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from pewa.connectors.base import ConnectorError

log = logging.getLogger(__name__)

# Longest single wait we accept for a rate-limit reset before giving up with a clear error.
MAX_WAIT_SECONDS = 120.0


class HttpClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        max_retries: int = 5,
        backoff: float = 1.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.name = name
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep
        self.requests = 0
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, auth=auth, timeout=timeout,
                                    follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise ConnectorError(f"{self.name}: could not reach {self._client.base_url.host}: {exc}") from exc
                self._wait(self.backoff * 2**attempt, f"network error ({type(exc).__name__})")
                continue
            self.requests += 1
            wait = self._retry_after(response, attempt)
            if wait is None:
                break
            if attempt == self.max_retries:
                raise ConnectorError(f"{self.name}: still rate-limited or failing after {self.max_retries} retries "
                                     f"(HTTP {response.status_code} on {path})")
            self._wait(wait, f"HTTP {response.status_code}")
        if response.status_code in (401, 403):
            raise ConnectorError(f"{self.name}: access denied (HTTP {response.status_code} on {path}). "
                                 "Check the token and its scopes.")
        if response.status_code == 404:
            raise ConnectorError(f"{self.name}: not found (HTTP 404 on {path}). Check the configured names and "
                                 "that the token can see them.")
        if response.status_code >= 400:
            raise ConnectorError(f"{self.name}: HTTP {response.status_code} on {path}")
        return response

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.get(path, params).json()

    def _retry_after(self, response: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None if the response should not be retried."""
        status, headers = response.status_code, response.headers
        if status == 429 or (status == 403 and (headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0")):
            if retry_after := headers.get("retry-after"):
                try:
                    return float(retry_after)
                except ValueError:
                    pass
            if reset := headers.get("x-ratelimit-reset"):  # GitHub: epoch seconds when the quota refills
                return max(0.0, float(reset) - datetime.now(UTC).timestamp()) + 1
            return self.backoff * 2**attempt
        if status >= 500:
            return self.backoff * 2**attempt
        return None

    def _wait(self, seconds: float, reason: str) -> None:
        if seconds > MAX_WAIT_SECONDS:
            raise ConnectorError(f"{self.name}: rate limit resets in {seconds:.0f}s, longer than "
                                 f"{MAX_WAIT_SECONDS:.0f}s; try again later")
        log.info("%s: %s, retrying in %.1fs", self.name, reason, seconds)
        self.sleep(seconds)
