# GreenAPI (WhatsApp) integration — technical reference

How this service talks to WhatsApp. Transport, message shapes, identifiers,
call sites, and failure modes. No product logic here.

Ground truth in this repo:

| File | Role |
|---|---|
| `app/main.py` | builds the `GreenAPIBot`, owns the polling loop |
| `app/whatsapp/handler.py` | inbound: notification → filters → agent → reply |
| `app/whatsapp/sender.py` | outbound: `ChatSender` bound to one chat |
| `app/webhook/opener.py` | outbound from a webhook thread (own client) |
| `app/followup/scheduler.py` | outbound from a scheduler thread (own client) |

---

## 1. Libraries and transport

Two packages, both from GreenAPI:

| Package | Import | Used for |
|---|---|---|
| `whatsapp-chatbot-python` | `GreenAPIBot`, `Notification` | inbound routing (`app/main.py`, `handler.py`) |
| `whatsapp-api-client-python` | `API.GreenAPI` | raw outbound calls (`opener.py`, `scheduler.py`) |

**Transport is HTTP long-polling, not inbound webhooks.** `bot.run_forever()`
blocks the main thread and pulls `ReceiveNotification` in a loop. Nothing needs
to be publicly reachable for WhatsApp to work — our only public endpoint
(`/webhook/leadme/{secret}`) belongs to the CRM, not to GreenAPI.

Consequence: WhatsApp traffic survives the container having no inbound network
route, but dies if the process dies. There is no queue on our side — GreenAPI
holds undelivered notifications until we poll them.

---

## 2. Configuration

Declared in `app/config.py`:

| Variable | Default | Notes |
|---|---|---|
| `GREEN_API_INSTANCE_ID` | — (required) | instance id from console.green-api.com |
| `GREEN_API_TOKEN` | — (required) | instance API token |
| `GREEN_API_HOST` | `https://api.green-api.com` | control/messaging host |
| `GREEN_API_MEDIA_HOST` | `https://media.green-api.com` | media upload host |
| `ALLOWED_TEST_PHONES` | empty | CSV digit-only allow-list; empty = allow all |

Required instance settings (the library also sets these on start):

```
incomingWebhook:          yes
outgoingMessageWebhook:   no
outgoingAPIMessageWebhook: no
```

`outgoing*Webhook: yes` makes the bot receive its own sent messages as
notifications. Leave them off.

---

## 3. Client construction

Three independent clients exist at runtime. They do not share state.

```python
# 1. Inbound bot -- app/main.py::_build_bot
bot = GreenAPIBot(
    settings.green_api_instance_id,
    settings.green_api_token,
    delete_notifications_at_startup=False,   # see below
)
register_handlers(bot)
bot.run_forever()

# 2. Per-message outbound -- app/whatsapp/handler.py
#    reuses the API object the library hands to the handler
sender = ChatSender(api=notification.api, chat_id=chat_id)

# 3. Background outbound -- opener.py / scheduler.py
api = GreenAPI(settings.green_api_instance_id, settings.green_api_token)
api.sending.sendMessage(f"{phone}@c.us", text)
```

**`delete_notifications_at_startup=False` is load-bearing.** The default
(`True`) drains the notification queue on boot, so any message that arrived
while the container was restarting is discarded. Do not change it.

Background threads must build their own `GreenAPI` client. `notification.api`
only exists inside an inbound handler.

---

## 4. Identifiers

| Form | Example | Where |
|---|---|---|
| chatId (private) | `972501234567@c.us` | GreenAPI wire format |
| chatId (group) | `...@g.us` | ignored by the handler |
| phone (our DB) | `972501234567` | `leads.phone`, digits only, no `+` |

Conversions:

```python
_phone_from_chat_id("972501234567@c.us")  # -> "972501234567"   handler.py
_chat_id("972501234567")                  # -> "972501234567@c.us"  opener.py
```

Inbound chatIds are already E.164-without-plus, so no normalization is needed
on that path. Phones arriving from the CRM webhook are user-typed and go
through `_normalize_phone` in `app/webhook/server.py` (handles `00972…`,
`+972-058…`, local `05X…`). LeadMe's own format conversion is separate — see
`docs/leadme_integration.md` §4.

---

## 5. Inbound

### 5.1 Notification envelope

`notification.event` is the raw GreenAPI JSON:

```json
{
  "typeWebhook": "incomingMessageReceived",
  "senderData":  { "chatId": "972501234567@c.us", "senderName": "..." },
  "messageData": { "typeMessage": "textMessage", "textMessageData": { ... } }
}
```

### 5.2 Text extraction

`_extract_text` supports these `typeMessage` values. Anything else returns
`None` and the notification is dropped:

| `typeMessage` | Text read from |
|---|---|
| `textMessage` | `textMessageData.textMessage` |
| `extendedTextMessage` | `extendedTextMessageData.text` |
| `imageMessage` / `videoMessage` / `documentMessage` / `audioMessage` | `fileMessageData.caption` (or `""`) |
| `buttonsResponseMessage` | `buttonsResponseMessage.selectedButtonText` |
| `listResponseMessage` | `listResponseMessage.title` |

Media itself is never downloaded. A media message with no caption yields `""`,
which is falsy and therefore skipped.

### 5.3 Filter chain

In order, in `register_handlers._on_message`. Each step returns early:

1. no `chatId` → drop
2. `chatId` ends with `@g.us` → drop (group chats are not handled)
3. phone does not start with `972` → drop
4. `ALLOWED_TEST_PHONES` non-empty and phone not in it → drop
5. no extractable text → drop
6. `lead.bot_muted` → **persist the inbound message, then stop.** No agent
   invocation, no reply. This is the human-takeover path from the admin UI.

Only after all six does the agent run.

### 5.4 Execution and timeout

```python
sender.send_typing()                      # fire before the slow part

with ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(handle_message, phone=..., text=...,
                         sender_name=..., send_video_fn=sender.send_video)
    reply = future.result(timeout=_MESSAGE_TIMEOUT)   # 90 seconds
```

The agent call is wrapped in a one-shot executor purely to get a hard timeout —
`handle_message` has no internal deadline and an OpenAI or Chroma stall would
otherwise block the polling loop for that chat indefinitely.

On `TimeoutError` or any exception: the error string and an ISO timestamp are
written to `lead_metadata` (`last_error`, `last_error_at`) by `_save_lead_error`
so it surfaces in the admin UI, and **no reply is sent**. The lead sees silence,
not an error message.

`send_video_fn=sender.send_video` is how the agent's `send_video` tool reaches
WhatsApp — the tool never builds a client itself.

---

## 6. Outbound

### 6.1 `ChatSender`

`app/whatsapp/sender.py`. A dataclass bound to one `(api, chat_id)` pair.

| Method | GreenAPI call | Notes |
|---|---|---|
| `send_text(text, humanize=True)` | `sending.sendMessage` | no-op on empty text; raises on failure |
| `send_video(video, caption=None)` | `sending.sendFileByUrl` or `sendMessage` | dispatches on `video.kind` |
| `send_typing()` | `sending.showMessagePreview` | best-effort, swallows all errors |

### 6.2 Typing simulation

`send_text(humanize=True)` re-fires the typing indicator, then blocks:

```
delay = clamp(len(text) / 35.0, min=2.0s, max=6.0s)
```

This is a synchronous `time.sleep` on the handler thread. It is deliberate, not
an accident — instant replies read as automated. Budget for it when reasoning
about the 90 s timeout: the delay happens *after* `handle_message` returns, so
it does not count against that timeout.

`showMessagePreview` is not available on every GreenAPI plan; `send_typing`
catches and ignores the failure rather than breaking the send.

### 6.3 Media

`Video.kind` selects the delivery method:

| `kind` | Call | Behaviour |
|---|---|---|
| `"file"` | `sendFileByUrl(chat_id, url, f"{id}.mp4", caption)` | GreenAPI fetches the URL server-side and delivers a native video |
| `"link"` | `sendMessage(chat_id, f"{caption}\n{url}")` | plain text; WhatsApp renders its own link preview |

`sendFileByUrl` requires a **publicly reachable** URL — GreenAPI's servers do
the fetch, not us. A signed, expiring, or LAN-only URL fails there, not here.
Use `kind: "link"` for anything GreenAPI cannot stream (YouTube, Vimeo).

Catalog lives in `data/videos.json`, loaded once via `@lru_cache` — edits need
a container restart.

### 6.4 Text constraints

WhatsApp does not render Markdown. `[label](url)` reaches the lead as literal
brackets. Enforced in three places:

1. the system prompt forbids it,
2. `_strip_markdown_links` in `app/agent/graph.py` rewrites it out of agent replies,
3. `kind="link"` media is sent as bare text.

**Any new outbound copy must be plain text** — nudge templates, openers, and
anything sent from the admin UI bypass the agent and therefore bypass (2).

---

## 7. Outbound call sites outside the handler

| Source | Trigger | Client | Recorded as |
|---|---|---|---|
| `opener.py::handle_new_lead` | CRM webhook, first contact | own `GreenAPI` | `MessageRole.assistant` + `opener_sent_at` |
| `graph.py::handle_message` (session reset) | lead idle ≥ 7 days | `opener._greenapi_client()` | `MessageRole.assistant` + `opener_sent_at` |
| `scheduler.py` nudges | APScheduler tick | `_greenapi_client()` | `MessageRole.assistant` |

All three write the sent text to the `messages` table themselves. The handler
path is the only one where persistence happens inside `handle_message`.

Send failures on these paths are logged and swallowed — a failed opener must
not lose the lead row that was just created.

---

## 8. Failure modes

| Symptom | Cause | Where to look |
|---|---|---|
| Bot silent for every lead | instance logged out / QR expired | `docker compose logs bot` at startup; GreenAPI console |
| Startup log `GreenAPI bot failed to start` | bad credentials or instance unreachable | `main.py` blocks forever instead of exiting, so `/health` and `/admin` stay up — check them to confirm the process is alive |
| Messages lost across a redeploy | `delete_notifications_at_startup` flipped to `True` | `main.py::_build_bot` |
| Bot replies to itself in a loop | `outgoingMessageWebhook` enabled on the instance | GreenAPI instance settings |
| Specific lead gets no reply | `bot_muted`, non-`972` phone, or `ALLOWED_TEST_PHONES` set | filter chain §5.3; admin UI shows mute state |
| Reply missing, `last_error` set on the lead | agent raised or exceeded 90 s | `lead_metadata.last_error` in the admin UI |
| Video never arrives, no error | URL not publicly reachable | GreenAPI fetches it server-side; test the URL from outside your network |
| Lead sees `[text](url)` | copy bypassed the agent post-processing | §6.4 |

Useful log prefixes: `[mute]`, `[CTWA]`, `[opener]`, `[session-reset]`,
`Handle message from`, `Failed to send text to`.

---

## 9. Instance setup

1. Create an instance at [console.green-api.com](https://console.green-api.com/).
2. Scan the QR from instance settings with the phone that acts as the bot.
3. Copy `idInstance` / `apiTokenInstance` into `GREEN_API_INSTANCE_ID` / `GREEN_API_TOKEN`.
4. Set the three webhook flags per §2.

The bot's WhatsApp account is a real phone number belonging to the customer.
Sending test traffic through it reaches real people — set
`ALLOWED_TEST_PHONES` before experimenting against production credentials.
