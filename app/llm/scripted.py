"""Offline demo provider: pre-written replies for sample questions, no model call.

Only the model's *words and tool choice* are scripted. Everything around them is real: retrieval decides
which sources exist (a citation placeholder only becomes [n] if retrieval actually returned that section),
the tool runs the real SQL search, and the bot renders the real results. Questions outside the script get an
honest "demo mode" answer with the closest knowledge-base passage found by retrieval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import json

from app.llm.base import ChatMessage, LLMReply, ToolCall, ToolSpec, Usage

CHARS_PER_TOKEN = 4  # rough rule of thumb for English; used only to estimate cost in demo mode


def estimate_usage(system: str, messages: list[ChatMessage], tools: list[ToolSpec], reply: LLMReply) -> Usage:
    """What a real model call of the same size would bill (prompt + history + tool schemas in, reply out)."""
    prompt = system + "".join(m.text + json.dumps(m.tool_result or {}) for m in messages)
    prompt += "".join(json.dumps(t.parameters) + t.description for t in tools)
    output = reply.text + "".join(json.dumps(c.args) for c in reply.tool_calls)
    return Usage(input_tokens=len(prompt) // CHARS_PER_TOKEN, output_tokens=max(len(output) // CHARS_PER_TOKEN, 1))

CITE_RE = re.compile(r"\{cite:([^}]+)\}")
CONTEXT_LINE_RE = re.compile(r"^\[(\d+)\] (.+)$", re.MULTILINE)


@dataclass
class Rule:
    pattern: str  # regex, case-insensitive, matched against the latest user message
    reply: str  # final answer; "{cite:Section title}" -> [n], "{count}" -> tool result count
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)

    def matches(self, text: str) -> bool:
        return re.search(self.pattern, text, re.IGNORECASE) is not None


class ScriptedProvider:
    name = "demo"

    def __init__(self, rules: list[Rule], examples: list[str]):
        self.rules = rules
        self.examples = examples

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMReply:
        reply = self._reply(system, messages)
        reply.usage = estimate_usage(system, messages, tools, reply)
        return reply

    def _reply(self, system: str, messages: list[ChatMessage]) -> LLMReply:
        last_user = max(i for i, m in enumerate(messages) if m.role == "user")
        question = messages[last_user].text
        tool_results = [m for m in messages[last_user + 1 :] if m.role == "tool"]
        rule = next((r for r in self.rules if r.matches(question)), None)
        if rule is None:
            return LLMReply(text=self.fallback(system))
        if rule.tool and not tool_results:
            return LLMReply(tool_calls=[ToolCall(name=rule.tool, args=dict(rule.args), id="demo-call")])
        result = tool_results[-1].tool_result if tool_results else {}
        return LLMReply(text=render(rule.reply, system, result or {}))

    def fallback(self, system: str) -> str:
        context = CONTEXT_LINE_RE.search(system)
        intro = "This demo runs without a live AI model, so I only answer the sample questions word for word."
        if context:
            passage = system[context.end() :].strip().split("\n\n")[0].split("\n[")[0]
            short = passage if len(passage) <= 260 else passage[:257].rsplit(" ", 1)[0] + "…"
            return f"{intro} The closest part of our FAQ is “{context.group(2)}” [1]:\n{short}"
        samples = "\n".join(f"• {e}" for e in self.examples)
        return f"{intro} Try one of these:\n{samples}"


def render(template: str, system: str, result: dict[str, Any]) -> str:
    available = {title.strip(): n for n, title in CONTEXT_LINE_RE.findall(system)}

    def cite(match: re.Match) -> str:
        wanted = match.group(1).strip().lower()
        for title, n in available.items():
            if title.lower().endswith(wanted):
                return f"[{n}]"
        return ""  # retrieval did not return this section -> no citation

    text = CITE_RE.sub(cite, template)
    text = text.replace("{count}", str(result.get("count", 0)))
    return re.sub(r" +([.,;])", r"\1", text).strip()
