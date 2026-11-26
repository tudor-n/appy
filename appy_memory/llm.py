"""A minimal client for LM Studio's OpenAI-compatible chat API (just what the
consolidator needs; the service avoids the openai package to stay light on Python 3.9)."""
from __future__ import annotations

import httpx


REASONING_HEADROOM = 3000  # with reasoning on, Gemma thinks first, out of the same budget


class LLMError(Exception):
    pass


class BadRequest(LLMError):
    """The server refused this request (4xx, e.g. too long for the context): retrying the
    same request won't help."""


class EmptyOutput(LLMError):
    """The model answered with nothing (e.g. it ran out of tokens while reasoning)."""


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str = "", effort: str = "none",
                 timeout: float = 300, http: httpx.Client | None = None):
        self.model = model
        self.effort = effort
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.headers = headers

    def chat(self, messages: list[dict], *, temperature: float, max_tokens: int,
             schema: dict | None = None) -> tuple[str, int]:
        """One non-streamed completion: (content, total tokens). Raises LLMError."""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens + (0 if self.effort == "none" else REASONING_HEADROOM),
            "reasoning_effort": self.effort,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        try:
            r = self.http.post("/chat/completions", json=body, headers=self.headers)
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                raise BadRequest(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
            content = str(choice["message"]["content"] or "")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        if not content.strip():
            raise EmptyOutput(f"empty output (finish_reason {choice.get('finish_reason')})")
        usage = data.get("usage") or {}
        return content, int(usage.get("total_tokens") or 0)
