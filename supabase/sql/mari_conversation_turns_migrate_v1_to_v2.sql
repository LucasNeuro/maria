-- Migração v1 → v2 (preserva linhas).
-- v1 “completa”: user_message, assistant_reply, user_external_id (opcional).
-- v1 “mínima”: só session_id (ex. wa:5511999999999) + textos — sem user_external_id.
-- Depois de correr, alinha o código da Mari (versão que grava jsonb).

begin;

alter table public.mari_conversation_turns add column if not exists phone_e164 text;
alter table public.mari_conversation_turns add column if not exists tag_name text;
alter table public.mari_conversation_turns add column if not exists user_payload jsonb;
alter table public.mari_conversation_turns add column if not exists assistant_payload jsonb;
alter table public.mari_conversation_turns add column if not exists webhook_payload jsonb;
alter table public.mari_conversation_turns add column if not exists uazapi_chat_details jsonb;

-- phone_e164: só usa user_external_id se a coluna existir na tabela atual
do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'mari_conversation_turns'
      and column_name = 'user_external_id'
  ) then
    update public.mari_conversation_turns t
    set phone_e164 = nullif(regexp_replace(coalesce(t.user_external_id, ''), '[^0-9]', '', 'g'), '')
    where t.phone_e164 is null or btrim(t.phone_e164) = '';
  end if;
end $$;

-- Fallback: dígitos a partir de session_id (ex. wa:5511999999999)
update public.mari_conversation_turns
set phone_e164 = nullif(
  regexp_replace(replace(coalesce(session_id, ''), 'wa:', ''), '[^0-9]', '', 'g'),
  ''
)
where phone_e164 is null or btrim(phone_e164) = '';

-- Último fallback: qualquer dígito em session_id
update public.mari_conversation_turns
set phone_e164 = nullif(regexp_replace(coalesce(session_id, ''), '[^0-9]', '', 'g'), '')
where phone_e164 is null or btrim(phone_e164) = '';

-- user_payload a partir de user_message (só se a coluna existir)
do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'mari_conversation_turns'
      and column_name = 'user_message'
  ) then
    update public.mari_conversation_turns t
    set user_payload = jsonb_build_object('text', coalesce(t.user_message, ''), 'legacy', true)
    where t.user_payload is null and t.user_message is not null;
  end if;
end $$;

-- assistant_payload a partir de assistant_reply (só se a coluna existir)
do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'mari_conversation_turns'
      and column_name = 'assistant_reply'
  ) then
    update public.mari_conversation_turns t
    set assistant_payload = jsonb_build_object('text', coalesce(t.assistant_reply, ''), 'legacy', true)
    where t.assistant_payload is null and t.assistant_reply is not null;
  end if;
end $$;

-- Linhas antigas sem texto migrável: placeholders para satisfazer NOT NULL
update public.mari_conversation_turns
set user_payload = coalesce(user_payload, '{"text":"","legacy":true}'::jsonb)
where user_payload is null;

update public.mari_conversation_turns
set assistant_payload = coalesce(assistant_payload, '{"text":"","legacy":true}'::jsonb)
where assistant_payload is null;

alter table public.mari_conversation_turns drop column if exists user_message;
alter table public.mari_conversation_turns drop column if exists assistant_reply;
alter table public.mari_conversation_turns drop column if exists user_external_id;

do $$
begin
  if exists (
    select 1 from public.mari_conversation_turns
    where phone_e164 is null or btrim(phone_e164) = ''
  ) then
    raise exception
      'migração mari_conversation_turns: phone_e164 vazio após derivar de session_id; corrige session_id (dígitos) ou preenche phone_e164 manualmente antes do NOT NULL';
  end if;
end $$;

alter table public.mari_conversation_turns alter column user_payload set not null;
alter table public.mari_conversation_turns alter column assistant_payload set not null;
alter table public.mari_conversation_turns alter column phone_e164 set not null;

create index if not exists mari_turns_phone_created_idx on public.mari_conversation_turns (phone_e164, created_at desc);
create index if not exists mari_turns_session_idx on public.mari_conversation_turns (session_id, created_at desc);
create index if not exists mari_turns_tag_idx on public.mari_conversation_turns (tag_name) where tag_name is not null;

commit;
