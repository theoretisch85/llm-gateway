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

alter table chat_messages
  add column if not exists prompt_tokens integer,
  add column if not exists completion_tokens integer,
  add column if not exists total_tokens integer,
  add column if not exists tokens_per_second double precision;

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

create table if not exists document_assets (
  id uuid primary key default gen_random_uuid(),
  storage_location_id text not null,
  storage_location_name text not null,
  title text,
  file_name text not null,
  media_type text,
  size_bytes bigint not null,
  relative_path text not null,
  extracted_text text,
  text_excerpt text,
  tags text,
  created_at timestamptz not null default now()
);

create table if not exists home_assistant_entity_notes (
  entity_id text primary key,
  note text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists home_assistant_aliases (
  alias text primary key,
  domain text not null,
  entity_ids jsonb not null,
  learned_from text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_chat_messages_session_created
  on chat_messages(session_id, created_at);

create index if not exists idx_memory_summaries_session_created
  on memory_summaries(session_id, created_at);

create index if not exists idx_document_assets_created
  on document_assets(created_at desc);

create index if not exists idx_home_assistant_entity_notes_updated
  on home_assistant_entity_notes(updated_at desc);

create index if not exists idx_home_assistant_aliases_updated
  on home_assistant_aliases(updated_at desc);
