from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


class OllamaError(RuntimeError):
    """Base exception for Ollama client failures."""


class OllamaTimeoutError(OllamaError):
    """Raised when Ollama does not respond before the configured timeout."""

    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Ollama timed out after {timeout_seconds}s at {base_url}."
        )


class OllamaUnreachableError(OllamaError):
    """Raised when Ollama cannot be reached over HTTP."""

    def __init__(self, base_url: str, detail: str | None = None) -> None:
        self.base_url = base_url
        self.detail = detail
        message = f"Ollama is not reachable at {base_url}."
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)


class OllamaAPIError(OllamaError):
    """Raised when Ollama returns a non-success response or malformed payload."""

    def __init__(
        self,
        base_url: str,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.status_code = status_code
        self.detail = detail
        parts = [f"Ollama returned an error from {base_url}."]
        if status_code is not None:
            parts.append(f"Status code: {status_code}.")
        if detail:
            parts.append(detail)
        super().__init__(" ".join(parts))


@dataclass
class OllamaClient:
    base_url: str | None = None
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        settings = get_settings()
        resolved_base_url = self.base_url or settings.ollama_base_url
        resolved_timeout = self.timeout_seconds or settings.ollama_timeout_seconds
        self._base_url = resolved_base_url.rstrip("/")
        self._timeout_seconds = int(resolved_timeout)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
        )

    @property
    def resolved_base_url(self) -> str:
        return self._base_url

    @property
    def timeout_seconds_value(self) -> int:
        return self._timeout_seconds

    def close(self) -> None:
        self._client.close()

    def generate(
        self,
        prompt: str,
        model: str,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system is not None:
            payload["system"] = system

        try:
            response = self._client.post("/api/generate", json=payload)
        except httpx.TimeoutException:
            raise OllamaTimeoutError(
                self._base_url,
                self._timeout_seconds,
            ) from None
        except httpx.RequestError as exc:
            raise OllamaUnreachableError(self._base_url, str(exc)) from None

        if response.status_code >= 400:
            raise OllamaAPIError(
                self._base_url,
                status_code=response.status_code,
                detail=response.text[:500] if response.text else None,
            ) from None

        try:
            payload = response.json()
        except ValueError:
            raise OllamaAPIError(self._base_url, detail="Response was not valid JSON.") from None

        raw_output = payload.get("response")
        if not isinstance(raw_output, str):
            raise OllamaAPIError(
                self._base_url,
                detail="Response did not include a text 'response' field.",
            ) from None

        return raw_output.strip()
