"""Gemini adapter: request/response conversion and retries, against the real SDK types (no network)."""
from __future__ import annotations

import httpx
import pytest
from google.genai import errors, types

from app.llm import gemini
from app.llm.base import ChatMessage, LLMError, ToolCall, ToolSpec
from app.llm.gemini import GeminiProvider, from_response, to_content, to_declaration

SPEC = ToolSpec("search_products", "Search the catalog", {"type": "object", "properties": {"need": {"type": "string"}}})


def response(*parts: types.Part, prompt_tokens=120, output_tokens=30) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens, candidates_token_count=output_tokens, thoughts_token_count=5
        ),
    )


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    async def generate_content(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)
        self.aio = self


def provider(outcomes) -> GeminiProvider:
    return GeminiProvider(api_key="test", model="gemini-test", client=FakeClient(outcomes))


def test_tool_declaration_uses_json_schema():
    declaration = to_declaration(SPEC)
    assert declaration.name == "search_products"
    assert declaration.parameters_json_schema["properties"]["need"]["type"] == "string"


def test_history_conversion_keeps_thought_signature():
    call = ToolCall("search_products", {"need": "running"}, id="call-1", meta={"thought_signature": b"sig"})
    assistant = to_content(ChatMessage(role="assistant", text="Let me look.", tool_calls=[call]))
    assert assistant.role == "model"
    assert assistant.parts[0].text == "Let me look."
    assert assistant.parts[1].function_call.name == "search_products"
    assert assistant.parts[1].thought_signature == b"sig"
    tool = to_content(ChatMessage(role="tool", tool_name="search_products", tool_result={"count": 2}))
    assert tool.role == "user" and tool.parts[0].function_response.response == {"result": {"count": 2}}


def test_response_parsing_text_calls_usage_and_hidden_thoughts():
    reply = from_response(response(
        types.Part(text="internal reasoning", thought=True),
        types.Part(text="Here you go."),
        types.Part(function_call=types.FunctionCall(name="search_products", args={"need": "running"}, id="c9"),
                   thought_signature=b"abc"),
    ))
    assert reply.text == "Here you go."  # thoughts are never shown to the user
    assert reply.tool_calls[0].args == {"need": "running"} and reply.tool_calls[0].meta == {"thought_signature": b"abc"}
    assert reply.usage.input_tokens == 120 and reply.usage.output_tokens == 35  # output includes thinking tokens


def test_empty_candidate_is_safe():
    reply = from_response(types.GenerateContentResponse(candidates=[]))
    assert reply.text == "" and reply.tool_calls == []


async def test_request_shape():
    p = provider([response(types.Part(text="ok"))])
    await p.complete("SYSTEM", [ChatMessage(role="user", text="hi")], [SPEC])
    request = p.client.models.requests[0]
    assert request["model"] == "gemini-test"
    assert request["config"].system_instruction == "SYSTEM"
    assert request["config"].automatic_function_calling.disable is True
    assert request["config"].tools[0].function_declarations[0].name == "search_products"


async def test_retries_rate_limit_then_succeeds(monkeypatch):
    sleeps = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(gemini.asyncio, "sleep", no_sleep)
    p = provider([errors.APIError(429, {"error": {"message": "quota"}}),
                  errors.APIError(503, {"error": {"message": "overloaded"}}),
                  response(types.Part(text="done"))])
    reply = await p.complete("s", [ChatMessage(role="user", text="hi")], [])
    assert reply.text == "done" and sleeps == [1, 2]


async def test_client_error_is_not_retried():
    p = provider([errors.APIError(400, {"error": {"message": "bad request"}})])
    with pytest.raises(LLMError, match="400"):
        await p.complete("s", [ChatMessage(role="user", text="hi")], [])
    assert len(p.client.models.requests) == 1


async def test_timeout_is_retried_then_succeeds(monkeypatch):
    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(gemini.asyncio, "sleep", no_sleep)
    p = provider([TimeoutError(), response(types.Part(text="late but fine"))])
    reply = await p.complete("s", [ChatMessage(role="user", text="hi")], [])
    assert reply.text == "late but fine" and len(p.client.models.requests) == 2


async def test_network_error_after_all_retries_becomes_llm_error(monkeypatch):
    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(gemini.asyncio, "sleep", no_sleep)
    p = provider([httpx.ConnectError("down")] * 4)
    with pytest.raises(LLMError, match="ConnectError"):
        await p.complete("s", [ChatMessage(role="user", text="hi")], [])
    assert len(p.client.models.requests) == 4


async def test_fallback_model_takes_over_after_first_failure(monkeypatch):
    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(gemini.asyncio, "sleep", no_sleep)
    p = GeminiProvider(api_key="test", model="main", fallback_model="backup",
                       client=FakeClient([errors.APIError(503, {"error": {"message": "high demand"}}),
                                          response(types.Part(text="from backup"))]))
    reply = await p.complete("s", [ChatMessage(role="user", text="hi")], [])
    assert reply.text == "from backup"
    assert [r["model"] for r in p.client.models.requests] == ["main", "backup"]
    assert p.last_model == "backup"


async def test_no_fallback_keeps_the_main_model(monkeypatch):
    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(gemini.asyncio, "sleep", no_sleep)
    p = provider([errors.APIError(503, {"error": {"message": "high demand"}}), response(types.Part(text="ok"))])
    await p.complete("s", [ChatMessage(role="user", text="hi")], [])
    assert [r["model"] for r in p.client.models.requests] == ["gemini-test", "gemini-test"]


def test_fallback_and_timeout_come_from_env(tmp_path):
    from app.config import load_settings

    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=1:x\nLLM_PROVIDER=gemini\nGEMINI_API_KEY=k\nGEMINI_FALLBACK_MODEL=backup\n"
                   "GEMINI_TIMEOUT_S=12\n", encoding="utf-8")
    s = load_settings(env)
    assert s.gemini_fallback_model == "backup" and s.gemini_timeout_s == 12.0
