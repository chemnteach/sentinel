"""
sentinel/api/client.py

Multi-provider model client for Sentinel.

Supports providers selected via the SENTINEL_PROVIDER environment variable:

    - "github-models" (opt-in): Uses the GitHub Models inference endpoint
        (models.inference.ai.azure.com), an OpenAI-compatible chat completions API
        authenticated with a GitHub Personal Access Token.

  - "anthropic": Uses the Anthropic Messages API via the official ``anthropic``
    Python SDK, authenticated with an ANTHROPIC_API_KEY.

    - "openai": Uses the OpenAI chat completions endpoint
        (api.openai.com/v1/chat/completions), authenticated with OPENAI_API_KEY.

All agent calls go through call_model(). Both clients handle:
  - Retry with exponential backoff on transient errors
  - Token usage tracking per call and cumulative
  - Structured response parsing
  - Rate limit handling (429 / overloaded -> wait and retry)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
from urllib.parse import urlparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from sentinel.config import Config
from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

# GitHub Models uses the OpenAI-compatible chat completions endpoint
_CHAT_PATH = "/chat/completions"
_MAX_RETRY_AFTER_SECONDS = 120.0


def _resolve_retry_after_seconds(
    headers: httpx.Headers,
    fallback_seconds: float,
) -> float:
    """Resolve retry delay from provider headers with sane bounds.

    Some providers return `retry-after-ms` instead of `Retry-After` seconds.
    """
    retry_after = fallback_seconds

    retry_after_header = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after_header:
        try:
            retry_after = float(retry_after_header)
        except (TypeError, ValueError):
            retry_after = fallback_seconds
    else:
        retry_after_ms_header = headers.get("retry-after-ms")
        if retry_after_ms_header:
            try:
                retry_after = float(retry_after_ms_header) / 1000.0
            except (TypeError, ValueError):
                retry_after = fallback_seconds

    return max(0.0, min(retry_after, _MAX_RETRY_AFTER_SECONDS))


@dataclass
class ModelResponse:
    """Parsed response from a single model call."""

    content: str
    """The assistant's text response."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    raw: dict[str, Any] = field(default_factory=dict)
    """Full raw response for debugging."""
    token_source: str = "actual"


@dataclass
class UsageSummary:
    """Cumulative token usage across all calls in this session."""

    total_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    actual_calls: int = 0
    estimated_calls: int = 0
    mixed_calls: int = 0

    def add(self, response: ModelResponse) -> None:
        self.total_calls += 1
        self.total_prompt_tokens += response.prompt_tokens
        self.total_completion_tokens += response.completion_tokens
        self.total_tokens += response.total_tokens
        if response.token_source == "actual":
            self.actual_calls += 1
        elif response.token_source == "estimated":
            self.estimated_calls += 1
        else:
            self.mixed_calls += 1

    def report(self) -> str:
        summary = (
            f"API calls: {self.total_calls} | "
            f"Tokens: {self.total_tokens:,} "
            f"(prompt={self.total_prompt_tokens:,}, "
            f"completion={self.total_completion_tokens:,})"
        )
        if self.estimated_calls or self.mixed_calls:
            summary += (
                f" | Token source: actual={self.actual_calls}, "
                f"estimated={self.estimated_calls}, mixed={self.mixed_calls}"
            )
        return summary


class _TelemetryRecorder:
    """Thread-safe per-call telemetry persistence for model provider clients.

    Each provider client holds its own recorder; entries are appended to
    ``llm_telemetry.json`` and consumed by the Stage 2.75 budget gate and
    the LLM-call-hotspots panel. The shared file means calls from any
    provider participate in the same ordering / cumulative counters,
    though each recorder tracks its own in-process cumulative totals.
    """

    def __init__(self, config: Config, endpoint_label: str) -> None:
        self._config = config
        self._endpoint = endpoint_label
        self._store = StateStore(config.state_dir, config.repo_path)
        self._lock = threading.Lock()
        self._counter = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0

    def record(
        self,
        *,
        payload_model: str,
        status: str,
        attempt: int,
        duration_ms: int,
        http_status: int | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        token_source: str = "actual",
        call_metadata: dict[str, Any] | None = None,
        error: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        with self._lock:
            self._counter += 1
            self._prompt_tokens += prompt_tokens
            self._completion_tokens += completion_tokens
            self._total_tokens += total_tokens
            entry: dict[str, Any] = {
                "sequence": self._counter,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "endpoint": self._endpoint,
                "model": payload_model,
                "status": status,
                "attempt": attempt,
                "http_status": http_status,
                "duration_ms": duration_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "token_source": token_source,
                "cumulative_prompt_tokens": self._prompt_tokens,
                "cumulative_completion_tokens": self._completion_tokens,
                "cumulative_total_tokens": self._total_tokens,
            }
            metadata = call_metadata or {}
            for key in ("agent", "skill_name", "response_format", "phase", "stage"):
                value = metadata.get(key)
                if value is not None and str(value).strip():
                    entry[key] = str(value).strip()
            if bool(metadata.get("json_retry")):
                entry["json_retry"] = True
            if error:
                entry["error"] = error[:300]
            if retry_after_seconds is not None:
                entry["retry_after_seconds"] = retry_after_seconds
            self._store.append_to_list("llm_telemetry", entry, commit=False)
            if self._config.log_level == "DEBUG":
                log.debug(
                    "LLM call #%d status=%s attempt=%d http=%s model=%s tokens=%d/%d/%d cumulative=%d duration_ms=%d",
                    entry["sequence"],
                    status,
                    attempt,
                    http_status,
                    payload_model,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    entry["cumulative_total_tokens"],
                    duration_ms,
                )


# ======================================================================
# GitHub Models client (OpenAI-compatible)
# ======================================================================


# GPT-5 / o-series are reasoning models: OpenAI-compatible endpoints reject any
# temperature other than the default (1.0) and require ``max_completion_tokens``
# instead of the legacy ``max_tokens`` field.
_FIXED_TEMPERATURE_MODEL_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def _requires_default_temperature(model: str) -> bool:
    """True when ``model`` is a reasoning model that only accepts temperature 1.0."""
    return (model or "").strip().lower().startswith(_FIXED_TEMPERATURE_MODEL_PREFIXES)


class ModelClient:
    """
    Thin client for OpenAI-compatible chat completions (OpenAI or GitHub Models).

    Usage:
        client = ModelClient(config)
        response = client.call_model(
            system_prompt="You are a code reviewer.",
            user_message="Review this function: ...",
        )
        print(response.content)
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._usage = UsageSummary()

        provider = getattr(config, "provider", "")
        if provider == "":
            raise EnvironmentError(
                "No model provider resolved. Configure SENTINEL_PROVIDER=copilot-cli|openai|anthropic, "
                "or provide OPENAI_API_KEY / ANTHROPIC_API_KEY. "
                "GitHub Models is blocked unless SENTINEL_ALLOW_GITHUB_MODELS=true."
            )
        if provider == "github-models" and not getattr(config, "allow_github_models", False):
            raise EnvironmentError(
                "Provider 'github-models' is blocked by policy. "
                "Use SENTINEL_PROVIDER=openai|anthropic, or explicitly opt in with "
                "SENTINEL_ALLOW_GITHUB_MODELS=true."
            )
        if provider == "openai":
            if not config.openai_api_key:
                raise EnvironmentError(
                    "OPENAI_API_KEY is not set. Set it in the environment or in a local .env file "
                    "(see .env.example)."
                )
            base_url = config.openai_base_url
            headers = {
                "Authorization": f"Bearer {config.openai_api_key}",
                "Content-Type": "application/json",
            }
        else:
            github_token = config.github_token
            if not github_token:
                raise EnvironmentError(
                    "GITHUB_TOKEN is not set. Set it in the environment or in a local .env file "
                    "(see .env.example)."
                )
            base_url = config.models_endpoint
            headers = {
                "Authorization": f"Bearer {github_token}",
                "Content-Type": "application/json",
            }

        self._http = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=120.0,
        )
        self._telemetry = _TelemetryRecorder(config, _CHAT_PATH)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def call_model(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> ModelResponse:
        """
        Make a single chat completion call.

        Args:
            system_prompt: The skill/persona injected as the system message.
            user_message: The code, context, or task for the agent.
            temperature: Sampling temperature (low = more deterministic).
            max_tokens: Override config max_tokens for this call.
            call_metadata: Optional tags (agent, skill_name, etc.) for telemetry.

        Returns:
            ModelResponse with parsed content and token usage.

        Raises:
            APIError: If all retries are exhausted.
        """
        token_cap = max_tokens or self._config.max_tokens
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if _requires_default_temperature(self._config.model):
            payload["temperature"] = 1.0
            payload["max_completion_tokens"] = token_cap
        else:
            payload["temperature"] = temperature
            payload["max_tokens"] = token_cap

        response = self._post_with_retry(payload, call_metadata=call_metadata)
        self._usage.add(response)

        log.debug(
            "Model call complete | tokens=%d | model=%s",
            response.total_tokens,
            response.model,
        )
        return response

    def call_model_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Make a model call and parse the response as JSON.

        The system prompt should instruct the model to respond with JSON only.
        Strips markdown code fences if the model wraps the JSON in them.

        Returns:
            Parsed JSON dict.

        Raises:
            APIError: On API failure.
            ValueError: If the response cannot be parsed as JSON.
        """
        return _call_model_json_with_retry(
            self, system_prompt, user_message, temperature, max_tokens, call_metadata
        )

    @property
    def usage(self) -> UsageSummary:
        return self._usage

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ModelClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post_with_retry(
        self,
        payload: dict[str, Any],
        *,
        call_metadata: dict[str, Any] | None = None,
    ) -> ModelResponse:
        """POST to the chat completions endpoint with retry logic."""
        last_error: Exception | None = None

        for attempt in range(1, self._config.api_max_retries + 1):
            try:
                start_ns = time.perf_counter_ns()
                resp = self._http.post(_CHAT_PATH, json=payload)
                duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)

                if resp.status_code == 429:
                    retry_after = _resolve_retry_after_seconds(
                        headers=resp.headers,
                        fallback_seconds=self._config.api_retry_delay * attempt,
                    )
                    self._telemetry.record(
                        payload_model=str(payload.get("model", "")),
                        status="rate_limited",
                        attempt=attempt,
                        duration_ms=duration_ms,
                        http_status=429,
                        call_metadata=call_metadata,
                        error="Too Many Requests",
                        retry_after_seconds=retry_after,
                    )
                    log.warning(
                        "Rate limited (429). Waiting %.1fs before retry %d/%d",
                        retry_after, attempt, self._config.api_max_retries,
                    )
                    if attempt >= self._config.api_max_retries:
                        raise APIError("HTTP error: 429 Too Many Requests")
                    time.sleep(retry_after)
                    continue

                if resp.status_code >= 500:
                    self._telemetry.record(
                        payload_model=str(payload.get("model", "")),
                        status="server_error",
                        attempt=attempt,
                        duration_ms=duration_ms,
                        http_status=resp.status_code,
                        call_metadata=call_metadata,
                        error=(resp.text or "").strip().replace("\n", " ")[:300],
                    )
                    raise APIError(
                        f"Server error {resp.status_code}: {resp.text[:200]}"
                    )

                resp.raise_for_status()
                parsed = self._parse_response(resp.json())
                self._telemetry.record(
                    payload_model=str(payload.get("model", "") or parsed.model),
                    status="success",
                    attempt=attempt,
                    duration_ms=duration_ms,
                    http_status=resp.status_code,
                    prompt_tokens=parsed.prompt_tokens,
                    completion_tokens=parsed.completion_tokens,
                    total_tokens=parsed.total_tokens,
                    token_source=parsed.token_source,
                    call_metadata=call_metadata,
                )
                return parsed

            except httpx.TimeoutException as exc:
                last_error = exc
                delay = self._config.api_retry_delay * attempt
                log.warning(
                    "Timeout on attempt %d/%d. Retrying in %.1fs",
                    attempt, self._config.api_max_retries, delay,
                )
                time.sleep(delay)

            except httpx.HTTPStatusError as exc:
                if (
                    getattr(exc.response, "status_code", None) == 400
                    and getattr(self._config, "provider", "") == "github-models"
                ):
                    body = ""
                    try:
                        body = exc.response.text
                    except Exception:
                        body = ""
                    if "unknown_model" in body and payload.get("model") != "gpt-4o-mini":
                        log.warning(
                            "Model '%s' is not available on GitHub Models. Falling back to gpt-4o-mini.",
                            payload.get("model"),
                        )
                        payload["model"] = "gpt-4o-mini"
                        continue
                raise APIError(f"HTTP error: {exc}") from exc

            except APIError:
                raise

        raise APIError(
            f"All {self._config.api_max_retries} retries exhausted. "
            f"Last error: {last_error}"
        )

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> ModelResponse:
        """Parse a raw OpenAI-compatible chat completion response."""
        try:
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return ModelResponse(
                content=content,
                model=data.get("model", "unknown"),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                raw=data,
            )
        except (KeyError, IndexError) as exc:
            raise APIError(f"Unexpected response structure: {exc}\n{data}") from exc


# ======================================================================
# Anthropic client
# ======================================================================


class AnthropicClient:
    """
    Client for the Anthropic Messages API.

    Uses the official ``anthropic`` Python SDK. Provides the same public
    interface as ``ModelClient`` (call_model, call_model_json, usage, close,
    context manager) so they are interchangeable at runtime.

    Usage:
        client = AnthropicClient(config)
        response = client.call_model(
            system_prompt="You are a code reviewer.",
            user_message="Review this function: ...",
        )
        print(response.content)
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._usage = UsageSummary()

        try:
            import anthropic as _anthropic
        except ImportError as exc:
            raise EnvironmentError(
                "The 'anthropic' package is required for provider='anthropic'. "
                "Install it with: pip install anthropic"
            ) from exc

        self._anthropic = _anthropic

        # Explicit timeout: 5 minutes per API call (large prompts like
        # the Cartographer can take a while, but we need hung connections
        # to be detected and retried rather than blocking forever).
        self._timeout = _anthropic.NOT_GIVEN
        try:
            self._timeout = _anthropic.Timeout(timeout=300.0, connect=30.0)
        except (AttributeError, TypeError):
            pass  # Older SDK versions may not have Timeout; use default

        if not config.anthropic_api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not set. Set it in the environment or in a "
                "local .env file (see .env.example)."
            )
        self._client = _anthropic.Anthropic(
            api_key=config.anthropic_api_key,
            timeout=self._timeout,
        )

        self._telemetry = _TelemetryRecorder(config, "/v1/messages")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def call_model(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> ModelResponse:
        """
        Make a single Anthropic Messages API call.

        Args:
            system_prompt: The skill/persona injected as the system parameter.
            user_message: The code, context, or task for the agent.
            temperature: Sampling temperature (low = more deterministic).
            max_tokens: Override config max_tokens for this call.
            call_metadata: Optional tags (agent, skill_name, etc.) for telemetry.

        Returns:
            ModelResponse with parsed content and token usage.

        Raises:
            APIError: If all retries are exhausted.
        """
        resolved_max_tokens = max_tokens or self._config.max_tokens

        response = self._call_with_retry(
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=temperature,
            max_tokens=resolved_max_tokens,
            call_metadata=call_metadata,
        )
        self._usage.add(response)

        log.debug(
            "Model call complete | tokens=%d | model=%s",
            response.total_tokens,
            response.model,
        )
        return response

    def call_model_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Make a model call and parse the response as JSON.

        The system prompt should instruct the model to respond with JSON only.
        Strips markdown code fences if the model wraps the JSON in them.

        Returns:
            Parsed JSON dict.

        Raises:
            APIError: On API failure.
            ValueError: If the response cannot be parsed as JSON.
        """
        return _call_model_json_with_retry(
            self, system_prompt, user_message, temperature, max_tokens, call_metadata
        )

    @property
    def usage(self) -> UsageSummary:
        return self._usage

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AnthropicClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_with_retry(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
        call_metadata: dict[str, Any] | None = None,
    ) -> ModelResponse:
        """Call the Anthropic Messages API with retry logic."""
        last_error: Exception | None = None

        for attempt in range(1, self._config.api_max_retries + 1):
            start_ns = time.perf_counter_ns()
            try:
                message = self._client.messages.create(
                    model=self._config.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[
                        {"role": "user", "content": user_message},
                    ],
                )
                duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
                parsed = self._parse_anthropic_response(message)
                self._telemetry.record(
                    payload_model=self._config.model,
                    status="success",
                    attempt=attempt,
                    duration_ms=duration_ms,
                    http_status=200,
                    prompt_tokens=parsed.prompt_tokens,
                    completion_tokens=parsed.completion_tokens,
                    total_tokens=parsed.total_tokens,
                    token_source=parsed.token_source,
                    call_metadata=call_metadata,
                )
                return parsed

            except self._anthropic.RateLimitError as exc:
                duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
                retry_after = self._config.api_retry_delay * attempt
                if hasattr(exc, "response") and exc.response is not None:
                    retry_after = _resolve_retry_after_seconds(
                        headers=exc.response.headers,
                        fallback_seconds=retry_after,
                    )
                self._telemetry.record(
                    payload_model=self._config.model,
                    status="rate_limited",
                    attempt=attempt,
                    duration_ms=duration_ms,
                    http_status=429,
                    call_metadata=call_metadata,
                    error="Too Many Requests",
                    retry_after_seconds=retry_after,
                )
                log.warning(
                    "Rate limited (429). Waiting %.1fs before retry %d/%d",
                    retry_after, attempt, self._config.api_max_retries,
                )
                if attempt >= self._config.api_max_retries:
                    raise APIError("Anthropic API error: 429 Too Many Requests") from exc
                time.sleep(retry_after)

            except self._anthropic.InternalServerError as exc:
                duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
                self._telemetry.record(
                    payload_model=self._config.model,
                    status="server_error",
                    attempt=attempt,
                    duration_ms=duration_ms,
                    http_status=getattr(exc, "status_code", 500),
                    call_metadata=call_metadata,
                    error=str(exc)[:300],
                )
                last_error = exc
                delay = self._config.api_retry_delay * attempt
                log.warning(
                    "Server error on attempt %d/%d. Retrying in %.1fs: %s",
                    attempt, self._config.api_max_retries, delay, exc,
                )
                time.sleep(delay)

            except self._anthropic.APITimeoutError as exc:
                duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
                self._telemetry.record(
                    payload_model=self._config.model,
                    status="timeout",
                    attempt=attempt,
                    duration_ms=duration_ms,
                    call_metadata=call_metadata,
                    error=str(exc)[:300],
                )
                last_error = exc
                delay = self._config.api_retry_delay * attempt
                log.warning(
                    "Timeout on attempt %d/%d. Retrying in %.1fs",
                    attempt, self._config.api_max_retries, delay,
                )
                time.sleep(delay)

            except self._anthropic.APIStatusError as exc:
                duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
                status = getattr(exc, "status_code", 0)
                # Overloaded (529) is also retryable
                if status == 529:
                    self._telemetry.record(
                        payload_model=self._config.model,
                        status="overloaded",
                        attempt=attempt,
                        duration_ms=duration_ms,
                        http_status=529,
                        call_metadata=call_metadata,
                        error=str(exc)[:300],
                    )
                    last_error = exc
                    delay = self._config.api_retry_delay * attempt
                    log.warning(
                        "API overloaded (529). Waiting %.1fs before retry %d/%d",
                        delay, attempt, self._config.api_max_retries,
                    )
                    time.sleep(delay)
                else:
                    self._telemetry.record(
                        payload_model=self._config.model,
                        status="client_error",
                        attempt=attempt,
                        duration_ms=duration_ms,
                        http_status=status,
                        call_metadata=call_metadata,
                        error=str(exc)[:300],
                    )
                    raise APIError(f"Anthropic API error: {exc}") from exc

            except self._anthropic.APIError as exc:
                duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
                self._telemetry.record(
                    payload_model=self._config.model,
                    status="api_error",
                    attempt=attempt,
                    duration_ms=duration_ms,
                    call_metadata=call_metadata,
                    error=str(exc)[:300],
                )
                raise APIError(f"Anthropic API error: {exc}") from exc

        raise APIError(
            f"All {self._config.api_max_retries} retries exhausted. "
            f"Last error: {last_error}"
        )

    @staticmethod
    def _parse_anthropic_response(message: Any) -> ModelResponse:
        """Parse an Anthropic Messages API response into ModelResponse."""
        try:
            # Extract text from content blocks
            content_parts: list[str] = []
            for block in message.content:
                if block.type == "text":
                    content_parts.append(block.text)
            content = "\n".join(content_parts) if content_parts else ""

            input_tokens = message.usage.input_tokens
            output_tokens = message.usage.output_tokens
            return ModelResponse(
                content=content,
                model=message.model,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                raw=message.model_dump() if hasattr(message, "model_dump") else {},
            )
        except (AttributeError, TypeError) as exc:
            raise APIError(f"Unexpected Anthropic response structure: {exc}") from exc


# ======================================================================
# Copilot CLI bridge client (no direct API key)
# ======================================================================


class CopilotCliClient:
    """Client that bridges Sentinel model calls through GitHub Copilot CLI."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._usage = UsageSummary()
        self._telemetry = _TelemetryRecorder(config, "copilot-cli")

    def call_model(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> ModelResponse:
        del temperature, max_tokens
        start_ns = time.perf_counter_ns()
        prompt = (
            "SYSTEM INSTRUCTIONS:\n"
            f"{system_prompt}\n\n"
            "USER REQUEST:\n"
            f"{user_message}"
        )

        prompt_file_path: str | None = None
        command = [
            "gh",
            "copilot",
            "--",
            "-s",
            "--output-format",
            "text",
            "--model",
            self._config.model,
            "--allow-all-tools",
            "--allow-all-paths",
            "--allow-all-urls",
            "--add-dir",
            str(self._config.repo_path),
        ]

        # Windows has a strict command-line length cap. Route prompts via
        # temporary files to keep the command length bounded regardless of size.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            delete=False,
        ) as tmp:
            tmp.write(prompt)
            prompt_file_path = tmp.name
        command.extend(
            [
                "-p",
                "Read the attached file and respond with only the final answer.",
                "-f",
                prompt_file_path,
            ]
        )

        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
        except OSError as exc:
            duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
            self._telemetry.record(
                payload_model=self._config.model,
                status="subprocess_error",
                attempt=1,
                duration_ms=duration_ms,
                call_metadata=call_metadata,
                error=str(exc)[:300],
            )
            raise APIError(f"Failed to execute Copilot CLI bridge: {exc}") from exc
        finally:
            if prompt_file_path:
                try:
                    os.unlink(prompt_file_path)
                except OSError:
                    pass

        duration_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            self._telemetry.record(
                payload_model=self._config.model,
                status="bridge_error",
                attempt=1,
                duration_ms=duration_ms,
                call_metadata=call_metadata,
                error=stderr[:300],
            )
            raise APIError(
                "Copilot CLI bridge failed "
                f"(exit {proc.returncode}): {stderr[:400]}"
            )

        content = (proc.stdout or "").strip()
        # Copilot CLI doesn't expose usage metrics; estimate token counts
        # from char length so the budget gate and hotspots have signal.
        prompt_tokens = _estimate_token_count(prompt)
        completion_tokens = _estimate_token_count(content)
        total_tokens = prompt_tokens + completion_tokens
        response = ModelResponse(
            content=content,
            model=self._config.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            raw={"provider": "copilot-cli"},
            token_source="estimated",
        )
        self._usage.add(response)
        self._telemetry.record(
            payload_model=self._config.model,
            status="success",
            attempt=1,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            token_source="estimated",
            call_metadata=call_metadata,
        )
        return response

    def call_model_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _call_model_json_with_retry(
            self, system_prompt, user_message, temperature, max_tokens, call_metadata
        )

    @property
    def usage(self) -> UsageSummary:
        return self._usage

    def close(self) -> None:
        return None

    def __enter__(self) -> "CopilotCliClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ======================================================================
# Shared utilities
# ======================================================================


def _sanitize_prompt_text(text: str) -> tuple[str, int]:
    """Remove invisible/control unicode characters that trigger gateway blocking."""
    if not text:
        return text, 0

    cleaned_chars: list[str] = []
    removed = 0
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            cleaned_chars.append(ch)
            continue

        category = unicodedata.category(ch)
        if category in {"Cf", "Cc", "Cs"}:
            removed += 1
            continue

        if ch in {"\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"}:
            removed += 1
            continue

        cleaned_chars.append(ch)

    return "".join(cleaned_chars), removed


def _call_model_json_with_retry(
    client: Any,
    system_prompt: str,
    user_message: str,
    temperature: float,
    max_tokens: int | None,
    call_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Call ``client.call_model`` and parse JSON, retrying once with a compact
    strict-JSON instruction (reduces truncation risk for large schemas)."""
    response = client.call_model(
        system_prompt=system_prompt,
        user_message=user_message,
        temperature=temperature,
        max_tokens=max_tokens,
        call_metadata=call_metadata,
    )
    try:
        return _parse_json_response(response.content)
    except ValueError as first_exc:
        log.warning("Model returned invalid JSON; retrying once with compact JSON instruction.")
        retry_response = client.call_model(
            system_prompt=system_prompt,
            user_message=(
                f"{user_message}\n\n"
                "Return only strict JSON with no markdown or prose. "
                "Keep all values concise and minimize token usage."
            ),
            temperature=min(temperature, 0.1),
            max_tokens=max_tokens,
            call_metadata=call_metadata,
        )
        try:
            return _parse_json_response(retry_response.content)
        except ValueError as retry_exc:
            raise ValueError(
                f"Model response is not valid JSON after retry: {retry_exc}"
            ) from first_exc


def _parse_json_response(content: str) -> dict[str, Any]:
    """Strip markdown fences and parse JSON from model response."""
    text = content.strip()
    # Strip ```json ... ``` or ``` ... ``` fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove opening fence (```json or ```)
        lines = lines[1:]
        # Remove closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback 1: strip trailing commas before } or ] (common LLM issue)
    import re
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback 2: extract the first JSON object from surrounding text
    start = cleaned.find("{")
    if start != -1:
        try:
            obj, _end = json.JSONDecoder().raw_decode(cleaned[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Fallback 3: truncated response — try to close open braces/brackets
    if start != -1:
        fragment = cleaned[start:]
        open_braces = fragment.count("{") - fragment.count("}")
        open_brackets = fragment.count("[") - fragment.count("]")
        if open_braces > 0 or open_brackets > 0:
            # Strip trailing partial string/value
            repaired = re.sub(r',?\s*"[^"]*$', "", fragment)
            repaired = re.sub(r",\s*$", "", repaired)
            repaired += "]" * max(0, open_brackets) + "}" * max(0, open_braces)
            try:
                obj = json.loads(repaired)
                if isinstance(obj, dict):
                    log.warning(
                        "Recovered truncated JSON response (%d chars, closed %d brace(s))",
                        len(fragment),
                        open_braces,
                    )
                    return obj
            except json.JSONDecodeError:
                pass

    raise ValueError(
        f"Model response is not valid JSON:\nContent: {content[:600]}"
    )


def _estimate_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _extract_usage_metrics(
    *,
    data: dict[str, Any],
    prompt_text: str,
    response_text: str,
) -> tuple[int, int, int, str]:
    usage = data.get("usage", {}) if isinstance(data.get("usage", {}), dict) else {}
    prompt_tokens = int(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("promptTokenCount")
        or 0
    )
    completion_tokens = int(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("completionTokenCount")
        or usage.get("candidatesTokenCount")
        or 0
    )
    total_tokens = int(
        usage.get("total_tokens")
        or usage.get("totalTokenCount")
        or 0
    )

    has_actual_prompt = prompt_tokens > 0
    has_actual_completion = completion_tokens > 0
    has_actual_total = total_tokens > 0

    estimated_prompt = _estimate_token_count(prompt_text)
    estimated_completion = _estimate_token_count(response_text)

    if not has_actual_prompt:
        prompt_tokens = estimated_prompt
    if not has_actual_completion:
        completion_tokens = estimated_completion
    if not has_actual_total:
        total_tokens = prompt_tokens + completion_tokens

    actual_fields = sum(1 for flag in (has_actual_prompt, has_actual_completion, has_actual_total) if flag)
    if actual_fields == 3:
        token_source = "actual"
    elif actual_fields == 0:
        token_source = "estimated"
    else:
        token_source = "mixed"

    return prompt_tokens, completion_tokens, total_tokens, token_source


# ======================================================================
# Error + NoOp + factory
# ======================================================================


class APIError(RuntimeError):
    """Raised when a model provider returns an unrecoverable error."""


class NoOpModelClient:
    """Local-only client used when external model inference is disabled."""

    def __init__(self) -> None:
        self._usage = UsageSummary()

    @property
    def usage(self) -> UsageSummary:
        return self._usage

    def call_model(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> ModelResponse:
        del system_prompt, user_message, temperature, max_tokens, call_metadata
        raise APIError("No-op client cannot execute external model calls")

    def call_model_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        call_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del system_prompt, user_message, temperature, max_tokens, call_metadata
        raise APIError("No-op client cannot execute external model calls")

    def close(self) -> None:
        return None

    def __enter__(self) -> "NoOpModelClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def create_model_client(
    config: Config,
) -> ModelClient | AnthropicClient | CopilotCliClient | NoOpModelClient:
    """
    Create the runtime model client based on provider configuration.

        Checks ``config.provider``:
            - ``"anthropic"`` -> AnthropicClient (Anthropic Messages API)
            - ``"openai"`` -> ModelClient (OpenAI chat completions)
            - ``"copilot-cli"`` -> CopilotCliClient (GitHub Copilot CLI bridge)
            - ``"github-models"`` -> ModelClient (GitHub Models / OpenAI-compatible)

    Returns a NoOpModelClient when ``config.local_only_mode`` is True.
    """
    if getattr(config, "local_only_mode", False):
        return NoOpModelClient()

    provider = getattr(config, "provider", "")

    if provider == "":
        raise EnvironmentError(
            "No model provider resolved. Configure SENTINEL_PROVIDER=copilot-cli|openai|anthropic, "
            "or provide OPENAI_API_KEY / ANTHROPIC_API_KEY. "
            "GitHub Models is blocked unless SENTINEL_ALLOW_GITHUB_MODELS=true."
        )

    if provider == "github-models" and not getattr(config, "allow_github_models", False):
        raise EnvironmentError(
            "Provider 'github-models' is blocked by policy. "
            "Use SENTINEL_PROVIDER=openai|anthropic, or explicitly opt in with "
            "SENTINEL_ALLOW_GITHUB_MODELS=true."
        )

    if provider == "anthropic":
        return AnthropicClient(config)
    elif provider == "copilot-cli":
        return CopilotCliClient(config)
    elif provider in {"github-models", "openai"}:
        return ModelClient(config)
    else:
        raise EnvironmentError(
            f"Unknown SENTINEL_PROVIDER: {provider!r}. "
            f"Supported values: 'github-models', 'anthropic', 'openai', 'copilot-cli'."
        )
