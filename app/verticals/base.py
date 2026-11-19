"""A vertical = one business plugged into the same engine: persona, knowledge base, catalog and tools."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ToolCall, ToolSpec

log = logging.getLogger(__name__)


@dataclass
class CardButton:
    text: str
    action: str  # e.g. "book:NH-103" -> handled by the bot, not by the model


@dataclass
class Card:
    item_id: str
    title: str
    lines: list[str]
    buttons: list[CardButton] = field(default_factory=list)


@dataclass
class ToolOutput:
    data: dict[str, Any]  # what the model sees
    cards: list[Card] = field(default_factory=list)  # what the user sees
    handoff: bool = False


@dataclass
class ToolContext:
    session: AsyncSession
    rank_items: Callable[[str, list[str]], list[str]] | None = None  # semantic re-ranking of catalog ids


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolOutput]]


@dataclass
class Tool:
    spec: ToolSpec
    handler: ToolHandler


@dataclass
class LeadAction:
    kind: str  # stored in leads.kind
    prompt: str  # asked before the phone number


@dataclass
class Vertical:
    key: str
    company: str
    assistant_name: str
    domain: str  # "flats for rent and sale in Lisbon"
    persona: str
    welcome: str
    examples: list[str]
    human_button: str
    tools: list[Tool]
    knowledge_dir: Path
    catalog_model: Any
    catalog_csv: Path
    item_text: Callable[[Any], str]  # text used to embed a catalog item for semantic ranking
    lead_actions: dict[str, LeadAction]  # card action prefix -> lead

    @property
    def tool_specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools]

    async def run_tool(self, ctx: ToolContext, call: ToolCall) -> ToolOutput:
        tool = next((t for t in self.tools if t.spec.name == call.name), None)
        if tool is None:
            return ToolOutput({"error": f"unknown tool {call.name}"})
        args, problems = validate_args(tool.spec.parameters, call.args)
        if problems:
            return ToolOutput({"error": "invalid arguments", "details": problems})
        try:
            return await tool.handler(ctx, args)
        except Exception:  # noqa: BLE001 — the model gets a clean error instead of the bot crashing
            log.exception("tool %s failed", call.name)
            return ToolOutput({"error": f"{call.name} failed, apologise and offer a human"})


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Light JSON-Schema check: known keys only, type coercion for numbers sent as strings, enums, ranges."""
    clean: dict[str, Any] = {}
    problems: list[str] = []
    properties = schema.get("properties", {})
    for key, value in (args or {}).items():
        prop = properties.get(key)
        if prop is None or value is None:
            continue  # ignore unknown / empty arguments instead of failing the turn
        kind = prop.get("type")
        try:
            if kind == "integer":
                value = int(float(value))
            elif kind == "number":
                value = float(value)
            elif kind == "boolean" and isinstance(value, str):
                value = value.strip().lower() in {"true", "yes", "1"}
            elif kind == "string":
                value = str(value).strip()
        except (TypeError, ValueError):
            problems.append(f"{key}: expected {kind}")
            continue
        if "enum" in prop and value not in prop["enum"]:
            problems.append(f"{key}: must be one of {prop['enum']}")
            continue
        if "minimum" in prop and value < prop["minimum"]:
            problems.append(f"{key}: must be >= {prop['minimum']}")
            continue
        clean[key] = value
    for key in schema.get("required", []):
        if key not in clean:
            problems.append(f"{key}: required")
    return clean, problems


HANDOFF_SPEC = ToolSpec(
    name="handoff_to_human",
    description=(
        "Offer the user a human colleague. Use when the user asks for a person, when the answer is not in the "
        "context, for complaints, or for anything you cannot do. The bot then asks for a phone number."
    ),
    parameters={
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "Short reason, shown to the human colleague."}},
        "required": ["reason"],
    },
)


async def handoff(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    return ToolOutput({"status": "the bot is now asking the user for a phone number"}, handoff=True)


HANDOFF_TOOL = Tool(HANDOFF_SPEC, handoff)


def ordered(items: list[Any], ids: list[str]) -> list[Any]:
    by_id = {item.id: item for item in items}
    return [by_id[i] for i in ids if i in by_id]
