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
| gemini-2.5-flash-lite (Google) | $0.10 / $0.40 | **$2.20** | free tier for development |
| Claude Haiku 5.5 (Anthropic) | $0.10 / $0.50 | **$2.30** | prompts up to 100k tokens |
| gpt-5-mini (OpenAI) | $0.25 / $2.00 | **$6.30** | |
| gemini-3.5-flash-lite (Google) | $0.30 / $2.50 | **$7.70** | default in `.env.example`; free tier |
| gemini-3.8-flash (Google) | $0.75 / $3.75 | **$17** | price doubles from 1 Jan 2027 |
| Claude Sonnet 5.5 (Anthropic) | $2 / $10 | **$45** | for complex, high-stakes conversations |

Formula: `18,000 × input_price / 1M + 900 × output_price / 1M` per conversation, × 1,000.

## Optional paid embeddings (instead of the local model)

| Model | Price per 1M tokens |
|---|---|
| OpenAI text-embedding-3-small | $0.02 |
| Gemini gemini-embedding-001 | $0.15 (free tier available) |

Indexing a typical FAQ of 50 pages (~40k tokens) costs well under a cent with either.

## Takeaway

For a small business, the model bill is usually a few dollars a month; hosting (a small VPS, ~$5–10/month) costs
more than the AI. Rate limiting (`RATE_LIMIT_PER_HOUR`) caps what a single user can spend.
