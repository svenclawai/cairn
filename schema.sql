-- Enable pgvector extension
create extension if not exists vector;

-- ─────────────────────────────────────
-- SESSIONS
-- ─────────────────────────────────────
create table sessions (
  id          uuid primary key default gen_random_uuid(),
  api_key     text not null,
  goal_prompt text not null,
  goal_schema jsonb,
  metadata    jsonb default '{}'::jsonb,
  status      text not null default 'active'
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
  embedding   vector(1536),
  result_count integer,
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
  url            text not null,
  canonical_url  text not null,
  status         text not null default 'queued'
    check (status in ('queued', 'visited', 'summarized', 'failed', 'skipped')),
  content_hash   text,
  extracted      jsonb default '{}'::jsonb,
  error          text,
  queued_at      timestamptz not null default now(),
  visited_at     timestamptz,
  summarized_at  timestamptz,
  unique(session_id, canonical_url)
);

create index pages_session_id_idx on pages(session_id);
create index pages_status_idx on pages(session_id, status);

-- ─────────────────────────────────────
-- COVERAGE SCORES
-- ─────────────────────────────────────
create table coverage_scores (
  id            uuid primary key default gen_random_uuid(),
  session_id    uuid not null references sessions(id) on delete cascade,
  scores        jsonb not null,
  queries_count integer not null,
  pages_count   integer not null,
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

create policy "sessions_api_key" on sessions
  using (api_key = current_setting('app.api_key', true));

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
