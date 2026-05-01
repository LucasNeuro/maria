---
name: CEP e endereço (ViaCEP)
description: Normalizar endereço a partir do CEP brasileiro antes de qualificar zona ou imóvel.
---

## Quando usar

- Cliente ou corretor informa **CEP** (8 dígitos) e precisas de **logradouro, bairro, cidade, UF**.
- Complemento ao POP de proprietário/parceiro (cidade/bairro) quando só há CEP.

## Procedimento

1. Limpar o CEP para só dígitos; chama **`consultar_cep_viacep(cep)`**.
2. Se o JSON trouxer `"erro": true` ou campo inválido, diz que o CEP não foi encontrado e pede confirmação ou endereço por escrito.
3. Usa **localidade + UF** na conversa; não inventes número nem complemento que não existam na resposta.
4. Guarda no **`dados_json`** do lead campos úteis: `cep`, `logradouro`, `bairro`, `localidade`, `uf`.

## Limitações

- ViaCEP é serviço público; indisponibilidade pontual → pedir cidade/bairro manualmente.
- Não substitui geolocalização nem raio de busca — isso virá com coordenadas / PostGIS noutra ferramenta.
