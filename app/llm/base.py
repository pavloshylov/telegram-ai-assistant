"""Provider-neutral types for chat with tool calling.

The agent only talks to `LLMProvider`; Gemini, OpenAI, Claude or a local model plug in by implementing
one method. Message and tool formats are converted inside each provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the arguments object


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)  # provider data to send back (e.g. Gemini thought signature)


@dataclass
class ChatMessage:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant asking for tools
    tool_name: str = ""  # tool result
    tool_call_id: str = ""
    tool_result: dict[str, Any] | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __iadd__(self, other: "Usage") -> "Usage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        return self


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class LLMError(RuntimeError):
    """Raised when the provider fails after retries; the bot answers with a polite fallback."""


class LLMProvider(Protocol):
    name: str

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMReply: ...
