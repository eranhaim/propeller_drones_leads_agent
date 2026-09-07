# CLAUDE.md

Guidance for Claude Code working in this repository.

These are rules, not suggestions.

## 1. Communication

Keep every response **short, direct, simple, and clear**.

* Answer only what was asked.
* Lead with the result.
* No preamble, repetition, recap, or narration of tool use.
* No slang.
* Use simple English and short sentences.
* Do not explain obvious things.
* If something failed, say what failed and why.
* If something was skipped, say what and why.
* If there is one clear answer, give it. Do not present unnecessary options.

The same standard applies to code, comments, names, and documentation.

**Simple is the default.**

---

## 2. Code

Write the **simplest correct code**.

* Prefer clear and explicit code over clever or compact code.
* Prefer standard language features over custom abstractions.
* Avoid unnecessary wrappers, generics, configuration, indirection, state, and dependencies.
* Do not optimize without a real requirement or measurement.
* Do not build for hypothetical future requirements.
* If simple code and clever code both work, choose simple code.

### Naming

Names must be:

* English
* Clear
* Descriptive
* Consistent
* Easy to pronounce

Use one name for one concept everywhere.

Functions describe their effect:

```text
push_engagement_level
get_or_create_lead
mark_video_sent
```

Avoid vague or implementation-based names:

```text
process_data
handle_thing
do_leadme_stuff
```

Booleans read as assertions:

```text
is_allowed
has_experience
bot_muted
```

Do not use negated names such as `not_disabled`.

Module-private helpers keep the leading underscore (`_normalize_phone`,
`_pick_leads_to_nudge`). That convention is used consistently and is how a
reader tells the public surface from the internals.

If a name is wrong, rename it and update all call sites.

**Hebrew is data, not code.** Prompt text, nudge copy, opener templates, and
user-facing replies are Hebrew. Identifiers, comments, docstrings, log
messages, and commit messages stay English.

---

## 3. Scope

Make the **smallest change that fully solves the request**.

* Do not reduce requested scope.
* Do not expand scope without a reason.
* Do not fix unrelated problems.
* Do not refactor unrelated code.
* Do not add opportunistic cleanup.

If an unrelated issue is found, leave it alone unless it blocks the requested work or verification.

Small does not mean incomplete.

---

## 4. Read Before Writing

Before changing code:

* Read the relevant files and nearby code.
* Understand the data flow, types, and call sites.
* Match existing conventions.
* Check error handling.
* Do not guess when the repository can answer the question.

Documentation lives in two places. **`handover/` is implementation-independent
and deliberately contains no code references — do not add any.** `docs/` is the
code-level material.

| Touching | Read first |
|---|---|
| Anything, first time | `handover/business_logic.md` |
| LeadMe protocol (endpoints, cookies, ids) | `docs/leadme_integration.md` |
| LeadMe client code (levels, queue, guards) | `docs/reference/leadme_client_technical.md` |
| WhatsApp / GreenAPI | `docs/reference/greenapi_technical.md` |
| The agent, its tools or prompt | `docs/reference/agent_runtime.md`, `docs/bot_flow.md` |
| Knowledge base / RAG | `knowledge/README.md` |
| Any LeadMe change | also `docs/handoff_2026_07_26.md` (anti-patterns) |

Those documents record traps that have already cost the customer real leads.

If an existing convention is harmful, do not silently create a second convention. State it and fix it consistently.

---

## 5. Design

For major architectural changes, design before coding.

Keep it short:

1. **Behavior** — what should happen.
2. **Design** — target structure and responsibilities.
3. **Conflicts** — what current code disagrees with it.
4. **Changes** — keep / modify / replace / split / merge / delete / add.
5. **Risks** — what may break or cannot be verified.

Do not do this for small fixes or simple changes.

For substantial changes ask:

> Would I choose this design if I were building the system today?

Existing code is evidence of past decisions, not a reason to preserve a bad design.

---

## 6. Abstractions

Use an abstraction only when it makes the code clearer.

For non-trivial abstractions, use one verdict:

* **Keep**
* **Modify**
* **Replace**
* **Split**
* **Merge**
* **Delete**
* **Add**

Warning signs:

* One caller
* Vague name
* Parameters only switch behavior
* Only forwards values
* Hides simple logic
* Exists mainly to avoid changing a caller

Prefer direct code when direct code is clearer.

---

## 7. Legacy Code

Do not preserve code just because it exists.

When new code makes old code unnecessary, delete it.

Do not add:

* Wrappers
* Adapters
* Shims
* Parallel implementations
* `_old`, `_v2`, `_legacy`
* Commented-out code

Update call sites instead.

Alembic migrations are the exception: they are append-only history (see §15).

---

## 8. Priority

When rules conflict:

**Correctness → Simplicity → Clear Responsibilities → Maintainability**

Correctness includes edge cases, errors, failure paths, concurrency, and data consistency.

Simplicity means fewer moving parts, not fewer characters.

Each file, module, and function should have a clear responsibility.

Do not add flexibility for requirements that do not exist.

---

## 9. Implementation

Follow the design and keep the change focused.

If the design becomes wrong:

1. Stop.
2. Explain why.
3. Update the design.
4. Continue.

Do not patch around a known-bad design.

---

## 10. Comments

Comments explain **why**, not **what**.

Comment only for:

* External system behavior (GreenAPI, LeadMe, OpenAI, WooCommerce quirks)
* Important constraints
* Non-obvious ordering
* Deliberate omissions
* Reasons an obvious solution was rejected
* Temporary migration conditions

This codebase leans heavily on "why" comments around the external
integrations, and that is deliberate — LeadMe's behavior is undocumented and
reverse-engineered. Keep that density there. Do not import it into ordinary
application code.

If code needs a comment to explain what it does, improve the code or name instead.

Delete stale comments.

---

## 11. Verification

Verify the affected code as far as the environment allows.

There is no unit-test suite (`tests/` holds only `__init__.py`). The real
verification tools are:

```bash
# Offline eval harness -- drives the LIVE agent through the customer's
# rejected conversations. This is the closest thing to a test suite.
docker compose exec -T bot python -m scripts.eval_rejects

# Import / syntax check without a running stack
python -m compileall app scripts

# Interactive chat simulator (no WhatsApp, no LeadMe writes)
#   http://localhost:8082/admin/simulator
docker compose logs -f bot
```

`scripts/eval_rejects.py` sets `LEADME_TEST_MODE=1` before importing anything
and uses synthetic `999…` phone numbers. Both guards must stay — without them
the harness writes fake leads into the customer's real CRM.

Prompt changes cannot be verified by reading. Run the eval harness or the
simulator, and say which conversations you actually drove.

Clearly distinguish:

* **Verified** — actually checked.
* **Not verified** — could not be checked.
* **Reasoned about** — reviewed but not executed.

Never claim verification that did not happen.

---

## 12. Readability

Before finishing, review the changed code as a new reader.

Check:

* Clear names
* Consistent terminology
* Logical file and function order
* Related code together
* Short, understandable functions
* No unnecessary complexity
* No debug output
* No dead code
* Clear error paths
* Useful comments

A readability pass must not change behavior.

---

## 13. Repository

Propeller Drones: a WhatsApp lead-conversion bot. Leads arrive from paid ads
(Meta/TikTok CTWA and the customer's website form via LeadMe), the bot holds a
free-flowing Hebrew conversation, and warm leads are handed to a human sales
rep through the LeadMe CRM. Read `README.md` and `docs/bot_flow.md` before
large changes.

One Python 3.11 service, plus Postgres and Chroma. Three long-lived threads in
one container (`app/main.py`):

1. **GreenAPI long-poll loop** (main thread) — inbound WhatsApp messages.
2. **FastAPI/uvicorn server** (daemon thread) — LeadMe webhook + admin UI.
3. **APScheduler** (daemon thread) — follow-up nudges, LeadMe queue drain,
   session-health and campaign-leak canaries.

```
app/
├── main.py          # entry: migrations, then the three threads
├── config.py        # every env var, one pydantic Settings class
├── whatsapp/        # GreenAPI inbound handler + outbound ChatSender
├── agent/           # LangGraph agent: graph, prompts, tools, context, classifier
├── rag/             # Chroma ingest + retrieval
├── videos/          # video catalog loaded from data/videos.json
├── crm/             # LeadMe: client, v3 API, delete, login, retry queue
├── webhook/         # FastAPI app + the canned first-contact opener
├── admin/           # HTML admin panel (leads, conversations, simulator)
├── followup/        # APScheduler nudges and canaries
├── shopify/         # unused today; WooCommerce is the live store client
├── woocommerce/     # read-only product catalog
└── db/              # models, session, repository, alembic migrations
```

### Commands

```bash
# Full stack
docker compose build
docker compose up -d postgres chroma
docker compose run --rm bot python -m scripts.ingest_knowledge --reset
docker compose up -d bot
docker compose logs -f bot

# Local (infra in Docker, app on the host)
python -m venv .venv
pip install -r requirements.txt
docker compose up -d postgres chroma
alembic upgrade head
python -m scripts.ingest_knowledge --reset
python -m app.main
```

Local runs need `DATABASE_URL` pointed at `localhost:5432` and `CHROMA_HOST=localhost`.

---

## 14. Environment and Ports

One `.env` at the repo root (see `.env.example`). Never commit it. Every
setting is declared in `app/config.py` — add new ones there with an explicit
`alias` and a default, and document them in `.env.example`.

* Webhook + admin UI: container `8080`, published on the host as
  `127.0.0.1:8082` (override with `WEBHOOK_HOST_PORT`). Loopback only — the
  host's nginx reverse-proxies public HTTPS to it.
* Postgres (`postgres:5432`) and Chroma (`chroma:8000`) have **no** published
  ports. They are reachable only inside the compose network. The commented-out
  `ports:` blocks in `docker-compose.yml` are for debugging; leave them commented.
* Production runs on EC2 (`ubuntu@54.173.144.0`). Deploy is manual: SSH in,
  `git pull`, `docker compose up -d --build bot`.

Endpoints:

* `GET  /health`
* `POST /webhook/leadme/{secret}` — must match `WEBHOOK_SECRET`
* `/admin/*` — HTTP Basic (`ADMIN_USER` / `ADMIN_PASSWORD`)

Check the environment before debugging connection issues.

---

## 15. Invariants

### Never lose a lead's message

This is the system's whole reason to exist. Concretely:

* `delete_notifications_at_startup=False` on the GreenAPI bot — a redeploy
  must not discard queued notifications.
* `handle_message` persists the inbound user message in its **own**
  transaction before the agent runs, so an agent crash still leaves a record.
* If the GreenAPI bot fails to start, `main.py` blocks forever instead of
  exiting, so the webhook and admin UI stay up.
* Every CRM push is wrapped in `try/except` and logged. A LeadMe failure must
  never block or delay the reply to the lead.

### The agent pipeline

`app/agent/graph.py::handle_message` is the single entry point for a lead's
message — WhatsApp, the admin simulator, and the eval harness all go through it.
Two transactions:

1. Persist the inbound message; decide the engagement level to push.
2. Build the prompt, invoke the agent, post-process, persist the reply.

Post-processing is a hard safety net for prompt rules the LLM keeps breaking,
and each layer exists because of a specific customer complaint:

* `_looks_like_english` → one retry with a Hebrew reminder, then a canned Hebrew fallback.
* `_strip_filler` → removes trailing "אני כאן בשבילך"-style sign-offs.
* `_strip_markdown_links` → WhatsApp does not render markdown.
* `_enforce_booking_promise` → if the reply *claims* a call was booked but
  `schedule_call` never fired, book it. Never let the bot lie to a lead.

When you tighten a prompt rule, ask whether the safety net also needs updating.
Prompt rule and post-processing rule must agree.

### Lead state

`leads` + `messages` in Postgres, `app/db/models.py`. Structured state lives in
columns (`familiarity_level`, `funnel_stage`, `videos_sent`, `bot_muted`);
everything else lives in the `lead_metadata` JSON blob (`intent`, `industry`,
`preferred_call_slot`, `has_experience`, all `leadme_*` keys, the push queue).

Write metadata only through `repository.update_lead_metadata` — it merges. A
direct assignment to `lead.lead_metadata` silently drops keys another thread
just wrote. That has already caused a real bug (see the CTWA fast path in
`app/whatsapp/handler.py`).

Every turn the lead's state is re-rendered into the system prompt by
`classifier.describe_state`, so the LLM never has to remember it.

A lead idle for `SESSION_RESET_DAYS` (7) is reset on their next message: stage,
familiarity and videos clear, history is hidden behind
`lead_metadata["session_reset_at"]` (nothing is deleted), and they get a fresh
opener instead of an agent reply. `_LEADME_KEYS` in `app/db/repository.py`
lists what survives a reset — `leadme_last_level` is deliberately **not** in it.

### Call windows

The only valid `preferred_call_slot` values are `9-12`, `12-15`, `15-18`,
`any`, `none`. `_VALID_SLOTS` in `app/agent/tools.py` rejects anything else —
the LLM has recorded a city name and an industry as a slot. No calls are
scheduled on Friday or Saturday.

### LeadMe

`app/crm/client.py` is the facade; `app/crm/leadme_client.py` is the only
module that writes to LeadMe. The rules, in full in `docs/leadme_integration.md`:

* **Never create a LeadMe row from the bot.** The public `/supplier/insert`
  and `/supplier/update` endpoints behave as upserts and drop unmatched leads
  into campaign `12277` (`הוסרו מ-Whatsapp`, the trash bucket).
  `LEADME_INSERT_MODE` defaults to `update-only` for this reason.
  `_campaign_leak_canary` in the scheduler watches that campaign's count.
* Writes go through the internal admin endpoints with session cookies, or the
  v3 REST API when `LEADME_API_KEY` is set. Cookies hard-expire every 24h;
  `app/crm/leadme_login.py` refreshes them via 2Captcha.
* Engagement levels are **upgrade-only**, and lower means more engaged:
  `1` = booked a call, `2` = replied to the bot, `3` = never replied.
  `push_engagement_level` refuses `1→2`, `1→3`, and `2→3`.
* CTWA leads reach the bot before LeadMe's supplier sync knows their phone.
  A push that cannot resolve the phone is enqueued in
  `lead_metadata["leadme_push_pending"]` and drained by the scheduler
  (`app/crm/leadme_queue.py`). Do not add a new "retry a few times then give
  up" loop — that is the bug the queue was built to fix.
* `LEADME_TEST_MODE=1` turns every LeadMe write into a logged no-op. The eval
  harness depends on it.

### RAG and facts

The bot must not invent facts about courses, licenses, prices, or locations.
Answers come from `search_knowledge`, which reads the Chroma collection built
by `scripts/ingest_knowledge.py` from the Propeller website plus
`knowledge/**/*.md`. Topic filtering uses the `topic` metadata field —
adding a topic means updating the ingest mapping *and* the `search_knowledge`
docstring, because that docstring is what the LLM reads to pick a topic.

Shop questions bypass RAG and hit the live WooCommerce catalog, so prices and
stock are real.

### Tools need context

LangChain tools get no arguments identifying the lead. `AgentContext` is
carried in a `ContextVar` set by `use_context` around the agent invocation
(`app/agent/context.py`). A tool called outside that scope raises. Any new tool
gets its lead and session from `current_context()` — never from a global, and
never from a parameter the LLM would have to fill in.

### Migrations

`app/db/migrations/versions/` is append-only. Never edit an applied migration;
add a new revision and update `app/db/models.py` to match. `main.py` runs
`alembic upgrade head` on every boot, so a broken migration takes the bot down.

---

## 16. Important Gotchas

### The prompt is the product

`app/agent/prompts.py` holds a ~600-line Hebrew system prompt. Most behavior
bugs are fixed there, not in Python. Its structure is deliberate:

* The numbered "rules you must never break" block sits near the top for
  primacy. It is a summary — each rule is spelled out again further down.
* Rules are written as named laws (`חוק המחיר`, `חוק חלון-השעות`) so a bad
  conversation can be traced back to the rule it violated.
* Almost every rule exists because the customer rejected a real conversation.
  Do not delete or soften one without knowing which complaint it answers.

When adding a rule, add it in both places (summary and detail) and keep the
numbering consistent.

### No markdown on WhatsApp

WhatsApp renders `[label](url)` as literal brackets. This is enforced three
times: in the prompt, in `_strip_markdown_links`, and in how videos of
`kind="link"` are sent as plain text. Any new outbound copy — nudges, openers,
admin-triggered messages — must be plain text too.

### Replies are deliberately slow

`ChatSender.send_text` shows a typing indicator and sleeps 2–6 seconds
proportional to the reply length. Instant replies read as a bot. Do not
"optimize" this away.

### Follow-ups have quiet hours

The scheduler only nudges between 09:00 and 20:00 Asia/Jerusalem, at most
`FOLLOWUP_MAX_NUDGES` times, and never for a lead who is `bot_muted`, booked,
or flagged `not_relevant`. Any new outbound automation must respect the same
constraints.

### The admin UI is hand-written HTML

`app/admin/routes.py` returns HTML strings — no templates, no framework. One
shared CSS block and a `_page()` wrapper. Follow that pattern; do not
introduce Jinja, a component library, or a frontend build for one panel.
Everything user-supplied goes through `_escape`.

### Caches are process-wide

`@lru_cache` wraps the settings, the chat model, the agent, the retriever, the
embeddings, and the video catalog. Editing `data/videos.json` or `.env` needs a
container restart, not just a new request.

### Scripts starting with `_debug_`

`scripts/_debug_*.py` are single-purpose LeadMe reconnaissance tools, indexed
at the bottom of `docs/leadme_integration.md`. They are reference material, not
dead code. Leave them alone unless asked.

### Secrets

`.env`, `*.pem`, and `data/leadme_cookies.json` are gitignored and hold live
credentials for the customer's CRM and WhatsApp number. Never print them, never
commit them, never paste them into a log line or a doc.

---

## 17. Known Issues

* No automated test suite. `scripts/eval_rejects.py` is the only regression net.
* LeadMe has no official admin API; the cookie-impersonation path is fragile by
  nature. `docs/leadme_api_escalation.md` is the open ask to the vendor.
* `app/shopify/` is wired to config but unused — WooCommerce is the live store
  integration.

---

## 18. Commits

Conventional-ish, scoped, one concern each:

```text
feat(scope):
fix(scope):
refactor(scope):
chore:
infra:
docs(scope):
```

Common scopes in this repo: `agent`, `prompt`, `leadme`, `admin`, `followup`,
`rag`, `whatsapp`.

Keep commit messages short, clear, and in English.

---

## Final Rule

Choose the simplest correct solution.

Use clear names.

Use few moving parts.

Do not add complexity without a real reason.

**Simple code. Simple explanations. Clear names. One implementation per responsibility.**
