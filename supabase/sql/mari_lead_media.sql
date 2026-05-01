-- Metadados de ficheiros guardados no Supabase Storage (fotos/vídeos de leads).
-- 1) Criar bucket no Dashboard → Storage → New bucket (nome recomendado: maria-lead-media).
--    Ou usar MARIA_STORAGE_BUCKET no env da Mari com o mesmo nome.
-- 2) Políticas: o backend usa SERVICE_ROLE (bypass RLS nas tabelas). Para leitura pública ao CRM,
--    configurar bucket público ou signed URLs na app.

create table if not exists public.mari_lead_media (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  phone_e164 text,
  lead_id uuid references public.leads (id) on delete set null,
  tipo_lead text not null check (tipo_lead in ('cliente_final', 'proprietario', 'parceiro')),
  storage_bucket text not null default 'maria-lead-media',
  object_path text not null,
  source_url text,
  content_type text,
  bytes_size integer,
  notas text,
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists mari_lead_media_created_at_idx on public.mari_lead_media (created_at desc);
create index if not exists mari_lead_media_phone_idx on public.mari_lead_media (phone_e164);
create index if not exists mari_lead_media_lead_id_idx on public.mari_lead_media (lead_id);

comment on table public.mari_lead_media is 'Anexos de leads (ficheiros no Storage + referência CRM).';

alter table public.mari_lead_media enable row level security;
