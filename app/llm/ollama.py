"""Local model provider for Ollama (https://ollama.com): the same agent, no cloud, no API key.

- Talks to Ollama's native `/api/chat` over HTTP (httpx), non-streaming, with function calling (`tools`).
- Messages and tools are converted here; the agent only sees `LLMProvider`.
- Small local models are slow on modest hardware, so the request timeout is generous and a failed request is
  retried once. Any failure becomes `LLMError` and the bot answers with its polite fallback.
- Ollama does not return tool-call ids, so ids are generated and tool results are matched by order.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.llm.base import ChatMessage, LLMError, LLMReply, ToolCall, ToolSpec, Usage

log = logging.getLogger(__name__)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        temperature: float = 0.2,
        num_ctx: int = 4096,
        max_output_tokens: int = 400,
        timeout_s: float = 600.0,
        retries: int = 1,
        keep_alive: str = "10m",
        client: httpx.AsyncClient | None = None,
    ):
        self.model = model
        self.last_model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self.keep_alive = keep_alive
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10.0))

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "messages": [{"role": "system", "content": system}, *to_messages(messages)],
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.max_output_tokens,
            },
        }
        if tools:
            body["tools"] = [to_tool(t) for t in tools]
        for attempt in range(self.retries + 1):
            try:
                response = await self.client.post(f"{self.base_url}/api/chat", json=body)
                if response.status_code != 200:
                    raise LLMError(f"Ollama API error {response.status_code}")
                return from_response(response.json())
            except (httpx.TransportError, ValueError) as exc:
                reason, error, cause = type(exc).__name__, LLMError(f"Ollama request failed: {type(exc).__name__}"), exc
            except LLMError as exc:
                if attempt == self.retries:
                    raise
                reason, error, cause = str(exc), exc, exc
            if attempt == self.retries:
                raise error from cause
            log.warning("Ollama %s, retry %s", reason, attempt + 1)
            await asyncio.sleep(2)
        raise LLMError("Ollama: retries exhausted")  # pragma: no cover


def to_tool(spec: ToolSpec) -> dict[str, Any]:
    return {"type": "function", "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters}}


def to_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.text}
            if m.tool_calls:
                item["tool_calls"] = [{"function": {"name": c.name, "arguments": c.args}} for c in m.tool_calls]
            out.append(item)
        else:
            out.append({"role": "tool", "tool_name": m.tool_name, "content": json.dumps(m.tool_result, ensure_ascii=False, default=str)})
    return out


def from_response(data: dict[str, Any]) -> LLMReply:
    message = data.get("message") or {}
    calls = []
    for n, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        calls.append(ToolCall(name=fn.get("name", ""), args=dict(args) if isinstance(args, dict) else {}, id=f"ollama-{n}"))
    usage = Usage(input_tokens=data.get("prompt_eval_count") or 0, output_tokens=data.get("eval_count") or 0)
    return LLMReply(text=(message.get("content") or "").strip(), tool_calls=calls, usage=usage)
