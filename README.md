# DataWise

**Business Intelligence, Locally**

Connect any database or upload a CSV  and ask questions in plain English. DataWise introspects your schema at runtime, generates SQL with a local LLM, validates it as strictly read-only, runs it, and turns the results into a plain-English answer with an auto-generated chart. No API keys, no cloud, no per-query billing. Everything runs on your machine.

---

## What this actually does

```
"What's our biggest market by revenue?"
        │
        ▼
  Schema introspection (runtime, any connected DB)
        │
        ▼
  Text-to-SQL via a local LLM (Ollama)
        │
        ▼
  Safety validation (read-only, single statement, correct aggregation)
        │
        ▼
  Execution (timeout + row-limit enforced)
        │
        ▼
  Plain-English interpretation (grounded in real computed rankings)
        │
        ▼
  Auto-generated chart (rule-based, no LLM)
```

This is **schema-agnostic by construction** — the same code runs against an e-commerce sales dataset, an HR/attrition dataset, or your own production database without touching a line of code in between. Proven by running it against multiple unrelated demo datasets during development.

---

## Core pipeline (9 stages)

| # | Stage | What it does |
|---|-------|---------------|
| 1 | **Connection layer** | SQLAlchemy-based, works against SQLite / PostgreSQL / MySQL, plus CSV → temp SQLite for businesses without a live DB |
| 2 | **Schema introspection** | Extracts tables, columns, types, PKs/FKs, row counts — cached per connection, compiled into a compact LLM-ready context |
| 3 | **Text-to-SQL** | Local LLM (Ollama, `llama3.2:3b` by default) generates SQL from the question + schema context, with a self-correction retry loop |
| 4 | **Safety layer** | Read-only enforcement, single-statement only, rejects DROP/DELETE/UPDATE/INSERT/ALTER, validates correct GROUP BY aggregation in both directions, timeout + row-limit caps |
| 5 | **Result interpretation** | Plain-English explanation of the returned data — rankings and superlative claims ("highest", "lowest") are **pre-computed in Python**, not left to the LLM, to prevent hallucinated comparisons |
| 6 | **Auto-charting** | Deterministic, rule-based chart selection (line / bar / scatter / grouped-bar) based on the shape of the result — no LLM involved |
| 7 | **Conversation memory** | Follow-up questions ("now break that down by region") resolve against prior turns, scoped per connection so context never leaks across databases |
| 8 | **Semantic layer** | Auto-generated plain-English glossary of tables/columns with business synonyms, editable by the user, safely mapped back to real column names in generated SQL |
| 9 | **Feedback loop** | Thumbs up/down on answers; confirmed question→SQL pairs are stored and reused as few-shot examples, measurably improving reliability on previously-unreliable questions |

---

## Why this is trustworthy (not just tested)

Every stage above was built, then **manually attacked with a real running Ollama instance and real data** before moving to the next — not just validated against mocked unit tests. That process surfaced and fixed real bugs a test suite alone would have missed, including:

- The model defaulting to Postgres syntax (`EXTRACT()`) on a SQLite database — fixed by making the SQL-generation prompt dialect-aware
- Silently wrong aggregates from `GROUP BY` queries that selected non-aggregated columns — SQLite allows this and returns arbitrary data instead of erroring; now explicitly rejected by the safety validator
- The model answering "total profit **by** sales channel" with `WHERE sales_channel = 'online'` instead of grouping — fixed with an explicit anti-filter prompt rule
- A case-sensitivity bug where a guessed filter value (`'online'`) silently matched zero rows against real data (`'Online'`) — fixed with case-insensitive comparison guidance
- The model stating **wrong** "highest"/"lowest" claims despite being given correct numbers — fixed by pre-computing rankings in Python and feeding them to the model as ground truth, rather than trusting the LLM to compare numbers correctly
- `NULL` results (from an aggregate over zero matching rows) being described as "a sum of zero" — now correctly distinguished from a genuine zero
- CSV dates in `M/D/YYYY` format silently breaking every date-based query, since SQLite's `strftime()` returns `NULL` (not an error) on non-ISO dates — fixed by normalizing dates to ISO-8601 at CSV load time
- A self-correction retry loop added so that when the model *does* generate invalid SQL, the rejection reason is fed back for one automatic retry before failing

This bug list isn't a confession — it's the actual argument for why the safety layer matters. Small local LLMs get things wrong in specific, recurring ways; DataWise is built assuming that, not hoping around it.

---

## Tech stack

- **Backend:** Python, FastAPI, SQLAlchemy
- **LLM:** [Ollama](https://ollama.com), running locally — `llama3.2:3b` by default (CPU-friendly), swappable for a larger or SQL-specialized model (e.g. `sqlcoder`) via config
- **Frontend:** Vanilla HTML/CSS/JS, no framework, no build step — dark/dense design system inspired by developer tools like Linear and Vercel's dashboard
- **Charts:** Plotly
- **Validation:** `sqlparse` for AST-level SQL safety checks
- **Testing:** `pytest`, 130+ automated tests, plus a documented history of live manual verification against a real LLM

---

## Running locally

### Prerequisites
- Python 3.10+
- [Ollama](https://ollama.com) installed and running (`ollama serve`)
- The model pulled: `ollama pull llama3.2:3b`

### Setup

```bash
git clone https://github.com/PrajHacks/AI-Powered-Business-Analysis.git
cd ai-business-analyst
pip install -r requirements.txt
cp .env.example .env   # adjust settings if needed
uvicorn app.main:app --reload
```

Open `http://localhost:8000` — upload a CSV or connect a database, and start asking questions.

### Running tests

```bash
pytest -q
```

---

## Deployment

Full local inference requires a persistent server with the LLM loaded in memory — this cannot run on standard serverless free tiers (Vercel, etc.), since Ollama needs a long-lived process.

**Recommended architecture** for a low-cost live deployment:
- API + frontend on a lightweight always-on host (e.g. Render)
- Ollama on a small dedicated VPS (a 2 vCPU / 4GB instance is sufficient for `llama3.2:3b` — providers like Hetzner offer this in the ~$5/month range), with a reverse proxy enforcing an API key so the inference endpoint isn't open to the public internet

**`vercel-demo/`** in this repo contains a free, fully static+serverless demo deployment: CSV upload, live schema introspection, and hand-written SQL execution/charting all run for real via Vercel serverless functions (reusing the same validated backend logic). Natural-language question answering — which requires an always-on LLM — is demonstrated via pre-recorded real examples instead, clearly labeled as such, since running that live isn't possible within a free serverless architecture. See `vercel-demo/README.md` for details.

### CPU inference performance

Response times vary with question complexity: a few seconds for simple aggregations, up to 60–120 seconds for questions combining multi-concept semantic mapping, longer conversation history, or nuanced grouping logic, on CPU-only hardware. A GPU host or a hosted inference API would eliminate this variability — this project deliberately runs CPU-only local inference to keep cost at zero and demonstrate the full pipeline without any external dependency.

---

## Known limitations

- **In-memory state**: connections, conversation history, and feedback are stored in memory, not a database — they reset on server restart. This is a demo-scale choice, not a production one.
- **Small-model ceiling**: `llama3.2:3b` occasionally produces imprecise SQL on questions that stack multiple concepts (e.g. combining a business-glossary term with a precise single-dimension grouping request). The safety layer guarantees generated SQL is never destructive or structurally invalid, but cannot guarantee semantic precision from a 3B-parameter model. The feedback loop (stage 9) is the intended mitigation — confirmed-correct SQL for a tricky question gets reused as a few-shot example, and this has been verified to measurably improve reliability on repeat.
- **No multi-tenant isolation, auth, or encrypted credential storage** — out of scope for this build, documented as a stretch goal.
- **Single-instance only** — not designed to run multiple horizontally-scaled instances against the same in-memory state.

---

## Roadmap / stretch goals

- Multi-tenant isolation with encrypted, strictly-scoped stored credentials
- Embedding-based schema retrieval for databases with 100+ tables
- Proper login/auth flow
- Persistent storage for connections, conversation history, and feedback

