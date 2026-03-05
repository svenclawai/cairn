# Cairn — Design & Implementation Document

> Cairn is a stateful research session service for LLM agents. It tracks which search queries have been executed (with semantic deduplication), which pages have been visited, and how thoroughly a research goal has been covered — so agents know what they've already explored and what to do next.

-----

## Table of Contents

1. [Overview](#overview)
1. [Architecture](#architecture)
1. [Tech Stack](#tech-stack)
1. [Database Schema](#database-schema)
1. [API Specification](#api-specification)
1. [Core Logic](#core-logic)
1. [Project Structure](#project-structure)
1. [Environment & Configuration](#environment--configuration)
1. [Setup & Running](#setup--running)
1. [Test Case: Seattle Soft Serve](#test-case-seattle-soft-serve)
1. [Implementation Notes](#implementation-notes)

-----

## Overview

### The Problem

LLM agents doing web research have no native memory of what they've already searched or visited. Without external state, agents:

- Regenerate semantically identical search queries on each call
- Revisit pages they've already processed
- Don't know when research is "complete" — they either stop too early or loop forever

### What Cairn Provides

1. Search phrase generation with exclusion memory — given a goal and prior query history, generate the next best search phrase that isn't semantically redundant
1. URL/page state ledger — track pages by canonical URL with lifecycle status: unseen → queued → visited → summarized
1. Coverage scoring — a structured breakdown of how thoroughly each dimension of the research goal has been satisfied, with a diminishing-returns signal to tell agents when to stop

### Mental Model

A session is the top-level object. It holds a goal_prompt (natural language) and an optional goal_schema (structured dimensions to cover). Everything else — queries, pages, coverage scores — belongs to a session. Sessions are the unit of work for one research task.

-----

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Calling Agent                        │
│  (recruiting bot, sales hunter, research assistant)      │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP REST
┌──────────────────────▼──────────────────────────────────┐
│                   FastAPI Service                         │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐ │
│  │  Sessions   │  │   Queries    │  │     Pages       │ │
│  │  Router     │  │   Router     │  │     Router      │ │
│  └──────┬──────┘  └──────┬───────┘  └────────┬────────┘ │
│         │                │                    │          │
│  ┌──────▼────────────────▼────────────────────▼───────┐ │
│  │              Core Services Layer                    │ │
│  │  query_generator.py │ dedup.py │ coverage.py       │ │
│  └──────────────────────┬────────────────────────────┘ │
└─────────────────────────┼───────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│                      Supabase                            │
│               PostgreSQL + pgvector                      │
│                                                          │
│  sessions │ queries (+ embeddings) │ pages │ coverage    │
└─────────────────────────────────────────────────────────┘
                          │
              ┌───────────▼───────────┐
              │     Anthropic API     │
              │  (query gen, coverage) │
              └───────────────────────┘
```

Request flow for `GET /sessions/{id}/next-query`:

1. Load session goal + goal_schema
1. Fetch all prior query embeddings for session
1. Generate candidate query via Claude
1. Embed candidate, check cosine similarity against prior queries
1. If similarity > 0.92 with any prior query, regenerate (up to 3 attempts)
1. Return accepted query (do NOT auto-log it — agent logs it after executing)

Request flow for `GET /sessions/{id}/coverage`:

1. Load session goal_schema
1. Fetch all visited/summarized pages + executed queries
1. Send to Claude with structured JSON response prompt
1. Store result in coverage_scores, return to caller

-----

## Tech Stack

|Layer          |Technology                                  |Reason                                       |
|---------------|-------------------------------------------|---------------------------------------------|
|API framework  |FastAPI (Python 3.11+)                     |Async-native, auto OpenAPI docs              |
|Database       |Supabase (PostgreSQL)                      |Managed Postgres + pgvector in one place     |
|Vector search  |pgvector (via Supabase)                    |Semantic dedup without separate vector DB    |
|Embeddings     |`text-embedding-3-small` (OpenAI)          |Fast, cheap, 1536-dim, good semantic quality |
|LLM            |Claude claude-sonnet-4-20250514 (Anthropic)|Query generation + coverage scoring          |
|HTTP client    |`httpx` (async)                            |For calling Anthropic API                    |
|DB client      |`supabase-py`                              |Official async Supabase Python client        |
|Validation     |Pydantic v2                                |Request/response models                      |
|Server         |Uvicorn                                    |ASGI server for FastAPI                      |

-----

## Database Schema

Run this SQL in the Supabase SQL editor to set up all tables and indexes.

```sql
-- Enable pgvector extension
create extension if not exists vector;

-- ─────────────────────────────────────
-- SESSIONS
-- ─────────────────────────────────────
create table sessions (
  id          uuid primary key default gen_random_uuid(),
  api_key     text not null,                    -- for multi-tenant RLS
  goal_prompt text not null,                    -- natural language research goal
  goal_schema jsonb,                            -- structured dimensions to cover (optional)
  metadata    jsonb default '{}'::jsonb,        -- caller-defined arbitrary data
  status      text not null default 'active'    -- active | completed | abandoned
    check (status in ('active', 'completed', 'abandoned')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- ─────────────────────────────────────
-- QUERIES
-- ─────────────────────────────────────
create table queries (
  id          uuid primary key default gen_random_uuid(),
  session_id  uuid not null references sessions(id) on delete cascade,
  phrase      text not null,
  embedding   vector(1536),                     -- text-embedding-3-small output
  result_count integer,                         -- how many results came back (optional)
  executed_at timestamptz not null default now()
);

create index queries_session_id_idx on queries(session_id);
create index queries_embedding_idx on queries
  using ivfflat (embedding vector_cosine_ops)
  with (lists = 100);

-- ─────────────────────────────────────
-- PAGES
-- ─────────────────────────────────────
create table pages (
  id             uuid primary key default gen_random_uuid(),
  session_id     uuid not null references sessions(id) on delete cascade,
  url            text not null,                 -- raw URL as provided
  canonical_url  text not null,                 -- normalized (tracking params stripped, resolved)
  status         text not null default 'queued'
    check (status in ('queued', 'visited', 'summarized', 'failed', 'skipped')),
  content_hash   text,                          -- SHA256 of page content (dedup across sessions)
  extracted      jsonb default '{}'::jsonb,     -- structured data pulled from page
  error          text,                          -- error message if status=failed
  queued_at      timestamptz not null default now(),
  visited_at     timestamptz,
  summarized_at  timestamptz,
  unique(session_id, canonical_url)             -- enforce one record per page per session
);

create index pages_session_id_idx on pages(session_id);
create index pages_status_idx on pages(session_id, status);

-- ─────────────────────────────────────
-- COVERAGE SCORES
-- ─────────────────────────────────────
create table coverage_scores (
  id            uuid primary key default gen_random_uuid(),
  session_id    uuid not null references sessions(id) on delete cascade,
  scores        jsonb not null,                 -- {dimension: score} + overall + marginal_gain
  queries_count integer not null,               -- snapshot: how many queries existed at scoring time
  pages_count   integer not null,               -- snapshot: how many visited pages at scoring time
  computed_at   timestamptz not null default now()
);

create index coverage_session_id_idx on coverage_scores(session_id, computed_at desc);

-- ─────────────────────────────────────
-- ROW LEVEL SECURITY
-- ─────────────────────────────────────
alter table sessions enable row level security;
alter table queries enable row level security;
alter table pages enable row level security;
alter table coverage_scores enable row level security;

-- Sessions: scoped to api_key passed as app setting
create policy "sessions_api_key" on sessions
  using (api_key = current_setting('app.api_key', true));

-- Child tables: inherit access through session ownership
create policy "queries_via_session" on queries
  using (session_id in (
    select id from sessions where api_key = current_setting('app.api_key', true)
  ));

create policy "pages_via_session" on pages
  using (session_id in (
    select id from sessions where api_key = current_setting('app.api_key', true)
  ));

create policy "coverage_via_session" on coverage_scores
  using (session_id in (
    select id from sessions where api_key = current_setting('app.api_key', true)
  ));

-- ─────────────────────────────────────
-- HELPERS
-- ─────────────────────────────────────

-- Similarity search for query dedup
create or replace function similar_queries(
  p_session_id uuid,
  p_embedding vector(1536),
  p_threshold float default 0.92,
  p_limit int default 5
)
returns table (phrase text, similarity float)
language sql stable
as $$
  select phrase, 1 - (embedding <=> p_embedding) as similarity
  from queries
  where session_id = p_session_id
    and embedding is not null
    and 1 - (embedding <=> p_embedding) >= p_threshold
  order by similarity desc
  limit p_limit;
$$;
```

-----

## API Specification

Base URL: `http://localhost:8000`
All endpoints accept and return `application/json`.
Authentication uses `X-API-Key` header.

-----

### Sessions

#### POST /sessions

Create a new research session.

Request body:
```json
{
  "goal_prompt": "Find all soft serve ice cream shops in Seattle, WA",
  "goal_schema": {
    "dimensions": [
      "shop_name",
      "neighborhood",
      "address",
      "hours",
      "signature_flavors",
      "price_range"
    ]
  },
  "metadata": {}
}
```

Response `201`:
```json
{
  "id": "uuid",
  "goal_prompt": "...",
  "goal_schema": {...},
  "status": "active",
  "created_at": "iso8601"
}
```

-----

#### GET /sessions/{session_id}

Get session details including summary stats.

Response `200`:
```json
{
  "id": "uuid",
  "goal_prompt": "...",
  "goal_schema": {...},
  "status": "active",
  "stats": {
    "queries_executed": 7,
    "pages_queued": 12,
    "pages_visited": 9,
    "pages_summarized": 6
  },
  "latest_coverage": {...},
  "created_at": "iso8601"
}
```

-----

#### PATCH /sessions/{session_id}

Update session status (e.g., mark as completed).

Request body:
```json
{
  "status": "completed"
}
```

-----

### Queries

#### GET /sessions/{session_id}/next-query

Generate the next non-redundant search phrase for this session.

Query params:
- `hint` (optional, string) — caller hint about what angle to explore next

Response `200`:
```json
{
  "phrase": "soft serve ice cream Seattle Capitol Hill",
  "reasoning": "Prior queries covered downtown and U-District. Capitol Hill not yet explored.",
  "similar_prior_queries": [],
  "attempts": 1
}
```

Response `200` (no new angles):
```json
{
  "phrase": null,
  "reasoning": "All major angles for this goal appear to be covered by prior queries.",
  "coverage_suggestion": "Consider calling /coverage to assess completeness."
}
```

-----

#### POST /sessions/{session_id}/queries

Log an executed search query. Call this after you actually run the search.

Request body:
```json
{
  "phrase": "soft serve ice cream Seattle Capitol Hill",
  "result_count": 8
}
```

Response `201`:
```json
{
  "id": "uuid",
  "phrase": "...",
  "executed_at": "iso8601"
}
```

-----

#### GET /sessions/{session_id}/queries

List all queries executed in this session.

Response `200`:
```json
{
  "queries": [
    {"id": "uuid", "phrase": "...", "result_count": 8, "executed_at": "iso8601"}
  ],
  "total": 7
}
```

-----

### Pages

#### POST /sessions/{session_id}/pages

Register one or more pages (typically from search results). Sets initial status to `queued`.

Request body:
```json
{
  "pages": [
    {"url": "https://www.yelp.com/biz/soft-swerve-seattle"},
    {"url": "https://www.softswerve.com/"},
    {"url": "https://www.google.com/maps?cid=12345"}
  ]
}
```

Response `201`:
```json
{
  "registered": 3,
  "skipped_duplicates": 1,
  "pages": [
    {"id": "uuid", "canonical_url": "...", "status": "queued"}
  ]
}
```

-----

#### PATCH /sessions/{session_id}/pages

Update the status of a page (by canonical or raw URL). Call this as you process pages.

Request body:
```json
{
  "url": "https://www.softswerve.com/",
  "status": "visited",
  "content_hash": "sha256hex",
  "extracted": {
    "shop_name": "Soft Swerve",
    "neighborhood": "Capitol Hill",
    "address": "123 Pine St",
    "hours": "12pm–10pm daily",
    "signature_flavors": ["ube", "matcha", "black sesame"],
    "price_range": "$5–$8"
  }
}
```

Response `200`:
```json
{
  "id": "uuid",
  "canonical_url": "...",
  "status": "visited",
  "visited_at": "iso8601"
}
```

-----

#### GET /sessions/{session_id}/pages

List pages in this session, optionally filtered by status.

Query params:
- `status` (optional) — filter by `queued`, `visited`, `summarized`, `failed`, `skipped`

Response `200`:
```json
{
  "pages": [
    {
      "id": "uuid",
      "url": "...",
      "canonical_url": "...",
      "status": "visited",
      "extracted": {...},
      "visited_at": "iso8601"
    }
  ],
  "total": 9
}
```

-----

### Coverage

#### GET /sessions/{session_id}/coverage

Compute (or retrieve cached) coverage score for this session.

Query params:
- `recompute` (optional, bool, default `false`) — force recompute even if recent score exists

Response `200`:
```json
{
  "session_id": "uuid",
  "computed_at": "iso8601",
  "overall_score": 0.74,
  "dimensions": {
    "shop_name": {"score": 0.95, "note": "14 shops identified"},
    "neighborhood": {"score": 0.90, "note": "Most neighborhoods covered; SoDo and White Center thin"},
    "address": {"score": 0.80, "note": "11 of 14 shops have verified addresses"},
    "hours": {"score": 0.60, "note": "Hours missing for 5 shops"},
    "signature_flavors": {"score": 0.75, "note": "Good coverage for established shops, sparse for newer ones"},
    "price_range": {"score": 0.55, "note": "Price data found for 8 shops only"}
  },
  "gaps": [
    "Hours and pricing data thin — try searching '[shop name] hours menu prices'",
    "White Center and SoDo neighborhoods not yet explored"
  ],
  "marginal_gain": 0.08,
  "recommendation": "continue",
  "queries_snapshot": 7,
  "pages_snapshot": 9
}
```

`recommendation` is one of: `continue` | `diminishing` | `stop`

`marginal_gain` is the delta between this coverage score and the previous one. When it drops below 0.05, the service recommends `diminishing`. Below 0.02, it recommends `stop`.

-----

## Core Logic

### 1. URL Canonicalization

Before storing any page URL, normalize it:

```python
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "mc_cid", "mc_eid"
}

def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip().lower())
    # Strip tracking params
    qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in STRIP_PARAMS}
    clean = parsed._replace(
        scheme=parsed.scheme,
        netloc=parsed.netloc.lstrip("www."),
        query=urlencode(qs, doseq=True),
        fragment=""
    )
    return urlunparse(clean)
```

### 2. Semantic Deduplication

When `GET /next-query` is called:

```python
async def is_semantically_duplicate(
    session_id: str,
    candidate_phrase: str,
    threshold: float = 0.92
) -> tuple[bool, list[dict]]:
    embedding = await embed(candidate_phrase)
    similar = await db.rpc("similar_queries", {
        "p_session_id": session_id,
        "p_embedding": embedding,
        "p_threshold": threshold,
        "p_limit": 5
    })
    return len(similar) > 0, similar
```

Retry logic in the router: attempt up to 3 candidate generations. On each rejection, pass the similar queries back to Claude as context so it can steer away.

### 3. Query Generation Prompt

```
You are a research query generator for an autonomous agent.

Goal: {goal_prompt}
Goal schema (dimensions to cover): {goal_schema}

Queries already executed (do not repeat or semantically duplicate these):
{prior_queries_list}

Rejected candidates this round (too similar to prior queries):
{rejected_candidates}

Generate ONE search query that:
1. Has not been covered by prior queries
2. Directly advances coverage of the goal
3. Is specific enough to return useful results
4. Is 3-8 words

Respond with JSON only:
{
  "phrase": "your search phrase here",
  "reasoning": "one sentence explaining what gap this covers",
  "target_dimensions": ["dimension1", "dimension2"]
}
```

### 4. Coverage Scoring Prompt

```
You are evaluating research coverage for an autonomous agent.

Goal: {goal_prompt}
Goal schema dimensions: {goal_schema_dimensions}

Queries executed ({queries_count} total):
{queries_list}

Pages visited ({pages_count} total) with extracted data:
{pages_summary}

Score how completely each dimension has been covered on a 0.0–1.0 scale.
Be conservative — only give high scores when data is actually present and reliable.

Respond with JSON only:
{
  "overall_score": 0.0,
  "dimensions": {
    "dimension_name": {
      "score": 0.0,
      "note": "brief observation"
    }
  },
  "gaps": ["gap description 1", "gap description 2"],
  "recommendation": "continue | diminishing | stop"
}
```

### 5. Marginal Gain Calculation

```python
def compute_marginal_gain(session_id: str, new_score: float) -> float:
    previous = db.get_previous_coverage_score(session_id)
    if previous is None:
        return new_score  # first score, full gain
    return new_score - previous["overall_score"]
```

-----

## Project Structure

```
cairn/
├── main.py                    # FastAPI app entry point
├── config.py                  # Settings via pydantic-settings
├── database.py                # Supabase client init
│
├── routers/
│   ├── sessions.py            # /sessions routes
│   ├── queries.py             # /sessions/{id}/queries routes
│   ├── pages.py               # /sessions/{id}/pages routes
│   └── coverage.py            # /sessions/{id}/coverage routes
│
├── services/
│   ├── query_generator.py     # Claude-powered next query generation
│   ├── dedup.py               # Semantic similarity check via pgvector
│   ├── coverage.py            # Coverage scoring via Claude
│   ├── embeddings.py          # OpenAI embedding wrapper
│   └── url_utils.py           # URL canonicalization
│
├── models/
│   ├── session.py             # Pydantic models for sessions
│   ├── query.py               # Pydantic models for queries
│   ├── page.py                # Pydantic models for pages
│   └── coverage.py            # Pydantic models for coverage
│
├── schema.sql                 # Full DB schema (run in Supabase)
├── requirements.txt
├── .env.example
└── test_seattle_softswerve.py # Test case (see below)
```

-----

## Environment & Configuration

`.env.example`:
```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key       # service role bypasses RLS for internal calls
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...                            # for embeddings only
CAIRN_API_KEY=your-secret-api-key                # sent as X-API-Key header by callers
SIMILARITY_THRESHOLD=0.92                        # cosine similarity cutoff for dedup
MAX_DEDUP_ATTEMPTS=3                             # max retries before returning null phrase
COVERAGE_CACHE_MINUTES=5                         # don't recompute coverage more than once per N minutes
```

`config.py`:
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    supabase_url: str
    supabase_service_key: str
    anthropic_api_key: str
    openai_api_key: str
    cairn_api_key: str
    similarity_threshold: float = 0.92
    max_dedup_attempts: int = 3
    coverage_cache_minutes: int = 5

    class Config:
        env_file = ".env"

settings = Settings()
```

-----

## Setup & Running

```bash
# Install dependencies
pip install fastapi uvicorn supabase openai anthropic pydantic-settings httpx python-dotenv

# Copy and fill env
cp .env.example .env

# Run schema in Supabase SQL editor (schema.sql)

# Start server
uvicorn main:app --reload --port 8000

# View auto-generated API docs
open http://localhost:8000/docs
```

-----

## Test Case: Seattle Soft Serve

This test demonstrates an iterative exhaustive research session. It simulates an agent calling Cairn in a loop until coverage recommendation is `stop`.

File: `test_seattle_softswerve.py`

```python
"""
Cairn Test Case: Exhaustive Seattle soft serve ice cream research session.

This script simulates an agent using Cairn to iteratively discover all
soft serve shops in Seattle. It does NOT actually call search engines —
it mocks search results to test Cairn's session management, dedup,
page tracking, and coverage scoring logic end-to-end.

Run with: python test_seattle_softswerve.py
"""

import httpx
import json
import hashlib
import asyncio
from typing import Optional

BASE_URL = "http://localhost:8000"
API_KEY = "your-cairn-api-key"
HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}

# ─────────────────────────────────────────────────────────
# Mock search results — simulates what a real search engine
# would return for various queries about Seattle soft serve
# ─────────────────────────────────────────────────────────
MOCK_SEARCH_RESULTS = {
    "soft serve ice cream Seattle": [
        {"url": "https://www.softswerve.com/", "title": "Soft Swerve Seattle"},
        {"url": "https://www.yelp.com/biz/soft-swerve-seattle-2"},
        {"url": "https://mollymoon.com/locations", "title": "Molly Moon's"},
    ],
    "soft serve Seattle Capitol Hill": [
        {"url": "https://www.magicanncreamery.com/", "title": "Magic Ann Creamery"},
        {"url": "https://saltandstraw.com/pages/seattle", "title": "Salt & Straw Seattle"},
    ],
    "soft serve Seattle Ballard": [
        {"url": "https://www.twoscoopsballard.com/", "title": "Two Scoops Ballard"},
        {"url": "https://yelp.com/biz/soft-serve-ballard-seattle"},
    ],
    "soft serve Seattle Fremont": [
        {"url": "https://www.fremontcreamery.com/", "title": "Fremont Creamery"},
    ],
    "soft serve ice cream Seattle hours menu": [
        {"url": "https://www.softswerve.com/menu"},
        {"url": "https://www.magicanncreamery.com/hours"},
    ],
    "soft serve Seattle neighborhoods": [
        {"url": "https://www.timeout.com/seattle/restaurants/best-ice-cream-seattle"},
        {"url": "https://seattle.eater.com/maps/best-ice-cream-soft-serve-seattle"},
    ],
    "soft serve White Center SoDo Seattle": [
        {"url": "https://www.yelp.com/search?find_desc=soft+serve&find_loc=White+Center+Seattle"},
    ],
}

# ─────────────────────────────────────────────────────────
# Mock page extraction — simulates what an agent would
# extract from visiting each page
# ─────────────────────────────────────────────────────────
MOCK_EXTRACTED = {
    "https://www.softswerve.com/": {
        "shop_name": "Soft Swerve",
        "neighborhood": "Capitol Hill",
        "address": "217 Pine St, Seattle WA 98101",
        "hours": "Mon–Sun 12pm–10pm",
        "signature_flavors": ["ube", "matcha", "taro"],
        "price_range": "$5–$9"
    },
    "https://mollymoon.com/locations": {
        "shop_name": "Molly Moon's",
        "neighborhood": "Multiple (Capitol Hill, Fremont, Wallingford)",
        "address": "Various",
        "hours": "12pm–11pm daily",
        "signature_flavors": ["salted caramel", "honey lavender"],
        "price_range": "$4–$7"
    },
    "https://www.magicanncreamery.com/": {
        "shop_name": "Magic Ann Creamery",
        "neighborhood": "Capitol Hill",
        "address": "405 15th Ave E",
        "hours": "2pm–10pm Tue–Sun",
        "signature_flavors": ["black sesame", "pandan"],
        "price_range": "$6–$8"
    },
    "https://saltandstraw.com/pages/seattle": {
        "shop_name": "Salt & Straw",
        "neighborhood": "Capitol Hill",
        "address": "714 E Pike St",
        "hours": "11am–11pm daily",
        "signature_flavors": ["sea salt with caramel ribbons", "seasonal specials"],
        "price_range": "$6–$10"
    },
    "https://www.twoscoopsballard.com/": {
        "shop_name": "Two Scoops",
        "neighborhood": "Ballard",
        "address": "5214 Ballard Ave NW",
        "hours": "1pm–9pm Wed–Mon",
        "signature_flavors": ["classic vanilla twist", "chocolate hazelnut"],
        "price_range": "$4–$6"
    },
    "https://www.fremontcreamery.com/": {
        "shop_name": "Fremont Creamery",
        "neighborhood": "Fremont",
        "address": "3512 Fremont Ave N",
        "hours": "12pm–9pm daily",
        "signature_flavors": ["vanilla bean", "strawberry basil"],
        "price_range": "$5–$7"
    },
    "https://www.timeout.com/seattle/restaurants/best-ice-cream-seattle": {
        "shop_name": None,  # editorial list page
        "mentions": ["Soft Swerve", "Molly Moon's", "Husky Deli", "Fainting Goat"],
        "note": "List article — extract individual shop mentions"
    },
}


async def run_test():
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
        print("\n═══════════════════════════════════════")
        print("  CAIRN TEST: Seattle Soft Serve Hunt")
        print("═══════════════════════════════════════\n")

        # ─────────────────────────────────
        # 1. Create session
        # ─────────────────────────────────
        print("▸ Creating session...")
        resp = await client.post(f"{BASE_URL}/sessions", json={
            "goal_prompt": "Find all soft serve ice cream shops in Seattle, WA. "
                          "Identify as many distinct shops as possible across all neighborhoods.",
            "goal_schema": {
                "dimensions": [
                    "shop_name",
                    "neighborhood",
                    "address",
                    "hours",
                    "signature_flavors",
                    "price_range"
                ]
            }
        })
        resp.raise_for_status()
        session = resp.json()
        session_id = session["id"]
        print(f"  Session ID: {session_id}\n")

        # ─────────────────────────────────
        # 2. Research loop
        # ─────────────────────────────────
        iteration = 0
        recommendation = "continue"

        while recommendation in ("continue", "diminishing") and iteration < 15:
            iteration += 1
            print(f"── Iteration {iteration} ─────────────────────────")

            # Get next query
            resp = await client.get(f"{BASE_URL}/sessions/{session_id}/next-query")
            resp.raise_for_status()
            next_q = resp.json()

            if next_q["phrase"] is None:
                print("  No new query angles. Stopping loop.")
                break

            phrase = next_q["phrase"]
            print(f"  Next query: \"{phrase}\"")
            print(f"  Reasoning: {next_q['reasoning']}")

            # Simulate search — find closest mock result
            mock_results = _find_mock_results(phrase)
            print(f"  Mock results: {len(mock_results)} pages found")

            # Log executed query
            resp = await client.post(
                f"{BASE_URL}/sessions/{session_id}/queries",
                json={"phrase": phrase, "result_count": len(mock_results)}
            )
            resp.raise_for_status()

            # Register pages
            if mock_results:
                resp = await client.post(
                    f"{BASE_URL}/sessions/{session_id}/pages",
                    json={"pages": [{"url": r["url"]} for r in mock_results]}
                )
                resp.raise_for_status()
                registered = resp.json()
                print(f"  Pages registered: {registered['registered']} "
                      f"(skipped {registered['skipped_duplicates']} duplicates)")

            # Visit queued pages
            resp = await client.get(
                f"{BASE_URL}/sessions/{session_id}/pages",
                params={"status": "queued"}
            )
            queued_pages = resp.json()["pages"]

            for page in queued_pages[:3]:  # visit up to 3 per iteration
                url = page["canonical_url"]
                extracted = MOCK_EXTRACTED.get(url, {})
                content_hash = hashlib.sha256(url.encode()).hexdigest()

                await client.patch(
                    f"{BASE_URL}/sessions/{session_id}/pages",
                    json={
                        "url": url,
                        "status": "visited",
                        "content_hash": content_hash,
                        "extracted": extracted
                    }
                )
                print(f"  Visited: {url}")

            # Get coverage every 2 iterations
            if iteration % 2 == 0:
                resp = await client.get(
                    f"{BASE_URL}/sessions/{session_id}/coverage",
                    params={"recompute": True}
                )
                resp.raise_for_status()
                coverage = resp.json()
                print(f"\n  📊 Coverage Score: {coverage['overall_score']:.0%}")
                for dim, data in coverage["dimensions"].items():
                    bar = "█" * int(data["score"] * 10) + "░" * (10 - int(data["score"] * 10))
                    print(f"    {dim:<20} {bar} {data['score']:.0%}  {data['note']}")
                if coverage.get("gaps"):
                    print(f"\n  Gaps: {'; '.join(coverage['gaps'][:2])}")
                print(f"\n  Marginal gain: {coverage.get('marginal_gain', 0):.3f}")
                print(f"  Recommendation: {coverage['recommendation'].upper()}")
                recommendation = coverage["recommendation"]

            print()

        # ─────────────────────────────────
        # 3. Final report
        # ─────────────────────────────────
        print("\n═══════════════════════════════════════")
        print("  FINAL RESULTS")
        print("═══════════════════════════════════════\n")

        resp = await client.get(f"{BASE_URL}/sessions/{session_id}")
        session_detail = resp.json()
        stats = session_detail["stats"]
        print(f"  Queries executed: {stats['queries_executed']}")
        print(f"  Pages visited:    {stats['pages_visited']}")
        print(f"  Iterations:       {iteration}")

        # List all visited pages with extracted shop data
        resp = await client.get(
            f"{BASE_URL}/sessions/{session_id}/pages",
            params={"status": "visited"}
        )
        pages = resp.json()["pages"]
        shops = [p["extracted"] for p in pages if p["extracted"].get("shop_name")]
        print(f"\n  Shops found: {len(shops)}\n")
        for shop in shops:
            print(f"  🍦 {shop.get('shop_name')}")
            print(f"     {shop.get('neighborhood')} — {shop.get('address')}")
            print(f"     Hours: {shop.get('hours', 'unknown')}")
            print(f"     Flavors: {', '.join(shop.get('signature_flavors', []))}")
            print(f"     Price: {shop.get('price_range', 'unknown')}\n")

        # Mark session complete
        await client.patch(
            f"{BASE_URL}/sessions/{session_id}",
            json={"status": "completed"}
        )
        print("  Session marked complete. ✓")


def _find_mock_results(phrase: str) -> list[dict]:
    """Find closest matching mock results for a given phrase."""
    phrase_lower = phrase.lower()

    # Exact match first
    if phrase_lower in MOCK_SEARCH_RESULTS:
        return MOCK_SEARCH_RESULTS[phrase_lower]

    # Partial keyword match
    for key, results in MOCK_SEARCH_RESULTS.items():
        key_words = set(key.split())
        phrase_words = set(phrase_lower.split())
        if len(key_words & phrase_words) >= 2:
            return results

    # Fallback
    return MOCK_SEARCH_RESULTS.get("soft serve ice cream Seattle", [])


if __name__ == "__main__":
    asyncio.run(run_test())
```

Expected output (abbreviated):

```
═══════════════════════════════════════
  CAIRN TEST: Seattle Soft Serve Hunt
═══════════════════════════════════════

▸ Creating session...
  Session ID: a3f2...

── Iteration 1 ─────────────────────────
  Next query: "soft serve ice cream Seattle"
  Reasoning: Initial broad query to establish baseline
  Mock results: 3 pages found
  Pages registered: 3 (skipped 0 duplicates)
  Visited: https://www.softswerve.com/
  Visited: https://mollymoon.com/locations

── Iteration 2 ─────────────────────────
  Next query: "soft serve Seattle Capitol Hill"
  ...

  📊 Coverage Score: 45%
    shop_name            ████████░░ 80%  5 shops identified
    neighborhood         ██████░░░░ 60%  Capitol Hill well covered, others thin
    address              ███████░░░ 70%  4 of 5 shops have addresses
    hours                ████░░░░░░ 40%  Hours missing for 3 shops
    signature_flavors    ██████░░░░ 60%  Good for visited shops
    price_range          ████░░░░░░ 40%  Partial pricing data

  Gaps: Ballard, Fremont, White Center not explored; Hours and pricing thin

  Marginal gain: 0.450
  Recommendation: CONTINUE

...
```

-----

## Implementation Notes

### For the coding agent

- Use `supabase-py` async client throughout. All DB calls should be `await`.
- The `similar_queries` RPC function must be called with the embedding as a Python list (pgvector handles conversion).
- Background tasks for coverage scoring should use FastAPI's `BackgroundTasks` initially. This is sufficient for v1.
- The Anthropic API calls in `query_generator.py` and `coverage.py` should use `claude-sonnet-4-20250514` with `max_tokens=1000`.
- All LLM responses should be parsed as JSON. Strip markdown code fences before parsing.
- The `X-API-Key` header value should be stored against sessions so RLS works. Pass it as a Postgres config variable using `set_config('app.api_key', api_key, true)` at the start of each request.
- URL canonicalization must happen before any INSERT into pages — never store a raw URL as the dedup key.
- The `marginal_gain` field should compare `overall_score` against the most recent prior `coverage_scores` row for the session.
- Do not auto-execute or auto-log queries — the caller is responsible for executing searches and calling `POST /queries` afterward. Cairn generates and tracks; agents act.

### Scaling path (post-MVP)

- Add Redis for per-session locks to prevent race conditions in parallel multi-agent runs
- Move coverage scoring to a background queue (Supabase Queues or SQS) once compute time matters
- Add `content_hash` deduplication across sessions (same page visited in two different sessions)
- Add webhook support so sessions can push coverage updates to callers instead of polling
