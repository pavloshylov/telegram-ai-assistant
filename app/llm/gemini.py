"""Google Gemini provider (google-genai SDK, async).

- Function calling with JSON-Schema tool declarations; automatic function calling is off, the agent runs tools.
- Thought signatures returned with function calls are kept and sent back, as Gemini 3 models require.
- Retries with exponential backoff on rate limits (429) and server errors (5xx).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from google import genai
from google.genai import errors, types

from app.llm.base import ChatMessage, LLMError, LLMReply, ToolCall, ToolSpec, Usage

log = logging.getLogger(__name__)
RETRY_CODES = {429, 500, 502, 503, 504}


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        max_output_tokens: int = 800,
        retries: int = 3,
        client: Any | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self.client = client or genai.Client(api_key=api_key)

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMReply:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            tools=[types.Tool(function_declarations=[to_declaration(t) for t in tools])] if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = [to_content(m) for m in messages]
        for attempt in range(self.retries + 1):
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                return from_response(response)
            except errors.APIError as exc:
                if exc.code in RETRY_CODES and attempt < self.retries:
                    delay = 2**attempt
                    log.warning("Gemini %s, retry %s in %ss", exc.code, attempt + 1, delay)
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(f"Gemini API error {exc.code}") from exc
        raise LLMError("Gemini: retries exhausted")  # pragma: no cover


def to_declaration(spec: ToolSpec) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=spec.name, description=spec.description, parameters_json_schema=spec.parameters
    )


def to_content(message: ChatMessage) -> types.Content:
    if message.role == "user":
        return types.Content(role="user", parts=[types.Part.from_text(text=message.text)])
    if message.role == "assistant":
        parts: list[types.Part] = []
        if message.text:
            parts.append(types.Part.from_text(text=message.text))
        for call in message.tool_calls:
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(name=call.name, args=call.args, id=call.id or None),
                    thought_signature=call.meta.get("thought_signature"),
                )
            )
        return types.Content(role="model", parts=parts)
    # tool result goes back as a user turn with a function_response part
    return types.Content(
        role="user",
        parts=[types.Part.from_function_response(name=message.tool_name, response={"result": message.tool_result})],
    )


def from_response(response: types.GenerateContentResponse) -> LLMReply:
    candidate = response.candidates[0] if response.candidates else None
    parts = candidate.content.parts if candidate and candidate.content and candidate.content.parts else []
    text = "".join(p.text for p in parts if p.text and not p.thought)
    calls = [
        ToolCall(
            name=p.function_call.name,
            args=dict(p.function_call.args or {}),
            id=p.function_call.id or "",
            meta={"thought_signature": p.thought_signature} if p.thought_signature else {},
        )
        for p in parts
        if p.function_call
    ]
    meta = response.usage_metadata
    usage = Usage(
        input_tokens=(meta.prompt_token_count or 0) if meta else 0,
        output_tokens=((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)) if meta else 0,
    )
    return LLMReply(text=text.strip(), tool_calls=calls, usage=usage)
