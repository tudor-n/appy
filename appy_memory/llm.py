"""A minimal client for LM Studio's OpenAI-compatible chat API (just what the
consolidator needs; the service avoids the openai package to stay light on Python 3.9)."""
from __future__ import annotations

import httpx


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str = "", effort: str = "low",
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
            "max_tokens": max_tokens,
            "reasoning_effort": self.effort,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        try:
            r = self.http.post("/chat/completions", json=body, headers=self.headers)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        usage = data.get("usage") or {}
        return content, int(usage.get("total_tokens") or 0)
