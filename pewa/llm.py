"""Thin Anthropic client wrapper: retries, call logging, refusal handling and a dry-run mode.

Dry-run mode never touches the network. It returns, in order: queued `scripted` responses
(used by tests to feed malformed output), else the caller's `canned` response builder.
"""

import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy.orm import Session

from pewa.config import get_settings
from pewa.models import LLMCall

log = logging.getLogger(__name__)

# USD per million tokens (input, output).
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
# Models that accept server-side refusal fallbacks (`fallbacks: "default"`) on the Claude API.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DRY_RUN_MODEL = "dry-run"

Message = dict[str, Any]


class LLMError(Exception):
    """The model could not produce a response (API error, refusal, no canned response)."""


class LLMAuthError(LLMError):
    """Credentials were rejected; every further call would fail the same way."""


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICING.get(model, (0.0, 0.0))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def prompt_hash(model: str, system: str, messages: list[Message]) -> str:
    raw = json.dumps({"model": model, "system": system, "messages": messages}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class LLM:
    def __init__(
        self,
        model: str | None = None,
        *,
        dry_run: bool = False,
        session: Session | None = None,
        scripted: Iterable[str] = (),
        max_retries: int = 4,
    ):
        self.model = model or get_settings().pewa_model
        self.dry_run = dry_run
        self.session = session
        self.scripted = deque(scripted)
        self.max_retries = max_retries
        self._client: Any = None

    @property
    def label(self) -> str:
        return DRY_RUN_MODEL if self.dry_run else self.model

    def complete(
        self,
        purpose: str,
        system: str,
        messages: list[Message],
        *,
        canned: Callable[[], str] | None = None,
        max_tokens: int = 16000,
    ) -> str:
        """Return the model's text reply. Every attempt is logged to `llm_calls`."""
        started = time.monotonic()
        record = LLMCall(
            model=self.label,
            purpose=purpose,
            prompt_hash=prompt_hash(self.label, system, messages),
            dry_run=self.dry_run,
        )
        try:
            if self.dry_run:
                text = self._canned(canned)
            else:
                text, record.input_tokens, record.output_tokens = self._call_api(system, messages, max_tokens)
                record.cost_usd = cost_usd(self.model, record.input_tokens, record.output_tokens)
            return text
        except LLMError as exc:
            record.error = str(exc)[:2000]
            raise
        finally:
            record.latency_ms = int((time.monotonic() - started) * 1000)
            log.info("llm %s %s: %d in / %d out tokens, %d ms", purpose, self.label,
                     record.input_tokens or 0, record.output_tokens or 0, record.latency_ms)
            if self.session is not None:
                self.session.add(record)
                self.session.flush()

    def _canned(self, canned: Callable[[], str] | None) -> str:
        if self.scripted:
            return self.scripted.popleft()
        if canned is None:
            raise LLMError("dry-run: no canned response for this call")
        return canned()

    def _call_api(self, system: str, messages: list[Message], max_tokens: int) -> tuple[str, int, int]:
        import anthropic

        if self._client is None:
            # The SDK retries connection errors, 408/409/429 and 5xx with exponential backoff.
            # An explicit key wins; otherwise the SDK resolves ANTHROPIC_API_KEY / AUTH_TOKEN / profiles.
            key = get_settings().anthropic_key()
            self._client = anthropic.Anthropic(api_key=key, max_retries=self.max_retries) if key else \
                anthropic.Anthropic(max_retries=self.max_retries)
        request: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages}
        try:
            if self.model in FALLBACK_MODELS:
                # If the model's safety classifiers decline, the API re-runs the request on a
                # recommended fallback model instead of returning a refusal.
                response = self._client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                response = self._client.messages.create(**request)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMAuthError(f"API rejected the credentials ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the API: {exc}") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMError(f"model declined the request (category: {category})")
        text = "".join(block.text for block in response.content if block.type == "text")
        if response.stop_reason == "max_tokens":
            log.warning("llm response hit max_tokens=%d; output may be truncated", max_tokens)
        return text, response.usage.input_tokens, response.usage.output_tokens
