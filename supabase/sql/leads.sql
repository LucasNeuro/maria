-- Executar no Supabase: SQL Editor → New query → Run
-- Tabela de leads da Mari (SDR) — CRM próprio até integrações futuras

create table if not exists public.leads (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  tipo_lead text not null check (tipo_lead in ('cliente_final', 'proprietario', 'parceiro')),
  nome text not null,
  telefone text not null,
  email text,
  origem text,
  imovel_interesse text,
  perguntas_resumo text,
  midias_enviadas boolean,
  pediu_visita boolean,
  urgencia boolean,
  potencial text not null,
  resumo_geral text not null,
  dados jsonb not null default '{}'::jsonb
);

create index if not exists leads_created_at_idx on public.leads (created_at desc);
create index if not exists leads_tipo_lead_idx on public.leads (tipo_lead);

comment on table public.leads is 'Leads HUB Obra 10+ registados pela Mari (AgentOS).';
