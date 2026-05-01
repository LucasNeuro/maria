-- Histórico de mensagens cliente ↔ Mari (ex.: WhatsApp). Executar no SQL Editor do mesmo projecto que `public.leads`.
-- A API Python grava com SUPABASE_SERVICE_ROLE_KEY (bypass RLS).

create table if not exists public.mari_conversation_turns (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  canal text not null default 'whatsapp',
  session_id text not null,
  user_external_id text,
  user_message text not null,
  assistant_reply text not null,
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists mari_turns_session_idx on public.mari_conversation_turns (session_id, created_at desc);
create index if not exists mari_turns_created_idx on public.mari_conversation_turns (created_at desc);

comment on table public.mari_conversation_turns is 'Turnos de conversa (utilizador + resposta Mari), ex. canal WhatsApp.';

alter table public.mari_conversation_turns enable row level security;
