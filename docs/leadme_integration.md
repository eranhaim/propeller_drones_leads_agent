# LeadMe CMS integration – field manual

This document is the operating manual for anyone (human or AI agent) that
needs to make the Propeller Drones bot – or any sibling tool – talk to
[LeadMe CMS](https://www.leadmecms.co.il) reliably. It captures every
endpoint we reverse-engineered, every trap we fell into, and the exact
recipes we settled on. If you find something new, please add a section
here.

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
2. **All bot-side writes go through the internal admin endpoints** with
   session cookies (see §3). Concretely:
   - Change status pill → `POST /app/leads/changeLeadsStatus`
   - Attach a tag       → `POST /app/ajax/addLeadTag`
   - Delete a lead      → `POST /app/ajax/deleteLeads`
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

---

## 2. Two API surfaces – what to use when

LeadMe exposes two very different HTTP surfaces. Understanding which is
which is the single most important thing.

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

### 2.2. Internal admin API — USE THIS

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
| 0   | Checkbox HTML: `<input name="selectedLeads[]" value="<leadme_id>"/>`         |
| 1   | Plain numeric `leadme_id`                                                    |
| 2   | Lead name                                                                    |
| 3   | Phone (formatted `053-346-0489`)                                             |
| 4   | Campaign name (plain text, e.g. `מתעניינים אקדמיה`)                          |
| 5   | Status HTML: `<span class="label status" ...>חדש - רמה 1</span>`             |
| 6   | Created timestamp `dd/mm/YYYY HH:MM`                                         |
| 7   | Action buttons (view / edit) with `javascript:showLeadSummary(<id>)` etc.    |

### 5.2. Writes

| Purpose         | Endpoint                        | Payload (form-encoded)                                                                 |
|-----------------|---------------------------------|----------------------------------------------------------------------------------------|
| Change status   | `/app/leads/changeLeadsStatus`  | `data[status]=<rel_id>`, `data[leadId]=<id>[,<id>...]`, `csrf_lmcms=<token>`           |
| Add tag         | `/app/ajax/addLeadTag`          | `text=<tag>`, `leadId=<id>`, `csrf_lmcms=<token>`                                       |
| Delete lead(s)  | `/app/ajax/deleteLeads`         | `data[leadId][]=<id>` (or `leadIds[]=<id>` in some tenants), `csrf_lmcms=<token>`      |

**Trap**: `data[leadId]` for `changeLeadsStatus` is a *comma-joined
string*, not an array. The UI JS is:

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
Current mapping for the Propeller account (as of 2026-07):

| Status label (Hebrew)                | Numeric ID |
|--------------------------------------|-----------:|
| חדש (default)                        | 1          |
| חדש - רמה 1                          | 7326       |
| חדש - רמה 2                          | 7327       |
| חדש - רמה 3                          | 7328       |
| שיחה חוזרת                          | 14         |
| אין מענה 1 / 2 / 3                   | 4 / 5 / 1623 |
| רלוונטי ליום פתוח                   | 7155       |
| פולואפ - מחכה למימון חיצוני         | 250        |
| פולואפ - ניתנה הצעה                 | 5827       |
| מאגר - בדיקה                         | 5839       |
| נקבעה פגישה                          | 7          |
| ניוזלטר                              | 7228       |
| בוצעה עסקה                           | 10         |
| לקוח קיים                            | 609        |
| לא נסגר – כללי / 2 טון / מגמה / ביזנס / 25 ק"ג / חנות | 5821 / 7220 / 7221 / 7229 / 7222 / 5841 |
| הסרה מרשימות תפוצה                  | 6570       |
| לא רלוונטי                          | 2392       |

These IDs are per-account. If Roy adds a new status, re-run
`scripts/leadme-recon/get_status_ids.py` (or scrape any campaign page's
HTML) to refresh the mapping.

The engagement-level IDs (7326/7327/7328) live in `.env` as
`LEADME_STATUS_LEVEL_1/2/3`. Everything else is used ad-hoc in
one-off scripts.

---

## 7. Tags

### 7.1. Semantics

Tags are free-form Hebrew strings attached to a lead. They show up in
LeadMe's UI as pills below the lead's name. The sales team filters and
groups by tag heavily, so pick short, consistent labels.

The bot uses three canonical engagement tags (see
`app/crm/leadme_client.py::LEVEL_TAGS`):

- `רמה 1 · קבע שיחה` – applied when a call slot is captured.
- `רמה 2 · הגיב ולא קבע` – applied on the lead's first inbound.
- `רמה 3 · לא הגיב` – applied when the follow-up scheduler exhausts
  its nudges.

And an ad-hoc slot tag: `חלון: 9-12` (or `12-15`, `15-18`, `any`) –
added the moment a `preferred_call_slot` is set on the lead.

### 7.2. How to add a tag from Python

```python
from app.crm.leadme_client import _admin_add_tag
from app.crm.leadme_delete import _build_client, find_leadme_id_by_phone

client = _build_client()
leadme_id = find_leadme_id_by_phone("972533460489", client)
_admin_add_tag(client, leadme_id, "רמה 1 · קבע שיחה")
client.close()
```

`_admin_add_tag` returns `True` when LeadMe replied `{"result": true}`.
Idempotency: LeadMe deduplicates internally, so re-adding an existing
tag is safe (no duplicate pills).

### 7.3. Removing tags

There is a `/app/ajax/removeLeadTag` endpoint that the UI uses via
`itemRemoved` events on `.tagsinput`. It expects `text` + `leadId` just
like add. We haven't wired it into `leadme_client.py` yet because we
never need to remove – if you build it, add it there and update the
table in §5.2.

---

## 8. Adding a "call-window" (hours) tag

This is the exact workflow the sales team wanted: when a lead picks a
preferred call slot in WhatsApp (e.g. "9-12"), the bot should attach a
matching tag on the LeadMe row so a rep filtering by slot immediately
sees the queue.

Implementation (already in production):

1. The agent's `classify_lead` tool captures `preferred_call_slot` on
   the DB `Lead.lead_metadata` blob (`"9-12"`, `"12-15"`, `"15-18"`,
   or `"any"`).
2. When `schedule_call` fires, the code path is:

   ```
   app/agent/tools.py::schedule_call
     → app/crm/client.py::mark_ready_for_call
       → app/crm/leadme_client.py::push_engagement_level(lead, level=1)
         → push_lead
           → _admin_change_status(client, id, 7326)   # status pill
           → _admin_add_tag(client, id, "רמה 1 · קבע שיחה")
   ```

3. The `חלון: <slot>` tag is added in `_build_insert_payload`-style
   logic that lives on the current `push_lead` (see the block that
   composes `tag_parts`; the `preferred_call_slot` metadata becomes
   the `חלון: X-Y` tag).

If you need a different set of windows (Roy asked for e.g. "אחה"צ",
"בוקר"), extend the canonical list in *two* places:

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
from app.crm.leadme_client import _admin_change_status, _admin_add_tag
from app.crm.leadme_delete import _build_client, find_leadme_id_by_phone

client = _build_client()                       # loads cookies
lid = find_leadme_id_by_phone("972533460489", client)  # -> "22371797"
_admin_change_status(client, lid, "7326")      # -> חדש - רמה 1
_admin_add_tag(client, lid, "רמה 1 · קבע שיחה")
_admin_add_tag(client, lid, "חלון: 9-12")
client.close()
```

That's it. Everything else in this doc is either why this snippet
looks the way it does, or how to add a new capability without falling
into one of the historic traps.
