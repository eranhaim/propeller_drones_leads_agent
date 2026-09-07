# Agent rules and context — technical reference

The runtime contract of the LLM agent: how a turn is assembled, what context is
injected and from where, the tool interface, the validation and post-processing
rules enforced in Python, and transaction boundaries.

This is about mechanics. The conversational content of the system prompt lives
in `app/agent/prompts.py`; the conversational flow is diagrammed in
`docs/bot_flow.md`.

| File | Role |
|---|---|
| `app/agent/graph.py` | entry points, turn assembly, post-processing, simulator |
| `app/agent/prompts.py` | prompt template + `render_system_prompt` |
| `app/agent/tools.py` | tool definitions and `ALL_TOOLS` |
| `app/agent/context.py` | `AgentContext`, the `ContextVar` injection |
| `app/agent/classifier.py` | value parsing/persistence + `describe_state` |

---

## 1. Agent construction

```python
_model()  = ChatOpenAI(model=OPENAI_CHAT_MODEL, temperature=0.4)
_agent()  = create_react_agent(model=_model(), tools=ALL_TOOLS)
```

Both are `@lru_cache(maxsize=1)`. LangGraph's prebuilt ReAct agent — no custom
graph, no checkpointer, no LangGraph-managed memory. **The agent is stateless
between turns.** Continuity comes entirely from the message list we rebuild on
every call from Postgres.

Consequences:

* changing `OPENAI_CHAT_MODEL` needs a process restart,
* there is no thread/session id to pass,
* nothing persists in the graph — if it is not in the DB, the next turn does
  not know it.

---

## 2. Entry points

```python
handle_message(phone, text, sender_name=None, send_video_fn=None) -> str
simulate_message(session_id, text) -> {"reply": str, "state": dict}
```

`handle_message` is the single production entry point. WhatsApp
(`whatsapp/handler.py`), the follow-up scheduler, and `scripts/eval_rejects.py`
all call it. It returns the reply text; **it does not send it** — except on the
session-reset path, where it sends an opener itself and returns `""`.

An empty return means "nothing to send". Callers must handle it.

---

## 3. Turn assembly

The message list handed to the agent:

```
[ SystemMessage(render_system_prompt(describe_state(lead))) ,
  … up to 30 stored messages, oldest first … ]
```

The new user message is **not** appended separately — it was already written to
the DB in transaction 1, so it arrives as the last item of the history.

### 3.1 System prompt composition

`render_system_prompt(lead_state_description)` performs three literal
substitutions on `SYSTEM_PROMPT_TEMPLATE`:

| Placeholder | Source | Content |
|---|---|---|
| `{videos_catalog}` | `videos.catalog.list_for_prompt()` | one line per catalog entry: id, kind, title, trigger stage, familiarity levels, topics |
| `{lead_state}` | `classifier.describe_state(lead)` | the lead's current DB state |
| *(appended to `{lead_state}`)* | `render_system_prompt` | current weekday in Hebrew, from `Asia/Jerusalem` |

Substitution uses `str.replace`, **not** `str.format`. The template contains
literal braces in its instructions, so `format()` would raise `KeyError`. Keep
it that way when editing.

The weekday is computed per call, so a long-running process does not serve a
stale day.

### 3.2 Injected lead state

`describe_state(lead)` renders, from columns and `lead_metadata`:

```
name · familiarity_level · funnel_stage · intent · industry
has_experience · preferred_call_slot · videos_sent
```

Plus a conditional banner prepended when `funnel_stage == handed_off` or when
`preferred_call_slot` is already set.

This is re-rendered every turn. The model is never asked to remember state
across turns — anything it must know has to be persisted by a tool first and
will reappear here on the next message.

### 3.3 History window

`_history_as_messages` → `repository.recent_messages(limit=HISTORY_LIMIT)`.

| Constant | Value |
|---|---|
| `HISTORY_LIMIT` | 30 |
| `SESSION_RESET_DAYS` | 7 |

Only `user` and `assistant` rows are converted (to `HumanMessage` /
`AIMessage`). `system` and `tool` rows are stored but never replayed — **tool
calls and their results do not survive the turn.**

If `lead_metadata["session_reset_at"]` is set, only messages created after that
timestamp are returned. Nothing is deleted; older history is filtered out of
the model's view.

Ordering: the query takes the newest 30 by `created_at DESC`, then reverses to
oldest-first.

---

## 4. Tool context injection

LangChain tools receive only the arguments the model supplies. Nothing in a
tool signature identifies the lead. That is carried out of band:

```python
ctx = AgentContext(session=session, lead=lead, send_video=send_video_fn)
with use_context(ctx):
    result = _agent().invoke({"messages": input_messages})
```

`use_context` sets a `ContextVar` and resets it on exit. Inside a tool:

```python
ctx = current_context()   # raises RuntimeError if called outside use_context
```

`AgentContext` fields:

| Field | Type | Purpose |
|---|---|---|
| `session` | `Session` | the open SQLAlchemy session of transaction 2 |
| `lead` | `Lead` | the live ORM instance |
| `send_video` | `callable \| None` | outbound media callback, supplied by the caller |
| `videos_sent_this_turn` | `set[str]` | in-turn dedup guard |

**Any new tool takes its lead and session from `current_context()`** — never a
module global, and never a parameter the model would have to fill in.

`ContextVar` is per-thread and per-task, so concurrent leads do not collide.

---

## 5. Tool interface

`ALL_TOOLS` — eight tools, all returning a Hebrew string that the model reads
as an observation.

| Tool | Arguments | Side effects |
|---|---|---|
| `search_knowledge` | `query`, `topic?` | none (Chroma read; `topic="shop"` diverts to WooCommerce) |
| `classify_lead` | `familiarity?`, `stage?`, `intent?`, `industry?`, `preferred_call_slot?`, `has_experience?` | writes columns + `lead_metadata` |
| `send_video` | `video_id`, `caption?` | GreenAPI send, `videos_sent`, `video_sent_at` / `webinar_sent_at` |
| `recommend_video` | `topics_context?` | none |
| `schedule_call` | `summary?`, `preferred_call_slot?` | `funnel_stage=handed_off`, CRM level 1 |
| `cancel_call` | `reason?` | clears slot, `funnel_stage=warm`, CRM cancel note |
| `mark_not_relevant` | — | session reset + `not_relevant` flag |
| `search_shop_products` | `query` | none (WooCommerce read) |

### 5.1 Return-string conventions

Tool results are prompt input, so they are written as instructions to the
model, in Hebrew. Two conventions matter:

* **`NOT_AN_ERROR:` prefix.** Used when a tool completed correctly but had
  nothing to do — `schedule_call` without a slot, `search_shop_products` with
  no match. Without it the model reported a technical fault to the user.
  Any new "successful but empty" branch should use it.
* **Rejection is reported back, not silent.** `classify_lead` appends a warning
  describing the value it discarded, so the model can correct itself in the
  same turn.

### 5.2 Errors

Tools do not raise into the agent loop. Failures are caught, logged, and
returned as a Hebrew string. A raising tool would abort the whole invocation
and drop the turn.

---

## 6. Rules enforced in Python

Prompt rules are advisory; these are not. Each exists because the prompt rule
alone failed in production.

### 6.1 Slot whitelist

```python
_VALID_SLOTS = {"9-12", "12-15", "15-18", "any", "none"}
```

Enforced in both `classify_lead` and `schedule_call`. A non-member value is
discarded (never persisted), logged at `WARNING`, and reported back in the tool
result. Free-text times, city names, and industry names have all been emitted
here by the model.

`schedule_call` accepts an inline slot so a booking does not require a prior
`classify_lead` call; it validates identically, then falls back to the stored
value.

### 6.2 Video dedup — two layers

| Layer | Check | Catches |
|---|---|---|
| DB | `video.id in lead.videos_sent` | across turns |
| In-turn | `video.id in ctx.videos_sent_this_turn` | two `send_video` calls in one reply |

The DB layer alone is insufficient: `videos_sent` is only committed at the end
of the transaction, so a second call in the same turn still reads the old list.

### 6.3 Post-processing pipeline

Applied to the raw reply, in this order, in `handle_message`:

1. **`_looks_like_english`** — if the trimmed reply is ≥ 20 characters and
   Hebrew characters are < 15 % of all alphabetic characters, re-invoke the
   agent once with an explicit Hebrew reminder appended. If the retry is still
   non-Hebrew, substitute a canned Hebrew reply.
2. **`_strip_filler`** — pops trailing lines matching `_FILLER_PATTERNS`, plus
   blank lines, from the end only. Earlier lines are never touched, so real
   content that resembles filler survives.
3. **`_strip_markdown_links`** — `[label](url)` → `url`; `**url**` → `url`;
   `**email**` → `email`. WhatsApp renders no Markdown.
4. **`_enforce_booking_promise`** — if the reply matches
   `_BOOKING_PROMISE_RE` and `funnel_stage != handed_off`: when a slot exists,
   push CRM level 1 and set `handed_off`; when no slot exists, log and do
   nothing (never push a fabricated slot).

Order matters: the Hebrew retry runs inside the `use_context` block because the
retry may itself call tools. Steps 2–4 run after it, outside.

When you change a prompt rule that one of these mirrors, change both.

---

## 7. Transaction boundaries

`handle_message` uses two separate `session_scope()` blocks.

**Transaction 1 — durability.**
Load or create the lead; apply a session reset if the lead is idle ≥ 7 days;
clear a stale `not_relevant` flag; count prior user messages to decide the CRM
level; persist the inbound message. Committed before the agent runs, so an
agent crash still leaves the lead's message recorded.

Between the two transactions, the CRM level push runs in its own short-lived
session, wrapped in `try/except`. A CRM failure never blocks the reply.

**Transaction 2 — the turn.**
Re-fetch the lead by id (it may have vanished; that is checked), build the
prompt, invoke the agent, post-process, persist the reply.

The `Lead` instance is **not** carried across the boundary — only `lead_id` is.

Because tools write through `ctx.session`, all tool side effects commit or roll
back together with the assistant message at the end of transaction 2. External
effects already sent (a WhatsApp video, a CRM push) do not roll back.

### Metadata writes

Use `repository.update_lead_metadata(session, lead, **fields)` — it merges and
drops `None`/`""`. Direct assignment to `lead.lead_metadata` overwrites the
whole blob and can clobber keys another thread just wrote.

Two places assign directly on purpose, because a key must be *removed* and the
merge helper cannot express deletion: `cancel_call` (pops
`preferred_call_slot`) and `mark_not_relevant` (sets the flag after a reset).

---

## 8. Caching

| Cached | Function | Invalidated by |
|---|---|---|
| settings | `config.get_settings` | restart |
| chat model | `graph._model` | restart |
| agent | `graph._agent`, `graph._sim_agent` | restart |
| embeddings, Chroma client | `rag.store.*` | restart |
| retriever (MMR, k=5) | `rag.retriever._retriever` | restart |
| video catalog | `videos.catalog.load_catalog` | restart |

All are `@lru_cache`. Editing `.env` or `data/videos.json` requires a container
restart, not just a new request.

---

## 9. Simulator

`simulate_message` runs the same model and the same rendered system prompt
against stub tools, for the admin UI at `/admin/simulator`.

| | Production | Simulator |
|---|---|---|
| Tool set | `ALL_TOOLS` (8) | `_SIM_TOOLS` (6) |
| `search_knowledge` | real | **real** — shares the production tool |
| `classify_lead`, `send_video`, `recommend_video`, `schedule_call`, `cancel_call` | real | stubs writing to an in-memory dict |
| `mark_not_relevant`, `search_shop_products` | available | **absent** |
| State | Postgres | `_sim_sessions[session_id]` |
| `AgentContext.session` | live session | `None` |
| Post-processing | all four steps | `_strip_filler` + `_strip_markdown_links` only |
| Side effects | WhatsApp, CRM | none |

Stub tools reach their state dict through a second `ContextVar`
(`_sim_state_var`), because the shared `AgentContext` has no session to write
through.

The simulator constructs an unsaved `Lead()` so `describe_state` renders the
same block as production. Its state is in-memory and process-local: lost on
restart, not visible to `handle_message`, and no substitute for the eval
harness. Because the Hebrew retry and the booking safety net are absent, it
cannot verify those two rules.

---

## 10. Failure modes

| Symptom | Cause | Where |
|---|---|---|
| `RuntimeError: No AgentContext set` | tool called outside `use_context` | `context.py` |
| Reply is a Hebrew apology about a technical problem | agent invocation raised; canned fallback returned | `handle_message` except branch |
| Turn dropped, `last_error` on the lead | > 90 s or an exception in the caller | `whatsapp/handler.py` |
| Model re-asks for a slot already captured | banner missing from `describe_state` | `classifier.describe_state` |
| Slot silently not saved | value outside `_VALID_SLOTS` | `WARNING` log from `classify_lead` |
| Same video twice in one reply | in-turn guard bypassed | `ctx.videos_sent_this_turn` |
| Lead promised a call, sales never notified | safety net found no slot and skipped the push | `[booking-safety-net]` logs |
| Model "forgets" a fact it was told | fact never persisted by a tool | only `describe_state` fields survive the turn |
| Prompt render raises `KeyError` | `str.format` reintroduced | `render_system_prompt` |

Log prefixes: `[hebrew-safety-net]`, `[booking-safety-net]`, `[session-reset]`,
`[level-push]`, `[send_video]`, `[classify_lead]`, `[schedule_call]`,
`[simulator]`.

---

## 11. Adding a tool

1. Define it in `app/agent/tools.py` with `@tool` and a docstring — **the
   docstring is the model's only specification**, so argument constraints
   belong there verbatim.
2. Take `ctx = current_context()`; never add a lead/session parameter.
3. Validate every free-text argument against a whitelist; discard invalid
   values and report the rejection in the return string.
4. Catch all exceptions; return a string.
5. Append to `ALL_TOOLS`.
6. If it has external side effects, add a stub to `_SIM_TOOLS` in `graph.py` —
   otherwise the simulator will fire the real one.
7. Extend `scripts/eval_rejects.py` to cover it.
