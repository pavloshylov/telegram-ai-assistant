# Live test against the Gemini API

**Date:** 19 Nov 2026 · **API:** Google Gemini API, free tier (Google AI Studio key) ·
**Models:** `gemini-3.5-flash-lite` (default) and `gemini-3.1-flash-lite` · **Code state:** the commit that contains this file.

Until this test the Gemini provider had only been checked against the SDK's types with a fake client. This page records
what happened when the whole assistant ran against the real API, including what went wrong the first time.

## What was run

`scripts/live_check.py` drives the bot's **real handlers** (the same code path as in Telegram) through an in-process
fake Bot API, with the real `GeminiProvider`, real retrieval (local embeddings), real SQL search, the real lead flow and
database. Per vertical (real estate agency, online store) it sends 12 steps:

| # | Step | What it checks |
|---|---|---|
| 1 | Free-text search with filters ("2-bedroom to rent in Chiado up to €2,400, I have a dog") | function calling → validated SQL filters → card |
| 2–3 | Two policy questions (fees, documents / delivery, returns) | RAG answer with cited `[n]` sources |
| 4 | Follow-up about the card just shown | history + `get_*` tool, answer consistent with the card |
| 5–6 | Tap "Book a viewing" / "Order", share a phone number | lead saved, staff notified |
| 7 | Ukrainian request | replies in the user's language, search still works |
| 8 | Off-topic request (weather / a poem) | polite refusal, no hand-off |
| 9 | Question the documents cannot answer | says it is not sure and offers a person |
| 10–11 | "I'd like to talk to a person", phone number | hand-off lead |
| 12 | `/admin` | token counts and cost report |
| + | Rate limit set to 3 per hour, 4 questions sent | 4th is refused without calling the model |

Every model call is recorded (latency, tokens, tools and their arguments, model that answered, errors/retries).
Full transcripts of the two final runs: [`live_runs/`](live_runs/).

## Result (final code)

| | `gemini-3.5-flash-lite` + fallback `gemini-3.1-flash-lite` | `gemini-3.1-flash-lite` alone |
|---|---|---|
| Model calls | 25 | 25 |
| Failed after all retries | 0 | 1 (bot answered "a colleague can help", phone button shown) |
| Retries (503/504) | 0 | 28 |
| Latency per call: median / p90 / max | 1.2 s / 7.0 s / 9.3 s | 17.9 s / 46.6 s / 108.6 s |
| Tokens (in / out) | 33,255 / 836 | 32,515 / 916 |
| Cost at list price | $0.0121 | $0.0095 |
| Search with filters, cards | correct in both verticals | correct |
| Cited sources on policy questions | correct section every time | correct |
| Follow-up consistent with the card | yes | yes |
| Off-topic refused without hand-off | yes (both) | yes (both) |
| Hand-off after "talk to a person" | yes (both) | shop: yes; realty: the API failed with 503 on that step and the bot's failure path offered a person with the phone button |
| Out-of-documents question → hand-off | realty: promised a colleague, **did not call the tool**; shop: correct | correct, but the shop reply said "I have notified a colleague" before any phone number was given |
| Rate limit | 4th question refused | same |

Reading the table: with this test (one scenario, one day) `gemini-3.5-flash-lite` was clearly the more reliable model.
`gemini-3.1-flash-lite` answered correctly when it answered, but the API kept returning 503/504 for it that afternoon, so
it was slow and once failed completely. Latency of the **API itself** varied a lot during the day for both models
(about 1 s in quiet periods, 15–25 s on a 15-token prompt, more than 100 s on some 503 answers). Those are free-tier,
shared-capacity conditions; a paid key may behave differently, and this test does not show that.

No 429 (rate limit) answers appeared in roughly 140 scenario calls plus probes that day. Google does not publish
free-tier RPM/RPD for Flash-Lite on its public pages (the numbers are visible only inside AI Studio), so none are quoted here.

## What did not work the first time (and what changed)

| Finding in the first live run | Fix | Checked again live |
|---|---|---|
| 503 "high demand" answers took ~100 s each to arrive → answers up to 219 s; the SDK's timeout error (`TimeoutError`) was not an `APIError` and would have crashed the turn | 30 s per-request timeout; timeouts and network errors are retried; optional `GEMINI_FALLBACK_MODEL` takes over after the first failed attempt (thought signatures verified to work across the two models) | yes: fallback answered one step after a 504 |
| Citations `[n]` attached to search results and refusals | prompt rule: cite only facts from the context | yes, gone |
| Model promised "let me connect you with a colleague" without calling `handoff_to_human` → no phone button (seen in 2 of 5 real-estate runs) | prompt rule + a deterministic guard: such a promise (English or Ukrainian wording) is treated as a hand-off | guard covered by unit tests only; the last live run did not trigger it |
| Shop follow-up "which has the best battery?": text said one product, the cards showed a longer-lasting speaker | prompt rule: reuse the earlier filters or call `get_*`, name only what the tool returned | yes, consistent in 4 later runs |
| `gemini-3.1-flash-lite` pasted an invented JSON block (`{"cards": [...]}`) into the reply | `strip_json` removes JSON the model echoes | unit tests; not seen again |
| `gemini-3.1-flash-lite` called the hand-off tool for the weather question and for a question the documents answer | prompt rule: off-topic = decline without hand-off | yes for the weather question |
| `gemini-2.5-flash-lite` (named in the first cost table) returns 404 for new API keys | marked in [COSTS.md](COSTS.md) | – |

## Measured cost

Across the final `gemini-3.5-flash-lite` run: **about 1,750 input and 45 output tokens per question** (19 answered
questions, 33,255 / 836 tokens), i.e. **≈ $0.0006 per question** at list price ($0.30 / $2.50 per 1M tokens), or about
$3.8 per 1,000 six-question conversations. That is below the conservative estimate in [COSTS.md](COSTS.md)
(3,000 in / 150 out per question → $7.70). Short conversations only; a long history costs more input tokens.

## Website widget, same day

After the Telegram-side run, the website widget ([WIDGET.md](WIDGET.md)) was driven in headless Chrome against the **real
server with the live Gemini API** (`scripts/widget_screens.py`, `gemini-3.5-flash-lite`, fallback `gemini-3.1-flash-lite`).
Per business, on a desktop and a phone viewport: open the chat, tap a sample question (search → card), ask a policy
question (answer with sources), tap the card button, fill the phone form. Log:
[`live_runs/2026-11-19_website-widget_gemini-3.5-flash-lite.md`](live_runs/2026-11-19_website-widget_gemini-3.5-flash-lite.md).

| | Real estate | Online store |
|---|---|---|
| Questions answered (2 per viewport) | 4 / 4, every one with cited sources | 4 / 4, every one with cited sources |
| Cards rendered, card button → lead | yes, 2 leads (`viewing`), channel `web` | yes, 2 leads (`order`), channel `web` |
| Browser console / page errors | none | none |
| Answer time per question | 1.2–2.7 s | 1.2–2.7 s |
| Tokens → cost at list price | 7,866 in / 252 out → $0.0030 ($0.00075 per question) | 8,075 in / 221 out → $0.0030 ($0.00074 per question) |

Found by the run and fixed: **the store demo page was wider than a phone** (menu links overflowed to 465 px), which
widened the layout viewport and left the fixed chat bubble under page content; the first attempt of the script failed
on exactly that. The demo pages now fit 390 px and a test checks it. The Gemini API was calm during this run, so the
latencies are not comparable with the 100-second waits seen earlier in the day.

Not exercised live in the browser: hand-off to a person from the widget and the rate-limit message (both covered by
the API tests with a fake model), Telegram alerts for website leads (unit-tested against a mock Bot API, never sent).

## Known limits of this test

- **Not run inside the real Telegram app.** The handlers ran against an in-process fake Bot API, as in the test suite.
  A Telegram smoke test needs a separate bot token and has not been done.
- One scenario of 12 steps per vertical on one day, fictional data. It shows the pipeline works with a live model and
  how it failed; it is not a quality benchmark or a load test.
- Free-tier terms: Google may use free-tier prompts and responses to improve its products and humans may review them.
  Use a paid key for real customer conversations. Nothing here involved real customers.
- Docker files are provided but were never run on this machine.
- The widget was not hosted on a public server: everything ran on localhost.

## Reproduce

```bash
cp .env.example .env            # set LLM_PROVIDER=gemini, GEMINI_API_KEY=... (aistudio.google.com/apikey)
python scripts/live_check.py both --fallback gemini-3.1-flash-lite
```

The transcript is written to `data/live_runs/` (git-ignored); the key is never printed or written.
