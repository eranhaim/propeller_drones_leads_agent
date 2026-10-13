# LeadMe CMS integration – field manual

This document is the operating manual for anyone (human or AI agent) that
needs to make the Propeller Drones bot – or any sibling tool – talk to
[LeadMe CMS](https://www.leadmecms.co.il) reliably. It captures every
endpoint we reverse-engineered, every trap we fell into, and the exact
recipes we settled on. If you find something new, please add a section
here.

> **Scope.** This file documents *LeadMe's own HTTP surface*. For our
> client code — module map, transport selection, the level state machine,
> the retry queue, and guards — see
> [`reference/leadme_client_technical.md`](reference/leadme_client_technical.md).
> A shorter contract summary (what is sent/received, keys needed) is in
> [`integrations/leadme.md`](integrations/leadme.md).

> **Ground truth locations in this repo**
>
> - `app/crm/leadme_client.py` – the *only* place that pushes engagement
>   status / tag changes to LeadMe. Admin-only path.
> - `app/crm/leadme_delete.py` – shared cookie loading, phone-format
>   variants, the DataTables search helper, and the delete flow.
> - `app/followup/scheduler.py` – contains the `_campaign_leak_canary`
>   tripwire that alerts if leads ever end up in the trash campaign
>   again.
> - `scripts/_debug_*.py` – short single-purpose reconnaissance scripts
>   you can copy/modify. Index at the bottom.

---

## 1. TL;DR – the rules that keep us out of trouble

1. **Never call `https://api.leadmecms.co.il/supplier/insert/...`** or
   `.../supplier/update/...`. On this account both endpoints act as
   *upserts*: if the phone can't be resolved inside a supplier-linked
   campaign, LeadMe silently creates a duplicate row in the supplier's
   default campaign (`id=12277` = `הוסרו מ-Whatsapp`, i.e. the "trash"
   bucket). The customer will notice, we will apologize.
2. **Use v3 for supported bot writes.** Status and tag changes use the
   official API-key endpoints and are read-after-write verified. Keep the
   internal admin session only for unsupported actions such as delete and
   status reset (see §3).
3. **We never create LeadMe rows from the bot.** The only way a new
   lead should appear in LeadMe is through the customer's own website
   form → LeadMe supplier flow. If `push_lead` can't find a matching
   row, it logs a warning and returns.
4. **Phones must be normalized before searching.** LeadMe stores Israeli
   numbers in local format (`053-346-0489`). We store E.164ish
   (`972533460489`). Search matches substrings, so the *last 9 digits*
   of the E.164 number always work (`533460489`). See §4.
5. **Statuses require numeric IDs**, not names. Sending
   `status="חדש - רמה 1"` silently succeeds and does nothing. Sending
   `status="7326"` actually moves the status pill. See §6.
6. **Cookies expire.** Both `PHPSESSID` and `csrf_cookie_name` need to
   be re-exported when the admin endpoints start returning HTML login
   pages or `result:false, msg:"..."`. See §3.4.
7. **There is a background canary** (`_campaign_leak_canary`) that polls
   campaign 12277's lead count every 30 minutes. If it grows, an
   `ERROR` line lands in Docker logs. Do not disable it without a very
   good reason.
8. **Only `classify_engagement` decides a level**, and levels move in both
   directions. See §6.5.
9. **v3 allows 60 requests per minute, and a throttled read lies to you.**
   Past the limit LeadMe answers `429 {"result": false, "message": "Rate
   limit exceeded", "retryAfter": N}`, and `getLeadStatus` reports that
   *identically* to a lead it has never heard of — `result: false`, no lead.
   So an unpaced scan does not fail loudly, it quietly reports that the
   customer's leads are missing from their own CRM. **It has already made us
   tell the customer something false.** `app/crm/leadme_v3.py` gates every
   call process-wide; never bypass it, and never conclude anything about the
   customer's data from a scan that did not respect it. See §2.2.

---

## 2. API surfaces – what to use when

LeadMe exposes three distinct HTTP surfaces. Understanding which is which is
the single most important thing.

### 2.1. Public "supplier" API — DO NOT USE for bot writes

- Base: `https://api.leadmecms.co.il`
- Endpoints:
  - `POST /supplier/insert/{link_id}/{slug}`
  - `POST /supplier/update/p/{slug}`
- Auth: **none** – anyone with the slug can post.
- Response: always `200 OK` with an empty body on the update endpoint.
  You cannot tell success from silent failure from the HTTP layer.
- Intended for lead-source integrations (Facebook, TikTok, form
  builders). Meant to CREATE new leads.
- Trap on our account: `update` also creates when it can't find a
  match. That's how the "trash campaign" bug shipped.
- **When it's OK to use:** initial migrations, one-time bulk imports
  from an external source that legitimately owns the lead. Never from
  the bot's live push path.

### 2.2. Official v3 API — use for verified status and tag writes

- Base: `https://api.leadmecms.co.il/v3`
- Auth: `LeadMeCMS-API-Key` header. Store the key only in a protected
  production `.env`; never print it, commit it, or put it in browser code.
- Supported bot endpoints: `GET /getStatuses`, `POST /getLeadStatus`,
  `POST /updateLeadStatus`, `POST /addLeadTag`, `GET /getLeadTags`.
- Required confirmation: an update is successful only after a following
  `getLeadStatus` returns the requested status. A tag is successful only after
  `getLeadTags` returns the exact text. `{"result": true}` is not sufficient.
- `404` for an unknown phone or lead is expected during the CTWA sync race:
  keep the action queued. Do not create a LeadMe row.
- The vendor page currently says `POST /getLeadTags`; live verification on
  2026-09-29 returned HTTP 405 to POST and succeeded with GET plus a JSON
  body, so the client uses that working method until LeadMe corrects it.
- There is **no per-lead endpoint for the source, campaign or Facebook id**.
  Probed 2026-10-13: `getLead`, `getLeadById`, `getLeadData`,
  `getLeadDetails`, `getLeadInfo`, `getLeadFields`, `getLeadSource`,
  `getLeadCampaign`, `getLeads`, `getSources`, `getTags` and
  `getCustomFields` all return a CodeIgniter 404 page. `getCampaigns` works
  and is undocumented, but returns the account's global campaign list, not
  anything per lead. Phone → `getLeadStatus` → `leadId` → `getLeadTags` is
  the whole of what can be read about one lead.

#### Rate limit — 60 requests per minute, per API key

Measured 2026-10-13: throttled after exactly 60 calls in 5.2 seconds, with
`429 {"result": false, "message": "Rate limit exceeded", "retryAfter": N}`.

The danger is not the limit, it is that `getLeadStatus` cannot distinguish a
throttled read from a lead LeadMe has never heard of — both are `result:
false` with no lead. An unpaced scan of 601 leads reported 541 of them
missing from the CRM; re-checking the same phones at one call per second
found 60 of 60 present. See the retraction in §6.5.

`app/crm/leadme_v3.py` therefore holds one process-wide gate (the limit is
per key, so one gate covers every call site). A call that cannot get a slot
within two seconds fails fast rather than making a lead wait, and the retry
queue picks the work up later. `get_lead_tags_by_phone` returns a three-way
outcome — `found`, `absent`, `unavailable` — so callers can tell "no such
lead" from "LeadMe would not answer". Anything that treats `unavailable` as
`absent` is reintroducing the bug.

### 2.3. Internal admin API — only for unsupported actions

- Base: `https://www.leadmecms.co.il` (note: `www.`, not `api.`).
- Auth: cookies (`PHPSESSID`, `csrf_cookie_name`) from a logged-in
  admin session, plus `csrf_lmcms` echoed in every write POST.
- Rate: intended for a human clicking in the UI. Keep concurrency low
  (1–2 in-flight requests) and add small pauses between bulk operations
  to avoid tripping any rate limiting.
- Response: mostly `application/json` with `{"result": true/false,
  "msg": "..."}`. When cookies expire, LeadMe redirects to `/login` and
  the response is HTML – treat that as "session expired, refresh
  cookies."

---

## 3. Authentication – capturing and using cookies

### 3.1. What we actually need

Two cookies for the LeadMe admin domain:

| Cookie             | Purpose                                             |
|--------------------|-----------------------------------------------------|
| `PHPSESSID`        | Session ID – the whole point of "logged in".        |
| `csrf_cookie_name` | CSRF token. Its value must be echoed back as the    |
|                    | `csrf_lmcms` form field in every write POST.        |

Everything else in the browser cookie jar (Google Analytics, HotJar,
etc.) is noise and should be filtered out.

### 3.2. Storage format

We keep them in a Chrome-DevTools-compatible JSON array at the path
configured by `LEADME_COOKIES_PATH` (default `data/leadme_cookies.json`
inside the container). Example minimal file:

```json
[
  {
    "name": "PHPSESSID",
    "value": "ab12cd34…",
    "domain": ".leadmecms.co.il",
    "path": "/"
  },
  {
    "name": "csrf_cookie_name",
    "value": "ffeedd00…",
    "domain": ".leadmecms.co.il",
    "path": "/"
  }
]
```

`app/crm/leadme_delete.py::_build_client()` loads this file and
returns a pre-configured `httpx.Client`. Reuse that helper in any new
tooling – don't roll your own.

### 3.3. Capturing cookies with a headed Playwright session

LeadMe protects login with reCAPTCHA v2, so **fully-headless login is
not viable**. The workflow is: pop a headed Chromium, let a human solve
the CAPTCHA once, then dump cookies. The recon scripts historically
lived under `leadme-recon/` (excluded from git via `.gitignore`) and
looked roughly like this:

```python
# leadme-recon/capture_cookies.py
import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # HEADED!
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://www.leadmecms.co.il/login")
        print("Solve the CAPTCHA and log in. Press Enter here when done.")
        input()
        cookies = await ctx.cookies()
        keep = [c for c in cookies if "leadmecms.co.il" in c["domain"]]
        with open("captures/cookies.json", "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(keep)} cookies.")
        await browser.close()

asyncio.run(main())
```

Then upload the file to the container:

```bash
scp -i key.pem captures/cookies.json \
    ubuntu@<host>:/home/ubuntu/leadme_cookies.json
ssh -i key.pem ubuntu@<host> \
    "cp /home/ubuntu/leadme_cookies.json \
        propeller_drones_leads_agent/data/leadme_cookies.json \
     && docker cp propeller_drones_leads_agent/data/leadme_cookies.json \
        propeller_bot:/app/data/leadme_cookies.json"
```

### 3.4. When to refresh

Refresh cookies when *any* of these happen:

- `get_current_status_text(...)` returns `None` for phones you know
  exist.
- Admin POSTs come back as HTML instead of JSON (that HTML is the
  login page).
- `result:false, msg:"לא נבחרו רשומות!"` or similar generic errors
  where you know your payload is correct.
- The bot logs `[LeadMe] no admin cookies configured` or
  `session cookie may be expired`.

The bot has a self-service refresh page at `/admin/leadme-cookies` that
lets an operator paste a fresh JSON array without SSHing anywhere.

---

## 4. Phone-format normalization

LeadMe internally stores Israeli phones in *local* format with dashes:
`053-346-0489`. Our DB stores E.164-ish digits: `972533460489`. The
`getDataForTable` search matches substrings, so:

| Format          | Works? |
|-----------------|--------|
| `972533460489`  | no – LeadMe never stores the `972` prefix internally |
| `0533460489`    | yes   |
| `533460489`     | yes – **preferred**, always the last 9 digits |
| `053-346-0489`  | yes, but ugly |

Use `app.crm.leadme_delete._phone_variants(phone)` – it yields the
formats in priority order. The `_fetch_row()` helper already tries them
all until one hits.

Non-Israeli phones (a handful of `+90…`, `+970…`, `+44…` in the
customer's data) are stored as-is, so passing the raw digits usually
finds them.

---

## 5. Endpoint reference – confirmed URLs

Everything below has been hit successfully at least once from
`app/crm/leadme_delete.py` or a `scripts/_debug_*.py` probe.

### 5.1. Search / list

| Purpose               | Endpoint                          | Method | Auth       |
|-----------------------|-----------------------------------|--------|------------|
| Search all leads      | `/app/ajax/getDataForTable`       | GET    | cookies    |
| Prime session for above | `/app/leads`                    | GET    | cookies    |
| Campaign detail page  | `/app/campaigns/manageCampaign/{campaignId}` | GET | cookies |
| Campaign leads table  | `/app/ajax2/loadMcfTable/{status}` | POST  | cookies + csrf |
| Campaign pie chart    | `/app/ajax4/getPieData`           | POST   | cookies + csrf |

The `getDataForTable` endpoint expects the full DataTables query string
– 8 empty `columns[i][...]` groups plus `search[value]`, `search[regex]`,
`order[0][column]`, `order[0][dir]`, `start`, `length`, and a cache-buster
`_`. See `_search_params()` for the canonical builder.

**Row layout returned by `getDataForTable`** (indexes into the outer
JSON array `data`):

| idx | Content                                                                    |
|-----|-----------------------------------------------------------------------------|
| 0   | Checkbox HTML: `<input name="selectedLeads[]" value="<lc_id>"/>`             |
| 1   | Plain numeric `lc_id` (**lead-campaign id**, not the internal `leadId`!)     |
| 2   | Lead name                                                                    |
| 3   | Phone (formatted `053-346-0489`)                                             |
| 4   | Campaign name (plain text, e.g. `מתעניינים אקדמיה`)                          |
| 5   | Status HTML: `<span class="label status" ...>חדש - רמה 1</span>`             |
| 6   | Created timestamp `dd/mm/YYYY HH:MM`                                         |
| 7   | Action buttons (view / edit) with `javascript:showLeadSummary(<lc_id>)` etc. |

> **CRITICAL**: what you get here is the **lead-campaign id** (typically
> in the `22xxxxxx` range). LeadMe internally has a SECOND numeric id –
> the **internal `leadId`** (typically `13xxxxxx`) – which is what
> `addLeadTag` requires. See §7.4 for how to resolve it.

### 5.2. Writes

| Purpose         | Endpoint                        | ID to send                | Payload (form-encoded)                                                                 |
|-----------------|---------------------------------|---------------------------|----------------------------------------------------------------------------------------|
| Change status   | `/app/leads/changeLeadsStatus`  | `lc_id` (from row cell 1) | `data[status]=<rel_id>`, `data[leadId]=<lc_id>[,<lc_id>...]`, `csrf_lmcms=<token>`      |
| Add tag         | `/app/ajax/addLeadTag`          | **internal `leadId`** (§7.4) | `text=<tag>`, `leadId=<internal_leadId>`, `csrf_lmcms=<token>`                       |
| Delete lead(s)  | `/app/ajax/deleteLeads`         | `lc_id`                   | `data[leadId][]=<lc_id>` (or `leadIds[]=<lc_id>` in some tenants), `csrf_lmcms=<token>` |

**Two-IDs trap.** LeadMe carries two distinct numeric identifiers per
lead and mixes them freely across endpoints:

| Name                    | Where you get it                                    | Range  | Used by                                              |
|-------------------------|-----------------------------------------------------|--------|------------------------------------------------------|
| `lc_id` (lead-campaign) | `getDataForTable` row cell 1, `viewLead/<lc_id>` URL | `22xxxxxx` | `changeLeadsStatus`, `deleteLeads`, `viewLead` URL |
| `leadId` (internal)     | Scrape `viewLead` page HTML (§7.4)                   | `13xxxxxx` | `addLeadTag`, `removeLeadTag`                     |

If you send `lc_id` to `addLeadTag` you'll get `{"result":true}` back
and **nothing will land** – the tag is silently accepted against a
phantom lead. Always resolve to the internal `leadId` first.

**Second trap**: `data[leadId]` for `changeLeadsStatus` is a
*comma-joined string*, not an array. The UI JS is:

```javascript
data.leadId = $(".CB:checked, .leadCB:checked")
                 .map(function() { return $(this).val(); })
                 .get()
                 .join();
```

So `22371797,22371798` is one lead ID field, not two.

---

## 6. Status IDs table

The status "pill" on each lead row is a numeric relationship id, not a
name. LeadMe silently ignores status *names* if you send them via any
write endpoint. To discover the mapping, open any campaign page,
Right-Click → View Source, find the `dialog_changeStatus` block and
extract every `<a class="... changeStatusPuBtn" rel="<id>">Label</a>`.
Current mapping for the Propeller account, all 26 statuses as read through v3
`getStatuses` on 2026-10-12. Sorted by id, which is the order the endpoint
returns and the easiest to diff against a fresh read:

| Numeric ID | Status label (Hebrew)              |
|-----------:|------------------------------------|
| 1          | חדש (default)                      |
| 4          | ללא מענה                           |
| 5          | ללא מענה 2                         |
| 7          | נקבעה פגישה                        |
| 10         | בוצעה עסקה                         |
| 14         | שיחה חוזרת                        |
| 250        | פולואפ                             |
| 609        | לקוח קיים                          |
| 1623       | אין מענה 3                         |
| 2392       | לא מתאים לנו                      |
| 5821       | לא נסגר - לא מספיק חשוב לו כרגע  |
| 5827       | פולואפ - ניתנה הצעה               |
| 5839       | מאגר - בדיקה                       |
| 5841       | לא רלוונטי - פנאי וספורט          |
| 6570       | ענו לא                             |
| 7155       | רלוונטי ליום פתוח                 |
| 7220       | לא נסגר - 2 טון                    |
| 7221       | לא נסגר - מגמה מקצועית            |
| 7222       | לא נסגר - 25 ק"ג                   |
| 7228       | ניוזלטר                            |
| 7229       | לא נסגר  מסלול ביזנס              |
| 7326       | חדש - רמה 1                        |
| 7327       | חדש - רמה 2                        |
| 7328       | חדש - רמה 3                        |
| 7403       | לא נסגר - FPV                      |
| 7409       | לא נסגר - רוצה פיקדון צבאי        |

Renamed since the 2026-09-29 read, so any script matching on a label rather
than an id needs checking: 4 and 5 (`אין מענה 1/2` → `ללא מענה`/`ללא מענה 2`),
250 (`פולואפ - מחכה למימון חיצוני` → `פולואפ`), 5821 (`לא נסגר – כללי`), 5841
(`לא נסגר - חנות` → `לא רלוונטי - פנאי וספורט`) and 6570
(`הסרה מרשימות תפוצה` → `ענו לא`). 7403 and 7409 are new.

`getStatuses` returns labels HTML-escaped — 7222 comes back as
`לא נסגר - 25 ק&quot;ג`. Unescape before comparing to a literal.

There is still no exact `לא רלוונטי` title. `LEADME_STATUS_NOT_RELEVANT` is
set to 2392 (`לא מתאים לנו`), which is what the sales rep already applies to
these leads by hand. Status IDs are per-account. Re-run v3 `getStatuses` after
any LeadMe status change.

The engagement-level IDs (7326/7327/7328) live in `.env` as
`LEADME_STATUS_LEVEL_1/2/3`. Everything else is used ad-hoc in
one-off scripts.

### 6.5. What the three levels mean

Redefined by the customer on 2026-10-12. The levels measure **how much the
lead already knows** — about the drone industry and about Propeller as a
company — not how many messages they sent. For a Level 1 lead the sales call
is only "why now", never "what is a drone" and never "who are you".

| Level | Status | Who is here |
|---|---|---|
| 1 | 7326 | **Where they came from**: organic arrivals, the website form, a landing page, a call to the number published on the website, a **returning** lead. **Or what they did**: they took in Propeller's content *and* booked a call for a specific window. |
| 2 | 7327 | **A booked call that misses part of that pair**: no content behind it, no specific window, or a stale booking. A booked call is the whole of what Level 2 means. |
| 3 | 7328 | Everyone else. The starting point for **every paid-advertising lead** (Meta, TikTok, CTWA), where a lead who never meaningfully replies stays, and — since 2026-10-13 — where a lead who held a real conversation but never booked anything stays too. |

Three further rules:

- **The engagement route to Level 1 needs both halves.** Neither is
  sufficient alone: leads book calls without knowing what FPV is, and leads
  watch a video without intending to buy. "Content" is any one of the
  webinar, a substantive reply after a video we sent, or a conversation of at
  least `LEAD_DEEP_ENGAGEMENT_MESSAGES` (default 12) substantive inbound
  messages.
- **Engagement decays; where a lead came from does not.**
  `LEAD_ENGAGEMENT_FRESH_DAYS` (default 14) bounds the booking, and a lead
  who falls out of it lands at Level 2, not Level 3 — they did engage, just
  not recently. Source-based Level 1 never decays.
- **A booking only counts with a real window.** `9-12`, `12-15` or `15-18`.
  `schedule_call` books with `any` rather than stalling when the lead never
  names a time, and the customer's rule says "תיאם שיחה בשעה מסוימת", so an
  `any` booking stays at Level 2. `booked_at` in `lead_metadata` is what the
  decay is measured from; leads booked before 2026-10-12 have no such
  timestamp and fall back to their last inbound message, which can only make
  an old booking look newer than it is.

- **A conversation without a booking is Level 3** (`conversation_no_booking`).
  Roy's wording puts "לא תיאם שיחה" among the Level 3 traits, and it is read
  here as the strict boolean it looks like. Measured against production, this
  moves roughly 307 of 571 leads out of Level 2 and leaves about three
  quarters of the base at the bottom tier. **That shape is intended, not a
  bug** — Eran's decision on 2026-10-13, taken with the number in hand. The
  reasoning: a Level 2 that meant "talked to the bot" described almost
  everyone and so told the sales team nothing, and a level nobody trusts is
  worse than a level that honestly says "we know nothing about this lead
  yet". It is one branch in `classify_engagement` if the sales team wants it
  back at Level 2.
- **Levels move down as well as up.** `push_engagement_level` refused `1→2`,
  `1→3` and `2→3` until 2026-10-12. That guard is gone; the only remaining
  one is idempotency (pushing the level LeadMe already has is a no-op). The
  pending queue follows the same rule — a newer queued engagement replaces an
  older one rather than the lower level winning.

`app/crm/levels.py::classify_engagement` is the single owner of the decision
and returns the level plus the reason that produced it, so every push can be
traced back to a rule. `mark_engagement` pushes whatever came out. There is
deliberately **no** per-level helper and no event that sets a level directly:
until 2026-10-12 three separate code paths each pushed Level 1 on a booking,
and that is exactly the drift this structure prevents. A booking is half of a
Level 1 criterion again, but it is the classifier that says so, in one place.

#### What the data cannot tell us

- **Real video watching is not observable.** GreenAPI emits no play, watch or
  completion event for media we send. `engaged_with_a_video` uses the only
  available proxy: the lead wrote something substantive *after* the video went
  out. A lead who watches a video and says nothing looks identical to a lead
  who ignored it.
- **Returning-lead detection is partial.** `leadme_reentry_at` is set when a
  LeadMe webhook arrives for a phone we already have a row for. It therefore
  only catches repeat submissions that happened *after* this bot started
  recording leads, and only ones that came through the webhook. LeadMe exposes
  no lead-history or duplicate-submission field in the webhook payload or in
  v3, so a lead who left details a year before the bot existed is invisible
  to us. Measured 2026-10-12: it fires for **zero** of 593 leads.
- **Sending the webinar is not observable; being invited to it is.** There is
  no webinar in `data/videos.json`, so `WEBINAR_VIDEO_IDS` is empty and
  matches nobody — name its id there to switch that half on. What does work
  is LeadMe's own record: a `וובינר` tag or the `פניות וובינר` campaign is
  both a Level 1 source and the content half of the Level 1 pair. Attendance
  itself stays unobservable.
- **The webhook never carries the source, so the bot pulls it instead.**
  LeadMe's external interface fires at record intake, *before* the source,
  campaign, status and tags are written, so the normal shape of a delivery is
  a phone and nothing else: of 100 observed payloads, 11 had a campaign and 2
  had a source. The source is therefore fetched from v3 afterwards —
  `app/crm/leadme_source.py`. v3 has no per-lead endpoint for the source
  field itself (`getLead`, `getLeadDetails`, `getLeadInfo` and their obvious
  variants all 404, probed 2026-10-13), but the human-meaningful source lives
  in the tags anyway, and those are reachable.
- **Paid and organic are indistinguishable without tags.** A Click-To-WhatsApp
  ad arrives with `sourceType` `whatsapp`, exactly like a genuine organic
  arrival. Only its Facebook tags, or the `ctwa_campaign` the bot recorded
  itself, separate the two. This is why the organic Level 1 route ships
  disabled: an untagged WhatsApp lead may simply be an ad whose tags have not
  landed yet.

#### Retracted: "half the bot's leads are not in LeadMe"

Earlier revisions of this file reported that 50–60% of the bot's leads
resolved to no LeadMe row. **That was wrong, and it was our own measurement
error.** The scans that produced it ignored the v3 rate limit, and a
throttled read is reported exactly like a lead LeadMe has never heard of.
Re-checking the same phones at one call per second on 2026-10-13 found **60
of 60 present**. Treat the customer's CRM as complete unless a paced scan
says otherwise.

---

## 7. Tags

### 7.1. Semantics

Tags are free-form Hebrew strings attached to a lead. They show up in
LeadMe's UI as pills below the lead's name. The sales team filters and
groups by tag heavily, so pick short, consistent labels.

**Current bot policy (as of 2026-07-23): only ONE tag is pushed.**

- `חלון · <slot>` (e.g. `חלון · 9-12`, `חלון · 12-15`, `חלון · 15-18`,
  `חלון · any`) – added the moment `preferred_call_slot` is set on the
  lead.
- Separator is the middle-dot **U+00B7 (`·`)**, NOT a colon `:`. This
  matches how LeadMe's UI renders its own tag separators. If you push
  `חלון: 9-12` (colon), the sales team sees an inconsistent style and
  filters may miss it.

**What used to be here and is gone (do NOT re-introduce):**

- `רמה 1 · קבע שיחה` / `רמה 2 · הגיב ולא קבע` / `רמה 3 · לא הגיב` –
  the engagement level tags were removed on 2026-07-22 at the customer's
  request. `LEVEL_TAGS = {}` in `leadme_client.py`. The engagement
  signal now lives ONLY on the status pill (`חדש - רמה 1/2/3`).
- `ביטול שיחה · <reason>` – the cancellation tag was also dropped.
  `push_lead_cancellation` now only flips the status back to `חדש`
  (rel=1).

If you're asked to add a new tag category, extend `push_lead` (or
`push_lead_cancellation`) rather than sprinkling `_admin_add_tag` calls
elsewhere – that way the two-IDs resolution goes through the same code
path.

### 7.2. How to add a tag from Python – the RIGHT way

```python
from app.crm.leadme_client import _admin_add_tag, _resolve_tag_lead_id
from app.crm.leadme_delete import _build_client, find_leadme_id_by_phone

client = _build_client()
lc_id = find_leadme_id_by_phone("972533460489", client)   # "22371797"
tag_lead_id = _resolve_tag_lead_id(client, lc_id)         # "13xxxxxx"
_admin_add_tag(client, tag_lead_id, "חלון · 9-12")
client.close()
```

If you skip `_resolve_tag_lead_id` and pass `lc_id` directly to
`_admin_add_tag`, LeadMe replies `{"result": true}` and the tag simply
does not show up on the lead. This was the exact bug we chased for
half a day. Always resolve.

`_admin_add_tag` returns `True` when LeadMe replied `{"result": true}`.
Idempotency: LeadMe deduplicates internally, so re-adding an existing
tag is safe (no duplicate pills). But note that "success" here does
NOT prove the tag landed – only that LeadMe accepted the POST. The
`_resolve_tag_lead_id` step is what makes it actually land.

### 7.3. How `_resolve_tag_lead_id` works

Lives in `app/crm/leadme_client.py`. Given an `lc_id`, it:

1. Fetches `GET /app/leads/viewLead/<lc_id>` (the human "view lead"
   page).
2. Regex-extracts the first `uploadLeadProfileImage(<id>)` call from
   the returned HTML – that's the JS handler for the avatar upload
   widget, and its argument is the internal `leadId`.
3. Returns that id. On any failure (page 404s, regex miss, cookies
   expired) it falls back to returning the original `lc_id` and the
   subsequent tag POST will no-op – that's the graceful degradation
   we accept in exchange for not needing a dedicated resolver
   endpoint.

If LeadMe ever changes the viewLead template, this regex will silently
break and tags will stop landing. Symptom: `push_lead` logs show
`slot=<something>` but the tag pill never appears in the LeadMe UI.
Fix by scraping a fresh viewLead page and updating the regex.

### 7.4. Removing tags

There is a `/app/ajax/removeLeadTag` endpoint that the UI uses via
`itemRemoved` events on `.tagsinput`. It expects `text` + `leadId`
just like add – and just like add, `leadId` here means the **internal
`leadId`** (§7.3), NOT `lc_id`. We haven't wired it into
`leadme_client.py` yet – add a helper there if/when needed and update
§5.2.

---

## 8. Adding a "call-window" (hours) tag

This is the exact workflow the sales team wanted: when a lead picks a
preferred call slot in WhatsApp (e.g. "9-12"), the bot attaches a
matching tag on the LeadMe row so a rep filtering by slot immediately
sees the queue.

Implementation (as it exists today):

1. The agent's `classify_lead` tool captures `preferred_call_slot` on
   the DB `Lead.lead_metadata` blob (`"9-12"`, `"12-15"`, `"15-18"`,
   or `"any"`).
2. When `schedule_call` fires, the code path is:

   ```
   app/agent/tools.py::schedule_call
     → app/crm/levels.py::classify_engagement  (Level 1 on a fresh booking
                                                with a specific window, but
                                                only alongside our content)
     → app/crm/client.py::mark_engagement
       → app/crm/leadme_client.py::push_engagement_level
         → push_lead
             → find row via get_row_by_phone            (yields lc_id)
             → _admin_change_status(client, lc_id, 7326)  # status pill
     → app/crm/client.py::mark_call_window
       → _resolve_tag_lead_id(client, lc_id)            # -> internal leadId
       → _admin_add_tag(client, internal_leadId, f"חלון · {slot}")
   ```

   The status id is whatever the classifier's level maps to — 7326 only when
   a fresh booking with a named window meets content the lead took in, 7327
   otherwise. The slot tag is attached separately by `mark_call_window`, the
   single owner of that tag, so the booking and the level stay two
   independent facts about the lead.

3. The tag string is literally `f"חלון · {slot}"` – middle-dot
   separator, no colon.
4. If `preferred_call_slot` is missing from `lead_metadata`, no tag is
   pushed. The status change still happens.

**Backfill for historical leads** – `scripts/backfill_slot_tags.py`
walks every `funnel_stage=handed_off` lead in the DB that has a
`preferred_call_slot`, resolves the LeadMe row, and adds the slot tag
if missing. Safe to re-run (LeadMe dedupes internally):

```
docker exec -e PYTHONPATH=/app propeller_bot \
    python scripts/backfill_slot_tags.py --dry-run
docker exec -e PYTHONPATH=/app propeller_bot \
    python scripts/backfill_slot_tags.py
```

If you need a different set of windows (e.g. "אחה"צ", "בוקר"), extend
the canonical list in *two* places:

- `app/agent/prompts.py` – so the LLM asks for those windows.
- `app/agent/tools.py::VALID_SLOTS` – so the classifier accepts them.

Then the tag falls out automatically.

---

## 9. Delete a lead

Two-step flow, both admin endpoints:

```python
from app.crm.leadme_delete import (
    _build_client, find_leadme_id_by_phone, delete_leadme_id,
)

client = _build_client()
leadme_id = find_leadme_id_by_phone("0533460489", client)
if leadme_id:
    ok, detail = delete_leadme_id(leadme_id, client)
client.close()
```

`delete_leadme_id` tries both known endpoint paths
(`/app/ajax/deleteLeads` and `/app/leads/deleteLeads`) and both known
payload key names (`data[leadId][]`, `leadIds[]`), because LeadMe has
moved things around between tenants/versions. It returns
`(True, "deleted leadme_id=… via …")` on success.

**Do not delete leads that Roy manually reclassified.** The
`scripts/classify_existing_leads.py --only-if-still-new` flag is our
reference implementation of "check current status before touching".

---

## 10. Campaigns

### 10.1. Known campaign IDs on this account

| ID    | Name                    | Notes                                        |
|-------|-------------------------|----------------------------------------------|
| 12277 | הוסרו מ-Whatsapp        | **BANNED / TRASH.** The bot must never push into this. Canary watches it. |
| —     | מתעניינים אקדמיה        | Main inbound campaign from the customer's website form. |
| —     | פניות וובינר            | Webinar attendees.                            |
| —     | בוגרים - בעלי רישיונות  | Alumni.                                       |
| —     | שירותי רחפן             | Services enquiries.                          |

The full list is visible in LeadMe UI under `Campaigns → All`. The IDs
we don't have hard-coded aren't actually needed by any bot code today.

### 10.2. Listing leads in a specific campaign

Use `POST /app/ajax2/loadMcfTable/<status>` after priming with a `GET
/app/campaigns/manageCampaign/<campaignId>`. Payload:

```
campaignId  = <id>
tabs        = 1        (or 0/2 for different tabbed views)
userId      = ""       (empty for "all users")
startDate   = 01/06/2020
endDate     = 31/12/2099
csrf_lmcms  = <token>
```

Response:

```json
{"result": true, "cnt": <n>, "data": [ [row], [row], ... ], "tabs": "..." }
```

Row shape matches §5.1.

`scripts/_debug_mcf.py` is a working example.

### 10.3. Sanity-checking the trash campaign

```
docker exec -e PYTHONPATH=/app propeller_bot \
    python scripts/_debug_mcf.py 12277
```

If `cnt` is anything other than `0`, we have a leak. The bot's
scheduler canary logs this automatically – see
`app/followup/scheduler.py::_campaign_leak_canary`.

---

## 11. Reconnaissance playbook – how we discover new endpoints

You will need this every time LeadMe adds a feature or renames an
endpoint. The workflow is deterministic:

1. **Open the LeadMe UI page that already does what you want.** Log in
   as a human. Do the click.
2. **Open DevTools → Network before you click.** Note the exact request
   – URL, method, form fields, request cookies, response body.
3. **Prime by hitting the parent page first.** LeadMe's AJAX endpoints
   often 302 → `/404` unless the session has "seen" a related page in
   the same tab. E.g. `getDataForTable` needs a prior `GET /app/leads`.
4. **Send the exact same request from Python** using
   `_build_client()` – same cookies, same CSRF token, same form keys.
   Compare bodies byte for byte until you get `{"result": true, ...}`.
5. **Only then, wire it into `app/crm/leadme_client.py`** or the
   corresponding module. Add a row to §5 above.

### 11.1. Common trap: HTML instead of JSON

If the response's `content-type` is `text/html`, LeadMe is telling you
either "session expired, go log in" or "404, path unknown" or "CSRF
failure". Look at the first 200 characters – if it's the login page,
refresh cookies (§3.4); if it's a `<title>404 Page Not Found</title>`,
your URL is wrong; if it's `<title>סליחה, אירעה שגיאה!</title>`, the
payload structure is wrong.

### 11.2. Reading the on-page JS

Every LeadMe admin page ships its click handlers inline in the page
HTML. `scripts/_debug_leadme_js.py` and `scripts/_debug_mcf.py` are
templates for scraping those handlers. When you need to figure out how
the UI calls an endpoint, grep the page HTML for the button label
(e.g. `changeStatusPuBtn`) and read the enclosing `$.post` block.

### 11.3. Discovering status IDs

The `dialog_changeStatus` block in any leads/campaigns page is the
source of truth:

```html
<a class="btn ... changeStatusPuBtn" rel="7326" ...>חדש - רמה 1</a>
```

`rel="<id>"` is the numeric ID.

---

## 12. Debug scripts index

Small, disposable helpers that live in `scripts/`. Each is safe to run
against production because they only *read* – they don't push writes
(except `_debug_leadme_update.py`, which requires a leadme_id argument
so it can't accidentally target the wrong lead).

| Script                              | Purpose                                                              |
|-------------------------------------|-----------------------------------------------------------------------|
| `_debug_leadme_row.py <phone>`      | Fetch a lead's full DataTables row (all 8 cells).                    |
| `_debug_leadme_lead.py <leadme_id>` | Try `/app/leads/edit/<id>` and related detail pages.                |
| `_debug_leadme_update.py <id> <status_rel>` | POST `/app/leads/changeLeadsStatus` – changes a real status. |
| `_debug_admin_endpoints.py <id>`    | Probe `addLeadTag`, `addLeadComment` etc. Writes a probe tag.       |
| `_debug_leadme_campaign.py <cid>`   | Scrape the campaign manage page HTML for endpoint hints.            |
| `_debug_leadme_js.py`               | Dump the `changeStatusPuBtn` click handler from `/app/leads`.       |
| `_debug_mcf.py <cid>`               | Call `/app/ajax2/loadMcfTable` + `/app/ajax4/getPieData`.            |
| `_debug_js_url.py`                  | Fetch and grep `manageCampaign_.js` for ajax URLs.                  |
| `_smoke_push.py <phone>`            | Run the full `push_lead(level=1)` code path against a real lead, and verify campaign 12277 count didn't grow. |
| `backfill_slot_tags.py [--dry-run]` | Add missing `חלון · <slot>` tags to every handed-off lead in the DB. Idempotent. |

Run them inside the bot container so they share cookies:

```
docker exec -e PYTHONPATH=/app propeller_bot \
    python scripts/_debug_leadme_row.py 972533460489
```

---

## 13. Common pitfalls we hit (and how we know)

Chronological, so you can recognize the symptoms:

1. **"HTTP 200 but nothing happened."** LeadMe's public endpoints return
   200 for both success and silent failure. Never trust the status code
   alone. Verify by re-reading the row via `getDataForTable`.
2. **`getDataForTable` returns `recordsFiltered=0` for a phone that
   exists.** You're sending the E.164 format. Strip to the last 9
   digits.
3. **`/app/leads/getDataForTable` returns 404.** The endpoint path is
   `/app/ajax/getDataForTable`. LeadMe moved it. `_fetch_row` uses the
   correct one.
4. **`changeLeadsStatus` returns `{"result":false,"msg":"לא נבחרו
   רשומות!"}`.** Your `data[leadId]` is an *array* instead of a
   *comma-joined string*. Fix the payload shape.
5. **Status POST returns `{"result":true}` but the pill doesn't
   change.** You're sending a status *name* like `"חדש - רמה 1"`.
   LeadMe silently ignores names on writes. Send the numeric rel id.
6. **Bot logs `[LeadMe SAFETY] REFUSED to push status/tag ...
   banned campaign`.** A lead's LeadMe row is *inside* campaign 12277.
   Do NOT reinforce that. Move the lead to a real campaign manually,
   then rerun the push.
7. **`[leak-canary] ERROR: campaign 12277 grew from N to M`.**
   Something outside our bot leaked a lead. Check LeadMe automations
   and any other integration on that supplier slug.
8. **CAPTCHA re-appearing every session.** LeadMe's CAPTCHA is
   pinned to session freshness; solving it once is enough for that
   session's cookies, but next re-login will hit it again. Plan for a
   human step in the cookie-refresh workflow.
9. **`recordsTotal=9086` from a "campaign-scoped" search.** The global
   `getDataForTable` is not campaign-scoped. To scope, use
   `loadMcfTable` under `/app/ajax2/` (see §10.2).
10. **Cookies exported from Chrome contain 30+ entries.** Only the
    ones on `*.leadmecms.co.il` matter. `_build_client` already
    filters – if you copy the pattern into a new script, keep the
    filter.
11. **`addLeadTag` returns `{"result":true}` but the tag pill never
    appears on the lead.** You sent the `lc_id` (from `getDataForTable`,
    range `22xxxxxx`) instead of the internal `leadId` (range
    `13xxxxxx`). Route it through `_resolve_tag_lead_id(client, lc_id)`
    first. See §7.2–7.3.
12. **Slot tag renders as `חלון: 9-12` instead of `חלון · 9-12`.**
    You used a colon. LeadMe's UI uses a middle-dot (U+00B7) and the
    sales team filters exact strings. Fix in whatever script generated
    the tag.

---

## 14. Environment variables cheat sheet

| Var                       | Meaning                                                   |
|---------------------------|-----------------------------------------------------------|
| `LEADME_ADMIN_BASE`       | `https://www.leadmecms.co.il` – base for admin endpoints. |
| `LEADME_COOKIES_PATH`     | Path (in container) to the cookies JSON file.             |
| `LEADME_INSERT_URL`       | **Leave empty.** Historical stub for `/supplier/insert`.  |
| `LEADME_UPDATE_URL`       | **Leave empty.** Historical stub for `/supplier/update`.  |
| `LEADME_INSERT_MODE`      | `update-only` (default) / `never` (kill switch). Ignored by the current admin-only client except as a kill switch when set to `never`. |
| `LEADME_STATUS_ID`        | Fallback status id if the level-1 tier var is empty.      |
| `LEADME_STATUS_LEVEL_1`   | Numeric id for "חדש - רמה 1" (7326 on this account).      |
| `LEADME_STATUS_LEVEL_2`   | Numeric id for "חדש - רמה 2" (7327).                       |
| `LEADME_STATUS_LEVEL_3`   | Numeric id for "חדש - רמה 3" (7328).                       |
| `LEADME_SOURCE_LABEL`     | Tag prefix. Currently `WhatsApp Bot`.                    |
| `LEADME_TEST_MODE`        | If truthy, `push_lead` becomes a no-op (log only). Use in eval runs. |

---

## 15. Cheatsheet: from zero to "I can move a lead's status"

```python
# In a container shell with PYTHONPATH=/app.
from app.crm.leadme_client import (
    _admin_change_status, _admin_add_tag, _resolve_tag_lead_id,
)
from app.crm.leadme_delete import _build_client, find_leadme_id_by_phone

client = _build_client()                                   # loads cookies
lc_id = find_leadme_id_by_phone("972533460489", client)    # -> "22371797"

# Status uses lc_id directly.
_admin_change_status(client, lc_id, "7326")                # -> חדש - רמה 1

# Tags need the internal leadId. Resolve first, then add.
tag_lid = _resolve_tag_lead_id(client, lc_id)              # -> "13xxxxxx"
_admin_add_tag(client, tag_lid, "חלון · 9-12")             # middle-dot!

client.close()
```

That's it. Everything else in this doc is either why this snippet
looks the way it does, or how to add a new capability without falling
into one of the historic traps.
