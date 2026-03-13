# health-agent

Self-hosted AI agent that analyzes your health and mental state over time. Runs in Telegram, connects to your devices, remembers what matters.

> Tested on 9 years of Apple Health data in production.

## Why

Most health apps are trackers. This is an analyst. It connects your physical data with your mental state, finds patterns across both, and remembers them.

*"Every time I log anxiety in the evening, next day's HRV is lower"*
*"Is there a correlation between my insulin levels and weight?"*
*"I've been stressed at work for two weeks — is that why my VO2max dropped?"*
*"Make me a 2-week plan of food and workouts based on my lab results, fitness level and goals"*

The agent fetches the data itself. You just ask.

- Voice messages, food photos, smart scale screenshots, lab PDFs — send anything
- Patterns are verified by you before becoming long-term memory
- Everything runs on your server. Your data never leaves.

## How it works

```
Telegram / Health Devices
        ↓
    n8n (triggers & webhooks)
        ↓
  Python agent (logic)
        ↓
  PostgreSQL + pgvector (memory, metrics, dialogs)
```

### 5-layer memory architecture

| Layer | What | Storage |
|-------|------|---------|
| Session | Last 6 messages | `messages` |
| Episodic | All historical metrics | `health_metrics`, `sleep_sessions`, etc. |
| Semantic | Vector search over your history | `message_embeddings` (pgvector) |
| Knowledge | Uploaded docs & research (RAG) | `knowledge_chunks` (pgvector) |
| Synthesized | Verified long-term patterns | `memory_insights` |

**Memory Synthesizer** runs weekly: analyzes 28 days → finds patterns → accumulates confirmations (threshold: 3) → asks you to verify via `/memory` → confirmed patterns stay in memory forever.

### Tool-calling agent

14 tools the agent picks from autonomously. For ad-hoc questions, keyword matching selects 3–4 relevant tools instead of all 14 — reduces token usage ~10x.

## Device connectors

| Connector | Status | Method |
|-----------|--------|--------|
| Apple Health | ✅ Tested in production | Weekly export.xml |
| Oura Ring | 🔜 Planned | REST API + OAuth 2.0 |
| Whoop | 🔜 Planned | REST API + Webhooks |
| Garmin | 🔜 Planned | garminconnect (unofficial) |

Apple Health is the only connector tested in production. Oura, Whoop and Garmin connectors are planned but not yet implemented — if you own one of these devices and want to contribute, PRs are very welcome.

## Stack

- Python 3.12 + FastAPI + aiogram 3
- PostgreSQL 16 + pgvector
- LiteLLM (Claude, OpenAI, Gemini — swap via `.env`)
- n8n · Docker Compose

## Prerequisites

- Docker + Docker Compose
- A server with a public IP (or run locally)
- [Telegram bot token](https://t.me/BotFather) — create a bot, copy the token
- At least one LLM API key: [Anthropic](https://console.anthropic.com) or [OpenAI](https://platform.openai.com)
- Your Telegram user ID — send `/start` to [@userinfobot](https://t.me/userinfobot)

## Quick start

```bash
git clone https://github.com/Svetafo/health-agent
cd health-agent
cp .env.example .env
# Edit .env: set TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, API keys
docker compose up -d
```

Open Telegram, find your bot, send `/help`. If you see the command list — it's running.

## Apple Health setup

Health data flows via iOS Shortcuts → n8n webhook → agent.

1. Open n8n at `http://your-server:5678`, create an account
2. Import workflows from `n8n/workflows/` via **Settings → Import**
3. In your iPhone, create a Shortcut that sends Apple Health metrics as JSON to `http://your-server:5678/webhook/health`
4. Set up a daily automation to run the shortcut at 23:58

For the full field mapping see `src/health/intake.py`. Alternatively, export `export.xml` from the Health app weekly and run:

```bash
docker exec health-agent-app-1 python3 scripts/import_health_export.py /path/to/export.xml
```

## Bot commands

| Command | What it does |
|---------|-------------|
| `/ask <question>` | Ask anything — agent fetches data and answers |
| `/report` | Full analytics report across all data |
| `/scope` | Focus vector: what to act on, watch, or let go |
| `/mind` | Log a thought → layered reflection (CBT / schema / mentalization) |
| `/decision` | Decision analysis |
| `/food` | Log food via photo or text → calories, protein, fat, carbs |
| `/weight` | Log body metrics via smart scale screenshot or text measurements |
| `/sleep` | Log sleep via Apple Health screenshots or text |
| `/lab` | Upload lab results (PDF or photo) → structured storage + trend analysis |
| `/memory` | Verify pending patterns → long-term memory |
| `/plateau` | Weight plateau analysis: correlations, body recomposition |
| `/done` | Save current food or weight session |
| `/fix` | Correct last nutrition entry |

## Configuration

```env
TELEGRAM_BOT_TOKEN=
ALLOWED_USER_IDS=your_telegram_id
LLM_PROVIDER=anthropic
AGENT_MODEL=anthropic/claude-haiku-4-5-20251001
PARSING_MODEL=openai/gpt-4o-mini
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GROQ_API_KEY=           # optional — voice message transcription via Whisper
INTERNAL_API_KEY=
```

## Contributing

PRs welcome, especially:

- **Device connectors** — Oura Ring, Whoop, Garmin (see `src/health/` for the intake pattern)
- **New bot commands** — workout logging, medication tracking, custom metrics
- **Bugfixes and tests**

Please open an issue before starting a large feature.

## License

MIT
