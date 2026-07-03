# Propeller Drones – WhatsApp Lead-Conversion Bot

A conversational WhatsApp agent that warms up leads coming from paid ads for
**Propeller Drones** (drone sales + commercial-pilot academy).

The bot has free-flowing Hebrew conversations, classifies the lead's
familiarity with the field, pulls facts from a RAG knowledge base built on the
company's website, sends pre-produced videos at the right moments, and hands
warm leads off to a human sales rep.

## Stack

| Layer | Technology |
|-------|------------|
| WhatsApp | [GreenAPI](https://green-api.com/) via `whatsapp-chatbot-python` (HTTP long-polling) |
| Agent    | LangChain tool-calling agent, OpenAI `gpt-4o` |
| RAG      | LangChain + ChromaDB, `text-embedding-3-small` |
| State    | PostgreSQL 16 (leads, messages, funnel stage) |
| Runtime  | Docker Compose on EC2 |

## Architecture

```
WhatsApp lead
     │  message
     ▼
GreenAPI  ── long-poll ──► whatsapp-chatbot-python (in bot container)
                                    │
                                    ▼
                             LangChain agent
                             ┌────────┴────────┐
                             │                 │
                             ▼                 ▼
                        Chroma RAG        PostgreSQL
                       (website + docs)   (lead state)
                             │
                             ▼
                         Video catalog
                       (data/videos.json)
```

## Project layout

```
propeller_drones_leads_bot/
├── docker-compose.yml          # bot + postgres + chroma
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── .env.example
├── app/
│   ├── main.py                 # entry point (migrations + polling loop)
│   ├── config.py               # env-driven Settings
│   ├── whatsapp/               # GreenAPI handler + sender
│   ├── agent/                  # LangChain agent, prompts, tools, context
│   ├── rag/                    # ingestion + retrieval
│   ├── videos/                 # video catalog loader
│   └── db/                     # models, session, repository, migrations
├── data/
│   ├── knowledge/              # drop PDFs/txt here for extra RAG content
│   └── videos.json             # video catalog (edit to add your videos)
└── scripts/
    └── ingest_knowledge.py     # build the RAG index
```

## Setup

### 1. Get a GreenAPI instance
1. Sign up at [console.green-api.com](https://console.green-api.com/) (free "Developer" tier is fine to start).
2. Create an instance and note the **`idInstance`** and **`apiTokenInstance`**.
3. Scan the QR code from the instance settings with the WhatsApp app on the phone that will act as the bot.
4. In the instance settings enable:
   - `incomingWebhook: yes`
   - `outgoingMessageWebhook: no`
   - `outgoingAPIMessageWebhook: no`

The `whatsapp-chatbot-python` library also sets these automatically on start.

### 2. Configure environment
```bash
cp .env.example .env
# then edit .env and fill in:
#   OPENAI_API_KEY
#   GREEN_API_INSTANCE_ID
#   GREEN_API_TOKEN
#   ALLOWED_TEST_PHONES     (comma-separated, digits only; leave empty in prod)
```

### 3. Add knowledge
- The bot scrapes a curated set of Propeller Drones pages automatically (see `app/rag/ingest.py`).
- Drop any extra PDFs, `.txt`, or `.md` files into `data/knowledge/` — they'll be picked up too.

### 4. Update the video catalog
Edit `data/videos.json` to point `url` fields at the real hosted videos
(publicly reachable URLs — GreenAPI's `sendFileByUrl` will fetch them).

## Running on EC2 with Docker

```bash
docker compose build
docker compose up -d postgres chroma

# One-time: build the RAG index (also re-run after adding docs)
docker compose run --rm bot python -m scripts.ingest_knowledge --reset

# Start the bot
docker compose up -d bot
docker compose logs -f bot
```

The `bot` service will:
1. Wait for Postgres to be healthy.
2. Run Alembic migrations to create `leads` + `messages` tables.
3. Start the GreenAPI long-poll loop.

### Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `OPENAI_API_KEY` | OpenAI credentials | — (required) |
| `OPENAI_CHAT_MODEL` | Chat model | `gpt-4o` |
| `OPENAI_EMBEDDING_MODEL` | Embeddings | `text-embedding-3-small` |
| `GREEN_API_INSTANCE_ID` | GreenAPI instance id | — (required) |
| `GREEN_API_TOKEN` | GreenAPI token | — (required) |
| `DATABASE_URL` | SQLAlchemy URL | Postgres in compose |
| `CHROMA_HOST` / `CHROMA_PORT` | Chroma server | `chroma:8000` |
| `CHROMA_COLLECTION` | Collection name | `propeller_knowledge` |
| `PROPELLER_WEBSITE_BASE` | Root URL for scraper | `https://propeller-drones.com` |
| `ALLOWED_TEST_PHONES` | CSV allow-list (empty = all) | empty |
| `LOG_LEVEL` | Loguru level | `INFO` |

## How the conversation flow works

1. A lead sends a first WhatsApp message.
2. `app.whatsapp.handler` extracts the text, creates/loads a `Lead` row, and calls `app.agent.graph.handle_message`.
3. The agent runs with:
   - a Hebrew system prompt that encodes the 4-stage funnel (classify → convince → position → hand off),
   - the last 30 messages of history,
   - four tools: `search_knowledge`, `classify_lead`, `send_video` / `recommend_video`, `schedule_call`.
4. The LLM calls tools as needed, then produces a Hebrew reply.
5. The reply is sent via GreenAPI and stored in Postgres.

Familiarity levels (`unknown` → `beginner` / `aware` / `experienced`) and funnel
stages (`new` → `engaged` → `warm` → `ready_for_call` → `handed_off`) are
persisted in the `leads` table and re-loaded on every turn so the LLM never
forgets where a lead is.

## Local development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Point .env at localhost:
#   DATABASE_URL=postgresql+psycopg2://propeller:propeller@localhost:5432/propeller_bot
#   CHROMA_HOST=localhost

# Start only the infrastructure containers
docker compose up -d postgres chroma

# Migrate + ingest + run
alembic upgrade head
python -m scripts.ingest_knowledge --reset
python -m app.main
```

## Extending

- **Add a new video**: append an entry to `data/videos.json`. No code changes needed — the agent sees it automatically via the system prompt.
- **Add knowledge**: drop a file in `data/knowledge/`, then `python -m scripts.ingest_knowledge` (add `--reset` to rebuild from scratch).
- **Change sales tone**: edit `SYSTEM_PROMPT_TEMPLATE` in [app/agent/prompts.py](app/agent/prompts.py).
- **Add a new tool**: define it in [app/agent/tools.py](app/agent/tools.py) with `@tool` and append it to `ALL_TOOLS`.
