-- v2 — Turnos WhatsApp com JSONB (mensagens, webhook bruto, resposta /chat/details).
-- Executar no SQL Editor do mesmo projecto que `public.leads`.
-- A API Python grava com SUPABASE_SERVICE_ROLE_KEY (bypass RLS).
--
-- Se já tinhas a v1 (colunas text user_message / assistant_reply), corre primeiro:
--   supabase/sql/mari_conversation_turns_migrate_v1_to_v2.sql
--
-- Instalação limpa (apaga dados antigos desta tabela):
drop table if exists public.mari_conversation_turns cascade;

create table public.mari_conversation_turns (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  canal text not null default 'whatsapp',
  -- Número só dígitos (ex. 5511999999999) para filtrar / futura memória por contacto
  phone_e164 text not null,
  session_id text not null,
  -- Etiqueta curta (ex. nome WhatsApp / campanha); preenchida pelo servidor quando possível
  tag_name text,
  -- Mensagem cliente e resposta Mari (JSON livre: text, anexos futuros, etc.)
  user_payload jsonb not null,
  assistant_payload jsonb not null,
  -- Corpo bruto do webhook UAZAPI (evento recebido)
  webhook_payload jsonb,
  -- Resposta de POST /chat/details (modelo Chat completo na spec UAZAPI)
  uazapi_chat_details jsonb,
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists mari_turns_phone_created_idx on public.mari_conversation_turns (phone_e164, created_at desc);
create index if not exists mari_turns_session_idx on public.mari_conversation_turns (session_id, created_at desc);
create index if not exists mari_turns_tag_idx on public.mari_conversation_turns (tag_name) where tag_name is not null;

comment on table public.mari_conversation_turns is 'Turnos cliente↔Mari: payloads JSONB + snapshot UAZAPI (/chat/details) por phone_e164.';

alter table public.mari_conversation_turns enable row level security;
