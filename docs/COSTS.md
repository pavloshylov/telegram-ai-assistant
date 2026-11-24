# What the assistant costs to run

Prices are the providers' public list prices, standard tier, checked on **19 Nov 2026**:
[Gemini API pricing](https://aistudio.google.com/docs/pricing) ·
[OpenAI API pricing](https://developers.openai.com/api/docs/pricing) ·
[Claude API pricing](https://platform.claude.com/docs/en/about-claude/pricing).
Prices change often — re-check before quoting a client.

## Assumptions

- One question ≈ **3,000 input tokens** (system prompt + rules + 4 retrieved sections + history + tool schemas,
  and a second call when a search tool runs) and **≈150 output tokens** (short answers by design).
- One conversation ≈ **6 questions** → ~18,000 input + ~900 output tokens.
- Retrieval embeddings run **locally** (fastembed, multilingual MiniLM) → **$0** per query.
- The bot's own admin report (`/admin`) shows real token counts and cost once a live model is connected
  (in demo mode it shows an estimate).

## Cost per 1,000 conversations (≈ 6,000 questions)

| Model (provider) | Input / Output per 1M tokens | ≈ per 1,000 conversations | Notes |
|---|---|---|---|
| gpt-5-nano (OpenAI) | $0.05 / $0.40 | **$1.30** | cheapest listed option |
| gemini-2.5-flash-lite (Google) | $0.10 / $0.40 | **$2.20** | still on the pricing page, but the API answered 404 "no longer available to new users" for a new key on 19 Nov 2026 |
| gemini-3.1-flash-lite (Google) | $0.25 / $1.50 | **$5.85** | free tier; worked in the live test, but the API often answered 503/504 for it that day |
| Claude Haiku 5.5 (Anthropic) | $0.10 / $0.50 | **$2.30** | prompts up to 100k tokens |
| gpt-5-mini (OpenAI) | $0.25 / $2.00 | **$6.30** | |
| gemini-3.5-flash-lite (Google) | $0.30 / $2.50 | **$7.70** | default in `.env.example`; free tier; used in the [live test](LIVE_TEST.md) |
| gemini-3.8-flash (Google) | $0.75 / $3.75 | **$17** | price doubles from 1 Jan 2027 |
| Claude Sonnet 5.5 (Anthropic) | $2 / $10 | **$45** | for complex, high-stakes conversations |

Formula: `18,000 × input_price / 1M + 900 × output_price / 1M` per conversation, × 1,000.

## Measured in the live test

With gemini-3.5-flash-lite on 19 Nov 2026 ([LIVE_TEST.md](LIVE_TEST.md)): **≈ 1,750 input and 45 output tokens per
question** over 19 questions (about two model calls when a search tool runs), i.e. **≈ $0.0006 per question** and
≈ $3.8 per 1,000 six-question conversations at list price. The table above deliberately uses the higher
assumptions (3,000 in / 150 out) as a safe upper bound; longer conversations carry more history and cost more input
tokens. The free tier costs $0, but Google may use free-tier prompts to improve its products. Use a paid key for
real customers.

## WhatsApp messages (Meta fees)

The WhatsApp channel adds Meta's message fees to the model bill above; the model cost per question is the same as in
Telegram. What Meta's developer pricing page said on **23 Nov 2026** (read it again before quoting a client; the page has no
publication date):

- Since 1 Jul 2025 Meta charges **per message**, not per conversation, by message category and the recipient's country
  calling code. Template messages (marketing, utility, authentication) are charged whenever they are sent.
- Free-form **service** replies inside the 24-hour window are what this assistant sends. The page announces that from
  **1 Oct 2026** Meta charges for service messages again (and for utility messages sent inside an open window), except a free
  allowance of **1,000 service messages per business phone number per month**. A payment method on the business account is needed.
  Meta's own marketing page (whatsappbusiness.com) still says service messages are free: the two pages disagree, so check the rate card.
- Free entry points (click-to-WhatsApp ads) have their own free window.
- The per-country rates are in Meta's downloadable rate cards (USD, EUR, ...); no rate is quoted here on purpose.

How to estimate: one assistant answer is 1 to 3 WhatsApp messages (text, card, sometimes a second text), so
`WhatsApp cost per conversation = messages sent × (Meta service-message rate for the country, or 0 inside the free allowance)`.
On the **test number** nothing is charged. The usage report counts model tokens only; Meta's charges are in WhatsApp Manager.

## Optional paid embeddings (instead of the local model)

| Model | Price per 1M tokens |
|---|---|
| OpenAI text-embedding-3-small | $0.02 |
| Gemini gemini-embedding-001 | $0.15 (free tier available) |

Indexing a typical FAQ of 50 pages (~40k tokens) costs well under a cent with either.

## Takeaway

For a small business, the model bill is usually a few dollars a month; hosting (a small VPS, ~$5–10/month) costs
more than the AI. Rate limiting (`RATE_LIMIT_PER_HOUR`) caps what a single user can spend.
