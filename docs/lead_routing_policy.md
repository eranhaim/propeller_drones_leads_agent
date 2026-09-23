# Propeller Drones — Lead Taxonomy and Human Handoff Policy

This is the operator-facing source of truth for how the bot classifies,
routes, and escalates leads. The implementation is intentionally
conservative: the bot can collect facts and update LeadMe, but it must not
promise a human callback until a valid call window has been captured.

## Deterministic fields

| Field | Allowed values | Owner | Handling rule |
|---|---|---|---|
| `intent` | `course`, `shop`, `service`, `hobby`, `job`, `unknown` | Agent, persisted in `lead_metadata` | Capture as soon as the message makes it clear. Do not re-ask once known. |
| `familiarity` | `unknown`, `beginner`, `aware`, `experienced` | Agent, persisted on `Lead` | Used only to adapt educational content; it never selects a course for the lead. |
| `industry` | `security`, `solar`, `agriculture`, `mapping`, `infrastructure`, `cinema`, `delivery`, `washing`, `other`, or a Hebrew free-text value | Agent, persisted in `lead_metadata` | Industry is never a call window. |
| `preferred_call_slot` | `9-12`, `12-15`, `15-18`, `any` | Code validates before persistence | Reject all free-form times, cities, names, and industries. |
| `funnel_stage` | `new`, `engaged`, `warm`, `ready_for_call`, `handed_off` | Agent/code | `handed_off` only after `schedule_call` succeeds locally. |
| LeadMe level | `1` booked, `2` replied, `3` opener/no reply | CRM integration | Lower number is a stronger engagement level and must never be downgraded. |

## Routing rules

1. **Course** — educate from the knowledge base, capture familiarity and
   industry, then offer a human advisor only after meaningful discovery or an
   explicit request.
2. **Shop** — query the live WooCommerce source for product questions. Never
   invent product specifications or stock. Route a purchase-ready lead to a
   sales advisor after a legal call window is captured.
3. **Commercial service** — capture the service/industry requirement, retrieve
   only relevant service knowledge, and hand off after a legal call window.
4. **Hobby** — give the recreational-license guidance and store link. A human
   call is optional, never forced.
5. **Job** — route to `hr@propeller-drones.com`, set `intent=job` and
   `funnel_stage=handed_off`; do not create a sales callback.

## Price and high-intent escalation

- A price question moves a course lead to **warm**. The bot may state only
  the approved broad range (`1,200–15,000 ₪`); exact course pricing belongs
  to a human advisor.
- An explicit request to speak with a person, a request for an exact quote,
  a service quote, or a purchase-ready request is **high intent**. Ask for
  one of the approved call windows and then invoke `schedule_call`.
- Do not fabricate `any` or treat “tomorrow morning”, “13:00”, a city, or an
  industry as consent to a callback. Until an approved window is explicit,
  the lead remains in conversation and no callback is promised.
- Once a valid window is captured, the bot marks the lead `handed_off`,
  records the LeadMe Level 1 update, and adds the `חלון · <slot>` tag. If
  LeadMe is temporarily unavailable, the durable queue retries the write;
  the local handoff record remains intact.

## Human takeover and recovery

- An operator can mute a lead in `/admin/leads/<id>` before replying
  manually. New inbound messages are retained but receive no bot reply.
- LeadMe failures never block a WhatsApp reply. Pending CRM operations appear
  in `/admin/leadme-status`, are retried every three minutes, and are shown
  as abandoned if they exceed the configured lifetime/attempt limits.
- A stale GreenAPI receive loop terminates the worker so Docker restarts it.
  Health is `503` while the poller is stale, allowing the Compose healthcheck
  to recover a wedged process.

## Knowledge-source maintenance

- Website and store pages are refreshed by
  `python -m scripts.ingest_knowledge --reset` inside the bot image.
- Approved operational facts that are not on the website belong in
  `data/knowledge/` as UTF-8 Markdown/text. Re-ingest after each edit.
- Do not add price sheets, credentials, customer exports, or unverified
  marketing claims to the knowledge directory.

## Acceptance checks after a deployment

1. `/health` returns HTTP 200 with `"polling":"healthy"`.
2. `/admin/leadme-status` shows a healthy LeadMe session or a clear,
   operator-actionable failure state.
3. The simulator at `/admin/simulator` can run a Hebrew conversation without
   touching WhatsApp or LeadMe.
4. Send one approved test WhatsApp message and verify both inbound and
   outbound records in `/admin`.
5. For a genuine LeadMe test lead, verify that a Level 1 handoff and its call
   window tag arrive, or that the action is visible in the retry queue.
