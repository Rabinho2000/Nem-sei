# Sigenergy API

A integração Sigenergy fica limitada ao estado atual da instalação. Não há produção histórica, alarmes, inversores, strings, disponibilidade ou controlo remoto implementados sem payloads reais/documentação validada na repo.

## Configuração suportada

As variáveis atuais continuam compatíveis:

- `SIGENERGY_ENABLED`
- `SIGENERGY_APP_KEY`
- `SIGENERGY_APP_SECRET`
- `SIGENERGY_BASE_URL`
- `SIGENERGY_AUTH_ENDPOINT`
- `SIGENERGY_SYSTEMS_ENDPOINT`
- `SIGENERGY_ENERGY_FLOW_ENDPOINT`
- `SIGENERGY_REGION`
- `SIGENERGY_SYSTEM_IDS`

`SIGENERGY_SYSTEM_IDS` é o fallback quando a API não devolve lista de sistemas ou quando se quer limitar explicitamente os sistemas monitorizados.

## Implementado

| Área | Endpoint/config | Método | Notas |
| --- | --- | --- | --- |
| Login | `SIGENERGY_AUTH_ENDPOINT` | `get_access_token` / `authenticate` | Envia App Key/App Secret codificados no payload `key`. |
| Token | Bearer | `request_json` | O token é guardado em cache até expirar. |
| Região | `SIGENERGY_REGION` | headers | Envia `sigen-region` no login e nas chamadas autenticadas. |
| Sistemas | `SIGENERGY_SYSTEMS_ENDPOINT` | `list_systems` | Aceita listas em `data.list`, `records`, `systems`, `items`, `systemList` ou `rows`. |
| Estado atual | `SIGENERGY_ENERGY_FLOW_ENDPOINT` | `get_energy_flow` | Substitui `{system_id}`/`{systemId}` e lê `energyFlow` atual. |
| Scheduler | `integration-state-sigenergy-hourly` | sync horário | Só agenda estado/energyFlow. |

## Validação e tolerância de payload

- Payload Sigenergy tem de ser objeto JSON.
- `code` ausente, `0` ou `"0"` é tratado como sucesso.
- `data` pode vir como objeto ou como string JSON; ambos continuam suportados.
- Campos em falta no `energyFlow` resultam em `None`, não em exceção.
- Estado desconhecido é apresentado como `Sem dados`.

## Token, 401 e rate limit

- HTTP `401` invalida o token e faz relogin uma vez.
- HTTP `429` gera `ApiRateLimitError` para a camada comum persistir cooldown e evitar novas chamadas até ao próximo attempt.
- HTTP `5xx` e erros de rede usam backoff curto e limitado pela camada comum.
- Secrets, tokens e Bearer headers devem ser sanitizados em mensagens de erro.

## Fora de scope atual

Não existem endpoints implementados para:

- produção histórica;
- alarmes;
- inversores;
- strings;
- availability;
- controlo remoto.

O onboarding existente na app fica como compatibilidade operacional da UI atual, mas não faz parte do client de estado Sigenergy documentado aqui.
