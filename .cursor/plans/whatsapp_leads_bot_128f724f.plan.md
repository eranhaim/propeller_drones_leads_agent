---
name: WhatsApp Leads Bot
overview: Build a Python-based WhatsApp lead-conversion agent for Propeller Drones that uses LangChain with RAG to warm up leads from paid ads before sales calls. The bot classifies leads by familiarity level, sends relevant videos, and guides them through a soft-sell funnel -- all running in Docker on EC2.
todos:
  - id: scaffold
    content: "Scaffold project structure: directories, requirements.txt, .env.example, docker-compose.yml, Dockerfile"
    status: completed
  - id: config
    content: Create config.py with environment variable loading and validation
    status: completed
  - id: db
    content: Set up PostgreSQL models (Lead, Message) with SQLAlchemy + Alembic migrations
    status: completed
  - id: rag-ingest
    content: "Build RAG ingestion pipeline: website scraper + document loader + Chroma storage"
    status: completed
  - id: rag-retriever
    content: Build RAG retriever with MMR search and metadata filtering
    status: completed
  - id: video-catalog
    content: Create video catalog system with JSON config and selection logic
    status: completed
  - id: agent
    content: Build LangChain agent with tools (search_knowledge, send_video, classify_lead, schedule_call) and Hebrew system prompt
    status: completed
  - id: classifier
    content: Implement lead classification logic (beginner/aware/experienced) and funnel stage tracking
    status: completed
  - id: whatsapp
    content: "Integrate GreenAPI: message handler, text sender, video sender, conversation routing"
    status: completed
  - id: main
    content: Wire everything together in main.py entry point
    status: completed
  - id: docker
    content: Create Dockerfile and docker-compose.yml with bot + postgres + chroma services
    status: completed
  - id: readme
    content: Write README with setup instructions, env var docs, and deployment guide
    status: completed
isProject: false
---

# WhatsApp Lead-Conversion Agent for Propeller Drones

## Architecture Overview

```mermaid
flowchart LR
    subgraph WhatsApp["WhatsApp"]
        Lead[Lead from Ad]
    end
    subgraph Docker["Docker Compose on EC2"]
        GreenAPI_Webhook[GreenAPI Webhook Listener]
        Agent[LangChain Agent]
        RAG[RAG Retriever]
        Chroma[ChromaDB Vector Store]
        PG[(PostgreSQL)]
        VideoSelector[Video Selector]
    end
    Lead -->|message| GreenAPI_Webhook
    GreenAPI_Webhook --> Agent
    Agent -->|query| RAG
    RAG -->|search| Chroma
    Agent -->|read/write| PG
    Agent -->|pick video| VideoSelector
    Agent -->|reply + video| GreenAPI_Webhook
    GreenAPI_Webhook -->|send| Lead
```



## Project Structure

```
propeller_drones_leads_bot/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
├── app/
│   ├── main.py                  # Entry point, GreenAPI webhook loop
│   ├── config.py                # Settings from env vars
│   ├── whatsapp/
│   │   ├── handler.py           # Incoming message router
│   │   └── sender.py            # Outgoing messages + video/file sending
│   ├── agent/
│   │   ├── graph.py             # LangChain agent with tools
│   │   ├── prompts.py           # System prompts (Hebrew)
│   │   ├── classifier.py        # Lead familiarity classifier
│   │   └── tools.py             # RAG search, video lookup, schedule call
│   ├── rag/
│   │   ├── ingest.py            # Website scraper + doc loader + chunker
│   │   ├── retriever.py         # Chroma retriever wrapper
│   │   └── store.py             # ChromaDB init and persistence
│   ├── videos/
│   │   └── catalog.py           # Video catalog with trigger conditions
│   └── db/
│       ├── models.py            # SQLAlchemy models (Lead, Conversation)
│       ├── session.py           # DB session factory
│       └── migrations/          # Alembic migrations
├── data/
│   ├── knowledge/               # Place PDFs, docs here for RAG ingestion
│   └── videos.json              # Video catalog (URL, topic, trigger stage)
├── scripts/
│   └── ingest_knowledge.py      # One-time script to build vector store
└── tests/
```

## Core Components

### 1. WhatsApp Integration (GreenAPI)

Uses the `whatsapp-chatbot-python` library. The bot runs a polling loop via `bot.run_forever()` that listens for incoming messages. Each message triggers the agent pipeline.

- **Incoming**: `@bot.router.message()` catches all text messages, routes to the LangChain agent
- **Outgoing**: `notification.answer()` for text, `notification.api.sending.sendFileByUrl()` for videos
- The bot handles one conversation per phone number, identified by `chatId` (e.g. `972501234567@c.us`)

### 2. LangChain Agent

A conversational agent built with LangChain, using the **tool-calling** pattern. The agent has access to:

- `**search_knowledge**` tool -- RAG retriever that queries the Chroma vector store for company/industry info
- `**send_video**` tool -- selects and sends a relevant video from the catalog
- `**classify_lead**` tool -- updates the lead's familiarity classification in the DB
- `**schedule_call**` tool -- marks the lead as ready for a sales call and sends a booking prompt

The agent is configured with a **system prompt in Hebrew** that encodes the sales methodology:

1. Identify the lead's familiarity level (beginner / aware / experienced) within the first 2-3 exchanges
2. Adapt tone and depth accordingly
3. First convince: drones are an attractive, lucrative profession
4. Then position: Propeller Drones is the industry leader (2,500+ graduates, biggest academy, government contracts)
5. Goal: lead wants to enter the drone world and is eager for a sales call
6. **Hard rule**: never ask more than 3 questions in a row without giving value

### 3. Lead Classification System

Three familiarity tiers stored in PostgreSQL per lead:


| Level           | Description                           | Bot Behavior                                                                       |
| --------------- | ------------------------------------- | ---------------------------------------------------------------------------------- |
| **beginner**    | Never heard of commercial drones      | Focus on the opportunity: salaries (12-20K NIS), growing market, diverse job types |
| **aware**       | Knows about drones, exploring options | Focus on Propeller's advantages: largest academy, 2,500+ graduates, gov contracts  |
| **experienced** | Has some drone experience or license  | Focus on advanced courses, career advancement, Propeller's industry connections    |


Classification happens dynamically via the LLM analyzing the conversation, not via explicit survey questions.

### 4. RAG Knowledge Base

Following the **langchain-rag** skill patterns:

- **Ingestion** ([scripts/ingest_knowledge.py](scripts/ingest_knowledge.py)):
  - `WebBaseLoader` to scrape key pages from `propeller-drones.com` (homepage, academy, courses, services, about)
  - `DirectoryLoader` + `PyPDFLoader` / `TextLoader` for documents in `data/knowledge/`
  - `RecursiveCharacterTextSplitter` with `chunk_size=1000, chunk_overlap=200`
  - Metadata tagging per chunk: `source`, `topic` (courses, services, about, faq)
- **Storage**: Chroma with `persist_directory="./chroma_db"`, using `OpenAIEmbeddings(model="text-embedding-3-small")`
- **Retrieval**: MMR search (`fetch_k=20, lambda_mult=0.5, k=5`) for diverse, relevant results

### 5. Video Catalog

A JSON-based catalog (`data/videos.json`) mapping videos to conversation triggers:

```json
[
  {
    "id": "intro_drones_career",
    "title": "Why Drones Are the Career of the Future",
    "url": "https://...",
    "trigger_stage": "beginner",
    "trigger_topics": ["career", "salary", "opportunity"],
    "description": "Overview of commercial drone career paths"
  }
]
```

The agent's `send_video` tool queries this catalog and picks the most contextually relevant video. Videos are sent via GreenAPI's `sendFileByUrl` method.

### 6. Conversation Persistence (PostgreSQL)

**Lead model**: `phone`, `name`, `familiarity_level`, `funnel_stage` (new / engaged / warm / ready_for_call), `created_at`, `last_message_at`, `metadata` (JSON)

**Message model**: `lead_id`, `role` (user/assistant), `content`, `timestamp`

Conversation history is loaded from PostgreSQL and injected into the LangChain agent as message history, so the agent has full context across sessions.

### 7. Docker Deployment

`docker-compose.yml` with three services:

- **bot**: The Python application
- **postgres**: PostgreSQL 16 for lead/conversation data
- **chroma**: ChromaDB server for vector store

## Environment Variables

```
OPENAI_API_KEY=
GREEN_API_INSTANCE_ID=
GREEN_API_TOKEN=
DATABASE_URL=postgresql://user:pass@postgres:5432/propeller_bot
CHROMA_HOST=chroma
CHROMA_PORT=8000
```

## Conversation Flow

```mermaid
stateDiagram-v2
    [*] --> NewLead: First message
    NewLead --> Classifying: Greeting + open question
    Classifying --> Beginner: Low familiarity
    Classifying --> Aware: Some familiarity
    Classifying --> Experienced: High familiarity
    Beginner --> Convincing: Send career opportunity info + video
    Aware --> Positioning: Send Propeller advantages + video
    Experienced --> Positioning: Send advanced course info
    Convincing --> Positioning: Lead shows interest
    Positioning --> ReadyForCall: Lead convinced
    ReadyForCall --> [*]: Schedule sales call
```



## Key Design Decisions

- **Polling (not webhooks)**: GreenAPI's `whatsapp-chatbot-python` uses HTTP long-polling (`receiveNotification`), which is simpler for EC2 deployment -- no need for a public HTTPS endpoint or reverse proxy.
- **Chroma over FAISS**: Chroma runs as a separate Docker service, making it easy to persist and scale independently. Follows the skill's recommendation for development/production use.
- **LangChain tool-calling agent** (not a rigid state machine): Lets the bot have natural, free-flowing Hebrew conversations while still having structured tools for RAG, video sending, and lead tracking. This satisfies requirement #1 (free conversation).
- **3-question rule** encoded in the system prompt, not in code logic -- gives the LLM flexibility to count and self-regulate.
- **Hebrew throughout**: All system prompts, knowledge base content, and responses are in Hebrew.

