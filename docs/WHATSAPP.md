# WhatsApp channel (Cloud API)

The same assistant engine as in Telegram and on the website, as a WhatsApp chat: `python -m app.whatsapp` starts a
FastAPI webhook server (`app/whatsapp/`) that Meta calls for every incoming message. RAG, function calling,
guardrails, the rate limit, the token log, leads and the usage report are shared code (`app/chat.py`, `app/agent.py`);
the channel only translates WhatsApp messages to and from them.

**Status (honest):** built and covered by 34 tests against an in-process fake of the Graph API.
tested live on Meta's **test number** (24 Nov 2026: welcome, search, two document questions, booking lead, hand-off,
Telegram alerts), with the results and failures in [LIVE_TEST_WHATSAPP.md](LIVE_TEST_WHATSAPP.md). Not tested live: the
24-hour window, delivery failures, duplicates, non-text input. Not done: a real business number, business verification, approved message templates,
a permanent token, hosting. Everything below that is about production describes what Meta requires, not what was done.

## What was checked in Meta's documentation (23 Nov 2026)

Read on developers.facebook.com (Cloud API and "Business Messaging" docs). Things that matter for the code:

| Topic | What the docs say |
|---|---|
| Webhook verification | `GET` with `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge`; compare the token, answer with the challenge value |
| Signature | header `X-Hub-Signature-256: sha256=<signature>`, SHA-256 signature made with the app secret over the payload. The page does not spell out "HMAC over the raw body"; that is the usual reading and what the code does (checked on the raw bytes before parsing) |
| Retries / duplicates | non-200 answers are retried with decreasing frequency (the WhatsApp page says up to 7 days, the generic Graph page says 36 hours); retries "can result in duplicate webhook notifications", your server must deduplicate. No dedup key is documented; the code uses the message id (`wamid`) |
| Incoming messages | `entry[].changes[].value.messages[]` with `id`, `from`, `timestamp`, `type`; `text.body`; `interactive.button_reply.{id,title}`, `interactive.list_reply.{id,title,description}`; unsupported content arrives as `type: "unsupported"` with error 131051. `from` is omitted when the user has adopted a username and their phone number is hidden (not supported here: the message is logged and skipped) |
| Statuses | `value.statuses[]`: `sent`, `delivered`, `read`, `failed` (+ `errors[]`). A message read on arrival may skip `delivered` |
| Sending | `POST https://graph.facebook.com/<version>/<phone-number-id>/messages`, `Authorization: Bearer <token>`; docs examples use **v27.0** (default in `WHATSAPP_GRAPH_VERSION`). A 200 only means "accepted"; delivery is reported by webhook |
| Limits | text body ≤ 4096; reply buttons: ≤ 3, title ≤ 20, body ≤ 1024, id ≤ 256; list: ≤ 10 rows in total, button label ≤ 20, row title ≤ 24, row description ≤ 72, row id ≤ 200, body ≤ 4096 |
| 24-hour window | a user message opens a 24-hour customer service window (every new user message resets it). Inside it any message type may be sent; after it only approved **template** messages. Error 131047 means the window is closed |
| Mark as read + typing | same endpoint, `status: "read"` + `message_id` (+ `typing_indicator: {type: "text"}`); the indicator disappears on our reply or after 25 s |
| Errors | `error.code` + `error.error_data.details`; 190 token expired, 130429 throughput reached (retry later), 131047 window closed, 131051 unsupported type |

Not found in the doc pages I read, so **not** treated as confirmed: the test number's recipient limit and the temporary
token's lifetime. In the n8n lab on this project the test number accepted at most 5 recipients and a token that
expires within hours (see that repo's notes); check the dashboard for the current values.

### What differs from Telegram

| | Telegram | WhatsApp |
|---|---|---|
| User identity | Telegram id, phone must be asked | the phone number is the identity: **no second "share your number" step**, the lead carries it |
| Buttons | many inline buttons per message, persistent keyboard | max **3 reply buttons** per message or one **list** (10 rows); no persistent keyboard |
| Formatting | HTML | `*bold*`, `_italic_`; the code converts model/card text |
| Text length | 4096 | 4096, longer answers are split at paragraph boundaries into several messages |
| Free-form replies | always | only within 24 h of the user's last message |
| Delivery | immediate error from the API | the API accepts first, delivery/failure arrives later as a status webhook |
| Duplicates | no | Meta retries webhooks: deduplicated by message id |
| Voice, images, location | rejected by the bot | same: a polite "text only" reply |

## How the channel behaves

- `GET /webhook` – verification handshake. `POST /webhook` – signature check, then **200 right away**; each new message
  runs in the background (the model can take seconds and Meta retries slow webhooks). `GET /health`,
  `GET /api/admin/stats` (only with `ADMIN_API_KEY`, header `X-Admin-Key`).
- **Idempotency:** the message id is stored in `wa_events` before processing; a repeat is counted and ignored.
- **Order:** one conversation processes one message at a time (a lock per user), so two quick messages are answered in order.
- **Input:** text, list-row and button taps. A tapped example row asks that example question. Words like "hi", "menu",
  "help" send the welcome text and a list (sample questions + "Talk to a person"). Anything else (voice, image, location,
  sticker) gets a friendly "text only" reply.
- **Output:** the model's answer as a plain chat message (no `[n]` markers and no "Sources" line: the model still answers
  only from the retrieved sections and the cited ones stay in the conversation log; `WHATSAPP_SHOW_SOURCES=true` brings the
  markers and the line back), then one
  message per item card with reply buttons: the card's own action ("📅 Book a viewing" / "🛒 Order") and
  "👤 Talk to a person". Button titles are cut to 20 characters, card text to 1024.
- **Leads:** tapping a card button creates the lead immediately with `+<WhatsApp number>` and the profile name, and
  staff get the same Telegram alert as for the website (set `BOT_TOKEN` + `ADMIN_IDS`; the alert says "(WhatsApp)").
  "Talk to a person", a model hand-off or a model outage create a `human` lead; only one per hour per user (a repeat
  just confirms). Leads from all channels share one table; the CSV export has a `channel` column.
- **24-hour window:** if the inbound message is older than 24 h when we process it, or Meta answers 131047, nothing is
  sent: the situation is logged (`customer service window is closed ... needs an approved template`), the event is
  stored as `window_closed`, and a late button tap still saves its lead. Sending a template to re-open the
  conversation is **not implemented**.
- **Errors:** Graph errors never crash the webhook (it still answers 200, otherwise Meta would retry for days). 130429 and
  5xx are retried twice; a failed send is stored as `send_failed`. Every sent message is stored in `wa_outbound` with its
  wamid, and status webhooks update it (`failed` keeps Meta's error code), so a missed delivery is visible in the database,
  not silent as in the n8n demos.
- **Tone:** the channel adds a style note to the system prompt: short, human texting style in the user's language, the
  answer first, one light follow-up question, at most one emoji, no filler phrases. If asked, the assistant says honestly that
  it is the company's AI assistant. Fixed messages (errors, lead confirmation) are worded the same way.
- **Limits and budget:** the same per-user hourly limit as Telegram (`RATE_LIMIT_PER_HOUR`), the same history window and
  token log; `/api/admin/stats` returns the same report as `/admin`.
- **Not supported:** message templates, media, location, groups, WhatsApp usernames without a phone number,
  multi-business routing. State is in-process (locks), so run **one** worker per phone number.

## Setup on Meta's test number

1. [developers.facebook.com](https://developers.facebook.com/apps/) → Create app → use case **Connect with customers through WhatsApp**
   (you need a Meta business portfolio; it may stay unverified for the test number).
2. App Dashboard → WhatsApp → **API Setup**: Meta created a **test business phone number**. Copy its **Phone number ID**
   (not the phone number) and click *Generate access token* (temporary). In "To", add your own WhatsApp number as a recipient.
3. App Dashboard → **App settings → Basic** → *App secret* → Show.
4. `.env` (never committed):

   ```
   WHATSAPP_TOKEN=...             # the access token
   WHATSAPP_PHONE_NUMBER_ID=...
   WHATSAPP_VERIFY_TOKEN=...      # any random string you invent
   WHATSAPP_APP_SECRET=...
   VERTICAL=realty                # or shop
   LLM_PROVIDER=gemini            # or demo
   BOT_TOKEN= / ADMIN_IDS=        # optional: lead alerts to Telegram
   ```
5. `python -m app.whatsapp` (listens on `127.0.0.1:8001`). Meta needs a public **HTTPS** URL: for a test use a
   temporary tunnel, e.g. `cloudflared tunnel --url http://127.0.0.1:8001` (it exposes only this port; stop it afterwards).
6. App Dashboard → WhatsApp → **Webhooks → Configuration** (the docs call the path "WhatsApp > Webhooks > Configuration"):
   Callback URL `https://<tunnel>/webhook`, Verify token = `WHATSAPP_VERIFY_TOKEN` → *Verify and save*; then subscribe to the
   **messages** field.
7. Write "hi" from the allowed WhatsApp number to the test number. The reply window is open for 24 h after each of your messages.

Test-number limits to keep in mind: only the recipients you added can be messaged; the temporary token expires and
every run needs a fresh one in `.env`; the number is shared infrastructure, not a brand.

## What production needs (Meta requirements, not done here)

- A **registered business phone number** you own (the test number is not for customers): registration via the API
  (`register` + verification code + two-step PIN) and an **approved display name**. New business portfolios start with a
  cap of two registered numbers.
- **Business verification** of the Meta business portfolio (requirements are in Meta's Business Help Center; not read here)
  to raise limits.
- A **permanent token**: a system user in Business Settings with the app and the WhatsApp account assigned (full
  control), token permissions `business_management`, `whatsapp_business_management`, `whatsapp_business_messaging`.
- **Approved templates** (marketing, utility, authentication) for anything sent outside the 24-hour window, plus a way to
  collect and store opt-in. The code does not send templates yet.
- Hosting with HTTPS, one worker, backups of the database, a data-retention and consent policy, and a **paid LLM key**
  (free-tier Gemini prompts may be used by Google).
- Monitoring of `wa_outbound` failures and of the token expiry.

Costs: see [COSTS.md](COSTS.md#whatsapp-messages-meta-fees). Meta's prices depend on the message category and the
recipient's country; read the current rate card, do not rely on this repo.
