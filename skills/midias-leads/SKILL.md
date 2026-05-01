---
name: Mídias de leads (Storage + CRM)
description: Guardar fotos/vídeos de proprietários ou corretores no Supabase Storage e ligar ao telefone/lead.
---

## Quando usar

- Fluxo **proprietário** ou **parceiro** após convite do POP a enviar fotos/vídeos.
- Cliente enviou URL de mídia (ex.: WhatsApp / UAZAPI com `fileURL` ou link HTTPS).

## Procedimento

1. Confirma que tens **URL HTTPS** acessível pelo servidor (timeout pode falhar em links internos ou expirados).
2. Define `tipo_lead`: `proprietario` ou `parceiro` (e `cliente_final` só se fizer sentido ao produto — normalmente anúncio usa corretor humano para materiais).
3. Chama **`registar_midia_url_no_lead`** com:
   - `url_midia`
   - `tipo_lead`
   - `nome_arquivo_sugerido` opcional (ex. `sala.jpg`)
   - `lead_id` opcional (UUID do registo em `public.leads`); se vazio, tenta o último lead compatível com o telefone do contexto WhatsApp.
   - `notas` opcional (ex. "fachada", "área de lazer").
4. Se `ok`, comunica ao cliente que **recebeste** e que o time vai analisar — sem inventar URLs novas.
5. Ao **registar lead** no fim do fluxo, marca `midias_enviadas=true` em **`registrar_lead_rascunho`** quando já existirem anexos guardados.

## Falhas comuns

- URL devolve 403/401 → pedir ao cliente reenviar ou enviar por escrito o essencial.
- Bucket não criado no Supabase → ver SQL `supabase/sql/mari_lead_media.sql` e criar bucket `maria-lead-media` (ou o nome em `MARIA_STORAGE_BUCKET`).

## Política

- Não expores paths internos longos ao cliente; resumo curto basta.
- Limite de tamanho é aplicado na ferramenta — não tentes contornar com URLs gigantes.
