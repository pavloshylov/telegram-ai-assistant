"""Ollama provider: request/response conversion, errors and config (no real Ollama needed)."""
import httpx
import pytest

from app.config import load_settings
from app.llm.base import ChatMessage, LLMError, ToolCall, ToolSpec
from app.llm.ollama import OllamaProvider

TOOL = ToolSpec("search_items", "Find items", {"type": "object", "properties": {"q": {"type": "string"}}})


def provider(handler, **kw) -> OllamaProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider("test-model", client=client, **kw)


async def test_text_reply_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"message": {"role": "assistant", "content": " Hello "}, "prompt_eval_count": 12, "eval_count": 3})

    reply = await provider(handler).complete("be nice", [ChatMessage("user", "hi")], [TOOL])
    assert reply.text == "Hello" and not reply.tool_calls
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (12, 3)
    assert '"stream":false' in seen["body"].replace(" ", "") and "search_items" in seen["body"] and "be nice" in seen["body"]


async def test_tool_call_parsed_and_history_converted():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode())
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "search_items", "arguments": {"q": "earbuds"}}}]}})

    p = provider(handler)
    reply = await p.complete("sys", [ChatMessage("user", "earbuds?")], [TOOL])
    assert [(c.name, c.args) for c in reply.tool_calls] == [("search_items", {"q": "earbuds"})]
    history = [
        ChatMessage("user", "earbuds?"),
        ChatMessage("assistant", tool_calls=[ToolCall("search_items", {"q": "earbuds"}, id="x")]),
        ChatMessage("tool", tool_name="search_items", tool_result={"items": [1]}),
    ]
    await p.complete("sys", history, [TOOL])
    assert '"role":"tool"' in bodies[1].replace(" ", "") and '"tool_name":"search_items"' in bodies[1].replace(" ", "")


async def test_http_error_becomes_llm_error():
    p = provider(lambda r: httpx.Response(500, text="boom"), retries=0)
    with pytest.raises(LLMError):
        await p.complete("s", [ChatMessage("user", "x")], [])


async def test_connection_error_retries_then_fails(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("down")

    async def no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.ollama.asyncio.sleep", no_sleep)
    with pytest.raises(LLMError):
        await provider(handler, retries=1).complete("s", [ChatMessage("user", "x")], [])
    assert len(calls) == 2


def test_config_accepts_ollama(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=ollama\nOLLAMA_MODEL=llama3.2:3b\nOLLAMA_NUM_CTX=2048\n", encoding="utf-8")
    s = load_settings(env, require_bot_token=False)
    assert (s.llm_provider, s.ollama_model, s.ollama_num_ctx) == ("ollama", "llama3.2:3b", 2048)
