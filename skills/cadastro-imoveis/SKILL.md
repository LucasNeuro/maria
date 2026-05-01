---
name: Cadastro de imóveis (Supabase)
description: Fluxo completo para captar dados de imóvel (corretor/proprietário), CEP, fotos no Storage e rascunho em mari_imoveis.
---

## Quando usar

- Intenção **proprietário** ou **parceiro** a **anunciar/cadastrar** um imóvel no HUB.
- Cliente envia **fotos** (URLs HTTPS no WhatsApp) ou **CEP** para endereço.

## Pré-requisitos (equipa)

1. Correr no Supabase o script `supabase/sql/mari_imoveis.sql`.
2. Bucket de Storage configurado (`MARIA_STORAGE_BUCKET`); mesmos requisitos que `mari_lead_media`.

## Sequência recomendada

1. **`listar_imoveis_contato`** (opcional) ou usa o bloco `imoveis_cadastro` em **`contexto_lead_por_telefone`** para retomar rascunho (`id` UUID).
2. **`salvar_rascunho_imovel`** — primeiro chamado pode ser `{}` para criar só o rascunho vazio; depois atualiza com `imovel_id` + `dados_json` parcial.
3. CEP (8 dígitos): **`consultar_cep_viacep`** → incorpora `logradouro`, `bairro`, `localidade`→`cidade`, `uf` no próximo `salvar_rascunho_imovel`.
4. Fotos: se a UAZAPI só expuser mídia via **`message/download`**, o **webhook** grava automaticamente no Storage e em **`mari_lead_media`** (ver `MARIA_PERSIST_UAZAPI_DOWNLOAD_MEDIA`). Opcional: **`MARIA_ATTACH_UAZAPI_MEDIA_TO_LATEST_IMOVEL=1`** também liga ao último **`mari_imoveis`** do contacto. Para anexar por **URL HTTPS** à galeria de um `imovel_id` explícito, usa **`anexar_midia_imovel`**.
5. Ao fechar o atendimento imobiliário em paralelo com lead: **`registrar_lead_rascunho`** e, se útil, incluir `imovel_id` no `dados_json` do lead **ou** `lead_id` no `dados_json` do imóvel (UUID da linha `leads`).

## Campos a cobrir (mínimo forte)

| Tema | Campos `dados_json` |
|------|---------------------|
| Tipo | `tipo_imovel` (apartamento, casa, terreno, comercial, …) |
| Negócio | `operacao`: `venda`, `locacao`, `venda_e_locacao` |
| Estado | `condicao_imovel`: `novo`, `usado`, `na_planta`, `em_construcao` |
| Área | `metragem_total_m2`, `metragem_util_m2` |
| Dormitórios | `quartos`, `banheiros`, `vagas_garagem` |
| Endereço | `cep`, `logradouro`, `numero`, `complemento`, `bairro`, `cidade`, `uf`; ou `endereco_completo` |
| Valores | `valor_pretendido_reais`, `condominio_reais`, `iptu_reais` |
| Outros | `descricao_livre`, `mobiliado`, `aceita_permuta`, `extras` (JSON livre) |
| Workflow | `status`: `rascunho` → `pendente_validacao` quando feito para revisão humana |

## Multimodal (visão)

No **WhatsApp**, o mesmo `agent.arun` recebe imagens quando a UAZAPI entrega bytes/URLs e `MARIA_MULTIMODAL_VISION=1`. O **modelo** em `MARIA_MODEL` deve suportar **imagem** (ver `.env.example`). Descreve o que vês e confirma dados com o cliente antes de gravar.

## Áudio

Mensagem **só em voz**: o webhook ainda **não** transcreve; pede texto. Fotos recebidas como mídia (message/download) ainda podem ser gravadas automaticamente se houver pipeline de imagem ativa.
