-- Cadastro de imóveis (HUB Obra / Mari) — tabelas dedicadas + metadados de fotos no Storage.
-- Pré-requisito: bucket de Storage (mesmo de leads ou ex.: maria-imovel-media via MARIA_STORAGE_BUCKET).
-- Executar no Supabase: SQL Editor → Run.

-- Imóvel em rascunho ou workflow de publicação
create table if not exists public.mari_imoveis (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  phone_e164 text not null,
  lead_id uuid references public.leads (id) on delete set null,
  status text not null default 'rascunho'
    check (status in ('rascunho', 'pendente_validacao', 'publicado', 'arquivado')),
  -- Classificação
  tipo_imovel text,
  operacao text check (operacao is null or operacao in ('venda', 'locacao', 'venda_e_locacao')),
  condicao_imovel text check (
    condicao_imovel is null
    or condicao_imovel in ('novo', 'usado', 'na_planta', 'em_construcao')
  ),
  -- Áreas e composição
  metragem_total_m2 numeric,
  metragem_util_m2 numeric,
  quartos integer,
  banheiros integer,
  vagas_garagem integer,
  -- Endereço (complementar ao ViaCEP no fluxo Mari)
  endereco_completo text,
  cep text,
  logradouro text,
  numero text,
  complemento text,
  bairro text,
  cidade text,
  uf text,
  latitude double precision,
  longitude double precision,
  -- Valores (opcional no rascunho)
  valor_pretendido_reais numeric,
  condominio_reais numeric,
  iptu_reais numeric,
  descricao_livre text,
  mobiliado boolean,
  aceita_permuta boolean,
  extras jsonb not null default '{}'::jsonb,
  notas_internas text
);

create index if not exists mari_imoveis_phone_idx on public.mari_imoveis (phone_e164);
create index if not exists mari_imoveis_updated_idx on public.mari_imoveis (updated_at desc);
create index if not exists mari_imoveis_status_idx on public.mari_imoveis (status);
create index if not exists mari_imoveis_lead_id_idx on public.mari_imoveis (lead_id);

comment on table public.mari_imoveis is 'Rascunhos e registos de imóveis captados pela Mari (cadastro corretor/proprietário).';

-- Fotos/documentos do imóvel no Storage (bytes no bucket; aqui só referência)
create table if not exists public.mari_imovel_midia (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  imovel_id uuid not null references public.mari_imoveis (id) on delete cascade,
  phone_e164 text,
  storage_bucket text not null default 'maria-lead-media',
  object_path text not null,
  source_url text,
  content_type text,
  bytes_size integer,
  legenda text,
  sort_order integer not null default 0,
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists mari_imovel_midia_imovel_idx on public.mari_imovel_midia (imovel_id);
create index if not exists mari_imovel_midia_created_idx on public.mari_imovel_midia (created_at desc);

comment on table public.mari_imovel_midia is 'Anexos de imóvel no Storage (path + meta); mesmo padrão que mari_lead_media.';

-- updated_at automático
create or replace function public.mari_imoveis_set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists mari_imoveis_updated_at on public.mari_imoveis;
create trigger mari_imoveis_updated_at
  before update on public.mari_imoveis
  for each row execute procedure public.mari_imoveis_set_updated_at();

alter table public.mari_imoveis enable row level security;
alter table public.mari_imovel_midia enable row level security;
