# Live test of the WhatsApp channel (Meta test number)

**Date:** 24 Nov 2026 · **Channel:** WhatsApp Cloud API, Meta's **test phone number**, one recipient (the author's own
WhatsApp) · **LLM:** Google Gemini API, free tier, `gemini-3.5-flash-lite` · **Data:** the demo real estate agency
(Northwind Homes, made-up listings and documents) · **Webhook:** `python -m app.whatsapp` on a laptop behind a temporary
Cloudflare quick tunnel, removed after the test.

This is a test on **Meta's test number**, not a production deployment: no business verification, no registered business
number, no approved templates, no hosting, no customers. Phone numbers and names in the screenshots are blurred.

The tests in `tests/test_whatsapp.py` (fake Graph API) had already passed before this run. This page records what
happened with the real WhatsApp and the real model, including what went wrong.

## What was run

Six steps typed or tapped on a real phone in WhatsApp. After each step the webhook log and the database were checked.

| # | Step | Result |
|---|---|---|
| 1 | `hi` | Webhook verified by Meta (GET handshake, then signed POSTs). Welcome message with a "Try a question" list button. Delivery statuses arrived and were stored. |
| 2 | `2-bedroom to rent in Chiado up to 2400, I have a dog` | Function calling → SQL search → one short message and a listing card with two reply buttons ("Book a viewing", "Talk to an agent"). |
| 3 | `Do tenants pay an agency fee?` | Answer from the knowledge base, no `[n]` markers, no "Sources" line. The cited section (`Fees and commission › Rentals`) is stored in the conversation log. |
| 4 | `What documents do I need to rent?` | Same; cited section stored (`Renting a flat › Documents for a rental application`). |
| 5 | Tap "Book a viewing" (twice, on two different cards) | Lead saved with the WhatsApp number and profile name, **no second question for a phone number**, confirmation in WhatsApp, new-lead alert in Telegram (staff chat) with the transcript. |
| 6 | `I'd like to talk to a person` | The model called the hand-off tool; a `human` lead was saved and an alert sent; two confirmation messages in WhatsApp (see below). |

Screenshots: [`screens/whatsapp_01_hi.png`](screens/whatsapp_01_hi.png),
[`02_search`](screens/whatsapp_02_search.png), [`03_rag_fee_docs`](screens/whatsapp_03_rag_fee_docs.png),
[`05_lead_blurred`](screens/whatsapp_05_lead_blurred.png), [`06_second_lead`](screens/whatsapp_06_second_lead.png),
[`07_handoff`](screens/whatsapp_07_handoff.png), Telegram alerts:
[viewing lead](screens/telegram_alert_viewing_lead.png), [human lead](screens/telegram_alert_human_lead.png).

## What went wrong (and was fixed)

1. **Sources vanished from the log.** After the "no `[n]` in the chat" change, the style note told the model not to write
   citation markers, so it stopped citing and the log showed `sources: []` for answers that came from the documents (the
   "answers with sources" figure of the cost report would have been wrong too). Fix: the model still cites as before and
   the channel strips the markers before sending. Re-run: the sections are logged again.
2. **Two names for one button.** The welcome text (shared with Telegram) says "Talk to an agent", the WhatsApp list and
   cards said "Talk to a person". Fix: the channel uses the vertical's own button text.
3. **Stale server after a restart.** A webhook server left over from an earlier session was still listening on the port
   with old code and an expired token (`python -m app.whatsapp` then failed to bind). Found by checking the port owner;
   the old process was stopped. Not a code bug, but it would have silently run old code.
4. **Expired token.** The temporary token from the previous day answered with Graph error 190 until a new one was
   generated. This is the behaviour of the test number, documented in `WHATSAPP.md`.

## Observed, not fixed

- **A tapped reply button turns grey.** After tapping "Book a viewing" the button on that card is disabled in the
  WhatsApp client (the other button stays active). Booking another viewing needs a new card. I found no statement about
  this in the Meta docs I read, so this is an observation of the client, not a documented rule.
- **Two confirmations on hand-off.** The model's own sentence ("I'll have a colleague reach out…") is followed by the
  fixed "✅ Got it, thank you! … (+number)" message. Correct but repetitive.
- **Citation marker on a hand-off sentence.** In one run the model put `[1]` on the hand-off sentence, so that answer is
  logged with a source it does not need. It also shows as `[1]` inside the Telegram alert transcript (staff view).

## What was not tested live

- The 24-hour window (a reply after the window closed), template messages, delivery `failed` statuses and Meta's
  duplicate webhook deliveries: covered only by the fake-Graph tests.
- The Ukrainian-language, off-topic, "not in the documents" and rate-limit steps of `scripts/live_check.py`; the shop
  vertical on WhatsApp.
- Voice notes, images and other non-text input.
- PostgreSQL with this channel (the suite was run on SQLite).
- More than one recipient, more than a handful of messages, anything under load.
- A permanent token, a registered business number, Meta business verification, approved templates: **not done**.

## Setup used

Meta test number with the app's webhook field `messages` subscribed; callback URL = the tunnel + `/webhook`; Telegram
alerts sent by a separate test bot to the author's private chat. Free-tier Gemini may use prompts to improve Google's
products: a real client deployment needs a paid key.
