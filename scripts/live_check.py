"""Live check against the real Gemini API (no Telegram needed).

Runs the bot's real handlers (in-process fake Telegram API, same as the tests) with the real GeminiProvider and a
scripted conversation per vertical: filtered search (function calling), RAG answers with [n] sources, a follow-up,
a lead with a phone number, an out-of-scope question, a question the knowledge base cannot answer, hand-off to a
person, the /admin cost report and the per-user rate limit.

Every model call is recorded: latency, tokens, tools requested, errors/retries. A transcript is written to
data/live_runs/<timestamp>.md (git-ignored). The API key is read from .env and is never printed or written.

Usage:  python scripts/live_check.py [realty|shop|both] [--delay 6] [--model gemini-3.5-flash-lite] [--fallback gemini-3.1-flash-lite]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bot.handlers import router  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.db import init_db, make_engine, make_session_maker  # noqa: E402
from app.engine import build_agent  # noqa: E402
from app.llm.base import ChatMessage, LLMError, LLMReply, ToolSpec  # noqa: E402
from app.llm.gemini import GeminiProvider  # noqa: E402
from app.main import build_dispatcher  # noqa: E402
from app.rag import Embedder  # noqa: E402
from tests.fake_telegram import FakeTelegram, Harness  # noqa: E402

ADMIN_ID = 100
OUT = ROOT / "data" / "live_runs"

# (kind, payload). kinds: say | book (click first card's action button) | contact | admin
SCENARIOS: dict[str, list[tuple[str, str]]] = {
    "realty": [
        ("say", "2-bedroom to rent in Chiado up to €2,400, I have a dog"),
        ("say", "Do tenants pay an agency fee?"),
        ("say", "What documents do I need to rent?"),
        ("say", "Which of those has a balcony?"),
        ("book", "Book a viewing"),
        ("contact", "+351 912 345 678"),
        ("say", "Шукаю студію на продаж у Белені до 300 тисяч євро"),
        ("say", "What's the weather in Lisbon tomorrow?"),
        ("say", "Do you offer a cleaning service after move-out?"),
        ("say", "I'd like to talk to a person"),
        ("contact", "+351 913 000 111"),
        ("admin", ""),
    ],
    "shop": [
        ("say", "Earbuds for running under €130"),
        ("say", "How long does delivery to Ukraine take?"),
        ("say", "Can I return earbuds I already opened?"),
        ("say", "Which of them has the best battery life?"),
        ("book", "Order"),
        ("contact", "+48 600 100 200"),
        ("say", "Які закриті навушники до 100 євро для дому?"),
        ("say", "Write me a poem about cats"),
        ("say", "Do you offer a student discount?"),
        ("say", "I'd like to talk to a person"),
        ("contact", "+48 600 100 300"),
        ("admin", ""),
    ],
}
RATE_QUESTIONS = ["Hi", "What do you sell?", "Do you ship abroad?", "And returns?"]  # limit is set to 3 for this part


@dataclass
class Call:
    seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    tools: list[str] = field(default_factory=list)
    args: list[dict] = field(default_factory=list)
    model: str = ""
    error: str = ""


class Recorder:
    """Wraps a provider and records every model call."""

    def __init__(self, inner: GeminiProvider):
        self.inner = inner
        self.name = inner.name
        self.calls: list[Call] = []

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMReply:
        start = time.perf_counter()
        try:
            reply = await self.inner.complete(system, messages, tools)
        except LLMError as exc:
            self.calls.append(Call(time.perf_counter() - start, error=str(exc)))
            raise
        self.calls.append(Call(time.perf_counter() - start, reply.usage.input_tokens, reply.usage.output_tokens,
                               [t.name for t in reply.tool_calls], [t.args for t in reply.tool_calls],
                               self.inner.last_model))
        return reply


class WarningCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.name}: {record.getMessage()}")


def wants_phone(env: Harness, user) -> bool:
    kb = env.chat(user).reply_keyboard
    return bool(kb and any(b.request_contact for row in kb.keyboard for b in row))


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def run_vertical(vertical: str, base: Settings, embedder: Embedder, tmp: Path, delay: float, log: list[str]):
    settings = Settings(
        bot_token="42:LIVE", vertical=vertical, llm_provider="gemini", gemini_api_key=base.gemini_api_key,
        gemini_model=base.gemini_model, gemini_fallback_model=base.gemini_fallback_model,
        gemini_timeout_s=base.gemini_timeout_s, price_input_per_m=base.price_input_per_m,
        price_output_per_m=base.price_output_per_m, admin_ids=frozenset({ADMIN_ID}), data_dir=tmp,
        database_url=f"sqlite+aiosqlite:///{(tmp / f'{vertical}.db').as_posix()}",
    )
    engine = make_engine(settings.database_url)
    await init_db(engine)
    session_maker = make_session_maker(engine)
    agent = await build_agent(settings, session_maker, embedder)
    recorder = Recorder(agent.provider)
    agent.provider = recorder
    env = Harness(build_dispatcher(settings, session_maker, agent), FakeTelegram())
    owner, guest = env.user(ADMIN_ID, "Owner", "owner_live"), env.user(201, "Anna", "anna_live")
    rows = []
    try:
        log.append(f"\n## {vertical} — model {settings.gemini_model}\n")
        await env.start(guest)
        for n, (kind, payload) in enumerate(SCENARIOS[vertical], 1):
            if kind != "contact" and wants_phone(env, guest) and kind != "admin":
                await env.send(guest, "/cancel")
                log.append("_(phone prompt left open by the previous step → /cancel sent)_\n")
            chat_user = owner if kind == "admin" else guest
            before, calls_before = len(env.chat(chat_user).bubbles), len(recorder.calls)
            started = time.perf_counter()
            if kind == "say":
                await env.send(guest, payload)
            elif kind == "book":
                bubble = next((b for b in reversed(env.chat(guest).bot_bubbles())
                               if b.markup and any(payload in t for t in b.button_texts())), None)
                if bubble is None:
                    log.append(f"### {n}. [click '{payload}'] — **no card with this button on screen**\n")
                    rows.append((n, kind, payload, 0.0, 0, 0, [], "no card", False, False, 0))
                    continue
                await env.click_in(guest, bubble, next(t for t in bubble.button_texts() if payload in t))
            elif kind == "contact":
                await env.send_contact(guest, payload)
            else:
                await env.send(owner, "/admin")
            elapsed = time.perf_counter() - started
            new = env.chat(chat_user).bubbles[before:]
            bot_new = [b for b in new if b.sender != "user"]
            new_calls = recorder.calls[calls_before:]
            tools = [f"{n}{a}" for c in new_calls for n, a in zip(c.tools, c.args)]
            models = sorted({c.model for c in new_calls if c.model})
            cards = [b for b in bot_new if b.markup and any(("Book" in t or "Order" in t) for t in b.button_texts())]
            reply = bot_new[0].text if bot_new else ""
            sources = "Sources:" in reply
            handoff = wants_phone(env, guest) if kind == "say" else False
            errors = [c.error for c in new_calls if c.error]
            tin, tout = sum(c.input_tokens for c in new_calls), sum(c.output_tokens for c in new_calls)
            log.append(f"### {n}. [{kind}] {payload or '/admin'}\n")
            log.append(f"- model calls: {len(new_calls)} · tools: {tools or '—'} · {tin} in / {tout} out tokens · {elapsed:.1f}s"
                       f" · answered by: {', '.join(models) or '—'} · cards: {len(cards)} · sources: {'yes' if sources else 'no'} · hand-off prompt: {'yes' if handoff else 'no'}"
                       + (f" · ERRORS: {errors}" if errors else ""))
            for b in bot_new:
                log.append("  > " + strip_html(b.text).replace("\n", "\n  > "))
            log.append("")
            rows.append((n, kind, payload, elapsed, tin, tout, tools, "error" if errors else "ok", sources, handoff, len(cards)))
            if kind in {"say", "book"} and delay:
                await asyncio.sleep(delay)
        return rows, recorder.calls
    finally:
        router._parent_router = None
        await engine.dispose()


async def run_rate_limit(base: Settings, embedder: Embedder, tmp: Path, delay: float, log: list[str]) -> list[str]:
    settings = Settings(
        bot_token="42:LIVE", vertical="shop", llm_provider="gemini", gemini_api_key=base.gemini_api_key,
        gemini_model=base.gemini_model, gemini_fallback_model=base.gemini_fallback_model,
        gemini_timeout_s=base.gemini_timeout_s, admin_ids=frozenset({ADMIN_ID}), data_dir=tmp, rate_limit_per_hour=3,
        database_url=f"sqlite+aiosqlite:///{(tmp / 'rate.db').as_posix()}",
    )
    engine = make_engine(settings.database_url)
    await init_db(engine)
    session_maker = make_session_maker(engine)
    agent = await build_agent(settings, session_maker, embedder)
    env = Harness(build_dispatcher(settings, session_maker, agent), FakeTelegram())
    user = env.user(301, "Rate", "rate_live")
    answers = []
    try:
        for q in RATE_QUESTIONS:
            await env.send(user, q)
            answers.append(strip_html(env.last(user).text)[:120])
            await asyncio.sleep(delay)
    finally:
        router._parent_router = None
        await engine.dispose()
    log.append("\n## rate limit (3 questions/hour, 4 sent)\n")
    log.extend(f"- Q{n}: {q!r} → {a!r}" for n, (q, a) in enumerate(zip(RATE_QUESTIONS, answers), 1))
    return answers


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("vertical", nargs="?", default="both", choices=["realty", "shop", "both"])
    parser.add_argument("--delay", type=float, default=6.0, help="seconds between questions (free-tier RPM)")
    parser.add_argument("--model", default="")
    parser.add_argument("--fallback", default="", help="fallback model for 503/timeouts")
    parser.add_argument("--skip-rate", action="store_true")
    args = parser.parse_args()

    base = load_settings(ROOT / ".env") if (ROOT / ".env").exists() else None
    if base is None or not base.gemini_api_key:
        raise SystemExit("Put GEMINI_API_KEY (and BOT_TOKEN=any text for this script) into .env first.")
    if args.model:
        base = Settings(**{**base.__dict__, "gemini_model": args.model})
    if args.fallback:
        base = Settings(**{**base.__dict__, "gemini_fallback_model": args.fallback})
    counter = WarningCounter()
    logging.getLogger().addHandler(counter)
    tmp = Path(tempfile.mkdtemp(prefix="assistant_live_"))
    embedder = Embedder(cache_dir=str(ROOT / "data" / "models"))
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    log = [f"# Live check {stamp}\n", f"Model: `{base.gemini_model}` · fallback: `{base.gemini_fallback_model or 'none'}` · pause between questions {args.delay}s\n"]
    summary = []
    all_calls: list[Call] = []
    for vertical in (["realty", "shop"] if args.vertical == "both" else [args.vertical]):
        rows, calls = await run_vertical(vertical, base, embedder, tmp, args.delay, log)
        all_calls += calls
        summary.append((vertical, rows))
    if not args.skip_rate:
        await run_rate_limit(base, embedder, tmp, args.delay, log)

    ok = [c for c in all_calls if not c.error]
    lat = sorted(c.seconds for c in ok)
    tin, tout = sum(c.input_tokens for c in ok), sum(c.output_tokens for c in ok)
    cost = tin / 1e6 * base.price_input_per_m + tout / 1e6 * base.price_output_per_m
    log.append("\n## Totals\n")
    log.append(f"- model calls: {len(all_calls)} ({len(all_calls) - len(ok)} failed after retries)")
    log.append(f"- tokens: {tin} in / {tout} out → ${cost:.4f} at list price (free tier: $0)")
    if lat:
        log.append(f"- latency per model call: median {lat[len(lat) // 2]:.1f}s · p90 {lat[int(len(lat) * 0.9) - 1]:.1f}s · max {lat[-1]:.1f}s")
    log.append(f"- provider warnings (retries etc.): {len(counter.lines)}")
    log.extend(f"  - {line}" for line in counter.lines[:20])
    text = "\n".join(log)
    for secret in {base.gemini_api_key}:
        text = text.replace(secret, "***")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{stamp}.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nTranscript saved: {path}")


if __name__ == "__main__":
    asyncio.run(main())
