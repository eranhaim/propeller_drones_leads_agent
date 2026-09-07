# LeadMe integration — client-side technical reference

How **our code** talks to LeadMe: module surface, transport selection, call
contracts, the retry queue state machine, guards, and failure modes.

For LeadMe's own HTTP surface — endpoint URLs, cookie capture, DataTables
search parameters, status-ID discovery, campaign IDs — see the protocol field
manual `docs/leadme_integration.md`. This document does not repeat it.

---

## 1. Module map

| Module | Responsibility |
|---|---|
| `app/crm/client.py` | Public facade. The only import surface for callers outside `app/crm/`. |
| `app/crm/leadme_client.py` | Level state machine, transport selection, admin-path push, session health. |
| `app/crm/leadme_v3.py` | Official v3 REST client (API key). |
| `app/crm/leadme_delete.py` | Cookie loading, phone-format variants, row search, delete. |
| `app/crm/leadme_queue.py` | Durable retry queue for pushes that could not resolve a phone. |
| `app/crm/leadme_login.py` | Cookie auto-refresh via 2Captcha. |

**Rule:** code outside `app/crm/` imports from `client.py` only. The agent
tools, the opener, and the handler must not reach into `leadme_client` or
`leadme_v3` directly for writes. (`leadme_v3.check_auto_level1` is a read and
is the one deliberate exception, called from `graph.py`.)

---

## 2. Public facade — `app/crm/client.py`

```python
mark_ready_for_call(lead, note=None, slot=None) -> bool   # level 1
mark_engaged_no_book(lead, note=None)          -> bool    # level 2
mark_no_reply(lead, note=None)                 -> bool    # level 3
cancel_ready_for_call(lead, reason=None)       -> bool
```

All four take a live `Lead` ORM instance, not a phone string — they read and
write `lead.lead_metadata`, so the caller must hold an open session.

### Return-value semantics

`True` from the three `mark_*` functions means **"accepted"**, not
"landed in LeadMe". It covers: pushed successfully, deduplicated as a no-op,
refused by the downgrade guard, suppressed by test mode, *or* enqueued for
background retry. Callers must not retry on `True`, and must not treat it as
proof the CRM changed.

`False` means the request was rejected outright (invalid level, no cookies on
the cancel path). Only `cancel_ready_for_call` returns `False` for a genuine
push failure.

The real outcome is in the logs (`[LeadMe]`, `[leadme_v3]`, `[queue]`) and in
`lead_metadata`.

### `slot` on `mark_ready_for_call` is ignored

It is accepted for backwards compatibility and logged if passed. The effective
slot is always read from `lead_metadata["preferred_call_slot"]`. Persist it
first:

```python
repository.update_lead_metadata(session, lead, preferred_call_slot="9-12")
mark_ready_for_call(lead, note="...")
```

Silent parameter shadowing here was a real bug — see
`docs/handoff_2026_07_26.md` §5.3.

---

## 3. Two transports

| | v3 REST | Internal admin |
|---|---|---|
| Auth | `LeadMeCMS-API-Key` header | `PHPSESSID` + `csrf_cookie_name` cookies |
| Base | `https://api.leadmecms.co.il/v3` | `LEADME_ADMIN_BASE` (`https://www.leadmecms.co.il`) |
| Client | `httpx.Client(timeout=8.0)`, per call | `leadme_delete._build_client()` |
| Credential lifetime | static key | CSRF cookie hard-expires every 24 h |
| Module | `leadme_v3.py` | `leadme_client.py` + `leadme_delete.py` |

**Selection rule** (`push_engagement_level`): if `LEADME_API_KEY` is set, use
v3 and fall back to the queue on failure. Otherwise use the admin path.
There is no per-call override.

v3 is the preferred path — no cookie rotation, no HTML-login-page failure mode.
The admin path remains because v3 has no delete and no status-reset endpoint,
so `push_lead_cancellation` and `leadme_delete` are cookie-only.

### v3 endpoints used

| Function | Method | Path | Body |
|---|---|---|---|
| `get_lead_id(phone)` | GET | `/getLeadStatus` | `{"phone": "05XXXXXXXX"}` |
| `update_lead_status(lead_id, status_id)` | POST | `/updateLeadStatus` | `{"leadId": …, "status": …}` |
| `add_lead_tag(lead_id, tag)` | POST | `/addLeadTag` | `{"leadId": …, "tag": "…"}` |
| `get_lead_tags(lead_id)` | GET | `/getLeadTags` | `{"leadId": …}` |

Two GETs carry a JSON body, which `httpx` will not do through the normal
`client.get()` API — hence `build_request` + `send` in `leadme_v3.py`. Keep that
shape.

Every function returns `None` / `False` / `[]` on any failure and logs; none
raise. Success is `data["result"]` being truthy — **not** the HTTP status. A
`200` with `result: false` is a failure.

### Phone formats

`leadme_v3._normalize_phone` converts our `972XXXXXXXXX` to LeadMe's local
`0XXXXXXXXX`. The admin path instead tries several substring variants — see
the field manual §4.

---

## 4. Engagement level state machine

Three levels, stored as `lead_metadata["leadme_last_level"]`. **Numerically
lower means further along.**

| Level | v3 status id | Meaning in the CRM |
|---|---|---|
| 1 | `7326` | booked |
| 2 | `7327` | replied |
| 3 | `7328` | no reply |

`push_engagement_level(lead, level, note=None, slot=None)` enforces
upgrade-only transitions:

| From → To | Result |
|---|---|
| `None` → 1/2/3 | push |
| 3 → 2, 3 → 1, 2 → 1 | push |
| same → same | no-op, returns `True` |
| 1 → 2, 1 → 3 | **refused** |
| 2 → 3 | **refused** |
| level not in {1,2,3} | rejected, returns `False` |

`leadme_last_level` is written **only after the push actually lands.** On a
failed push the level is enqueued and the key is left unset — writing it
early would make the downgrade guard block a level that never reached LeadMe.
The queue drain writes it when the retry succeeds.

`push_lead_cancellation` pops `leadme_last_level` so a re-book is not blocked.

### Where levels are pushed from

| Call site | Level | Condition |
|---|---|---|
| `graph.handle_message` | 3 | first inbound message of a session |
| `graph.handle_message` | 1 | first message *and* `check_auto_level1(phone)` is true |
| `graph.handle_message` | 2 | second inbound message of a session |
| `tools.schedule_call` | 1 | booking confirmed |
| `graph._enforce_booking_promise` | 1 | reply promised a call but the tool never fired |
| `opener.handle_new_lead` | 1 | lead arrived via the CRM webhook |

Levels 2 and 3 are decided mechanically from the user-message count, not by the
model. Only level 1 has a model-driven path.

`check_auto_level1` (v3 read) returns true when the lead already carries one of
`AUTO_LEVEL1_TAGS`. Requires `LEADME_API_KEY`; returns `False` without it.

### Slot tag

For level 1 only, a `חלון · <slot>` tag is attached alongside the status.
Levels 2 and 3 carry no tag. CTWA attribution adds a separate `מקור: <campaign>`
tag from `handler._push_ctwa_tag`.

---

## 5. The retry queue

`app/crm/leadme_queue.py`. Exists because CTWA leads reach us before LeadMe's
supplier sync knows their phone — the row is not findable yet, so a push cannot
resolve a `leadId`.

### State schema

Stored on `lead.lead_metadata`:

```json
{
  "leadme_push_pending": [
    {"kind": "engagement", "level": 1, "slot": "9-12", "note": "..."},
    {"kind": "ctwa_tag", "campaign": "..."}
  ],
  "leadme_push_next_attempt_at": "2026-07-26T10:25:00+00:00",
  "leadme_push_attempts": 3,
  "leadme_push_queued_at": "2026-07-26T10:12:00+00:00",
  "leadme_push_abandoned": true,
  "leadme_push_abandoned_at": "2026-07-26T11:12:00+00:00"
}
```

Because it lives in the lead row, the queue survives container restarts. There
is no separate table and no external broker.

### Merge rules

Enqueueing is idempotent per kind:

* two `engagement` items collapse to one — **lower level wins**; the newer slot
  is kept; notes are concatenated.
* two `ctwa_tag` items with the same campaign collapse to one.
* different kinds coexist and are replayed together against one row lookup.

### Tunables

| Constant | Value | Meaning |
|---|---|---|
| `RETRY_INTERVAL_MINUTES` | 3 | spacing between attempts |
| `MAX_LIFETIME_MINUTES` | 180 | give up after 3 h |
| `MAX_ATTEMPTS` | 60 | secondary cap |
| `_INREQUEST_RETRIES` | 2 | fast lookups before enqueueing (~10 s) |
| `_INREQUEST_WAIT_SEC` | 5 | first wait; the second doubles it |

Drained by the `leadme_queue_drain` APScheduler job every
`LEADME_QUEUE_INTERVAL_MINUTES` (default 3). **This job runs even when
`FOLLOWUP_ENABLED=false`.**

On expiry the item is dropped and `leadme_push_abandoned` is set, which the
admin UI surfaces at `/admin/leadme-status`. `get_queue_snapshot()` is the
read API for that page.

### The anti-pattern this replaced

Do not add an in-request retry loop for a LeadMe lookup. The previous
"4 attempts over 30 s, then give up forever" dropped 40 of 44 pushes in a
48-hour window. Two fast attempts, then enqueue.

---

## 6. Guards

Checked before any write. Each returns early and logs:

| Guard | Condition | Behaviour |
|---|---|---|
| Test mode | `LEADME_TEST_MODE=1` | all writes become logged no-ops; reads still work |
| Test phone | `_is_test_phone(phone)` (`999…` prefix) | refuses the write |
| Kill switch | `LEADME_INSERT_MODE=never` | no writes at all |
| Banned campaign | row sits in campaign `12277` (`הוסרו מ-Whatsapp`) | refuses status and tag |
| No phone | empty `lead.phone` | returns early |

`LEADME_INSERT_MODE` values:

| Value | Effect |
|---|---|
| `update-only` (default) | never call `/supplier/insert`; update existing rows only |
| `insert-then-update` | legacy; can create duplicates |
| `never` | no writes |

**The bot never creates a LeadMe row.** The public supplier endpoints behave as
upserts and drop unmatched leads into campaign `12277`. `_campaign_leak_canary`
in the scheduler polls that campaign's count and logs an `ERROR` if it grows.

---

## 7. Session health and cookie refresh

`check_leadme_session_health(force=False)` probes the admin session and returns
a dict with a `status` field (`healthy`, `no_cookies`, …). Cached for 30 s
(`_HEALTH_CACHE_TTL_SECONDS`) so the admin page can poll it cheaply; pass
`force=True` to bypass.

Scheduler jobs:

| Job | Interval | Registered when |
|---|---|---|
| `leadme_queue_drain` | `LEADME_QUEUE_INTERVAL_MINUTES` (3) | always |
| `leadme_session_health` | 30 min | always |
| `leadme_leak_canary` | `max(15, FOLLOWUP_INTERVAL_MINUTES)` | `FOLLOWUP_ENABLED=true` |
| `leadme_proactive_refresh` | `LEADME_AUTO_REFRESH_INTERVAL_HOURS` (12) | `LEADME_AUTO_REFRESH_ENABLED=true` |

Cookie refresh (`leadme_login.py`) is pure `httpx` plus a 2Captcha solve; it
does not drive a browser. First run is deferred by one full interval so a
container restart does not burn a solve.

Cookies live at `LEADME_COOKIES_PATH` (`data/leadme_cookies.json`, gitignored,
bind-mounted). They can also be pasted through `/admin/leadme-cookies`.

---

## 8. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LEADME_API_KEY` | empty | v3 REST key; when set, selects the v3 transport |
| `LEADME_ADMIN_BASE` | `https://www.leadmecms.co.il` | admin host |
| `LEADME_COOKIES_PATH` | `data/leadme_cookies.json` | session cookies; empty disables admin writes |
| `LEADME_INSERT_MODE` | `update-only` | write mode, §6 |
| `LEADME_TEST_MODE` | `false` | suppress all writes |
| `LEADME_QUEUE_INTERVAL_MINUTES` | 3 | queue drain cadence |
| `LEADME_AUTO_REFRESH_ENABLED` | `false` | cookie auto-refresh master switch |
| `LEADME_LOGIN_EMAIL` / `_PASSWORD` | empty | dedicated bot account |
| `LEADME_CAPTCHA_API_KEY` | empty | 2Captcha key |
| `LEADME_AUTO_REFRESH_INTERVAL_HOURS` | 12 | must stay under the 24 h CSRF expiry |
| `LEADME_STATUS_LEVEL_1/2/3` | empty | admin-path status ids; empty pushes only the tag |

`LEADME_INSERT_URL` / `LEADME_UPDATE_URL` / `LEADME_STATUS_ID` /
`LEADME_SOURCE_LABEL` configure the legacy supplier API and are unused under
`update-only`.

---

## 9. Failure modes

| Symptom | Cause | Where to look |
|---|---|---|
| Push logged as success, CRM unchanged | HTTP 200 with `result: false` | check `data["result"]`, never the status code |
| Everything returns HTML | cookies expired | `check_leadme_session_health()`, `/admin/leadme-status` |
| Leads stuck at the initial status | CTWA race; items sitting in the queue | `get_queue_snapshot()`, `[queue]` logs |
| Queue never drains | drain job failed to register | startup log `leadme_queue_drain job registered` |
| Level never upgrades | `leadme_last_level` written before the push landed | §4; clear the key to unblock |
| Tag POST succeeds, tag absent | pushed against `lc_id` instead of the internal `leadId` | `_resolve_tag_lead_id`; field manual §7.3 |
| Duplicate rows in the trash campaign | a supplier insert ran | `LEADME_INSERT_MODE`; canary log |
| Test leads in the customer's CRM | `LEADME_TEST_MODE` not set before import | `scripts/eval_rejects.py` sets it before any `app.*` import |

Two distinct id types are in play — the DataTables row id (`lc_id`) and the
internal `leadId`. Status changes take `lc_id`; tags take `leadId`. Passing the
wrong one succeeds silently. See field manual §7.3 and handoff §5.7.
