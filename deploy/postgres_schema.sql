create extension if not exists pgcrypto;

create table if not exists chat_sessions (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  selected_mode text not null default 'auto',
  resolved_model text,
  route_reason text,
  rolling_summary text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists chat_messages (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references chat_sessions(id) on delete cascade,
  role text not null,
  content text not null,
  model_used text,
  token_estimate integer,
  created_at timestamptz not null default now()
);

create table if not exists memory_summaries (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references chat_sessions(id) on delete cascade,
  summary_kind text not null default 'rolling',
  content text not null,
  source_message_count integer not null,
  created_at timestamptz not null default now()
);

create table if not exists routing_events (
  id uuid primary key default gen_random_uuid(),
  session_id uuid references chat_sessions(id) on delete set null,
  requested_mode text not null,
  resolved_model text not null,
  reason text not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_chat_messages_session_created
  on chat_messages(session_id, created_at);

create index if not exists idx_memory_summaries_session_created
  on memory_summaries(session_id, created_at);
