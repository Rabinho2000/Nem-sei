# Sigenergy API

A integração Sigenergy suporta descoberta, estado atual e leitura de histórico
diário. O acesso foi confirmado em modo read-only para a instalação Expertcom.
Alarmes, inversores, strings, disponibilidade e controlo remoto continuam fora
do âmbito.

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
| Histórico diário | `/openapi/systems/{system_id}/history` | `get_system_history` | Envia `level` e `date` na query string; nunca usa JSON body no GET. |
| Scheduler | `integration-state-sigenergy-hourly` | sync horário | Só agenda estado/energyFlow. |

## Validação e tolerância de payload

- Payload Sigenergy tem de ser objeto JSON.
- `code` ausente, `0` ou `"0"` é tratado como sucesso.
- `data` pode vir como objeto ou como string JSON; ambos continuam suportados.
- Campos em falta no `energyFlow` resultam em `None`, não em exceção.
- Estado desconhecido é apresentado como `Sem dados`.
- O histórico diário com `code: 0` é autorizado. O antigo HTML 403 do
  CloudFront era causado pelo envio incorreto de `level` e `date` num JSON body
  de um pedido GET, não por falta da permissão `System history`.

## Contrato energético histórico

Os nomes confirmados pela API identificam energia em kWh. Cada contador é
persistido ou usado separadamente; não são somados campos semanticamente
sobrepostos:

| Campo Sigenergy | Destino | Significado |
| --- | --- | --- |
| `powerGenerationKwh` | `production_kwh` | Produção PV principal do período. |
| `powerUseKwh` | `consumption_kwh` | Consumo total. |
| `powerToGridKwh` | `export_kwh` | Energia exportada para a rede. |
| `powerFromGridKwh` | `grid_import_kwh` | Energia importada da rede. |
| `powerSelfConsumptionKwh` | `self_consumption_kwh` | Produção autoconsumida. |
| `esChargingKwh` | `battery_charging_kwh` | Energia carregada na bateria. |
| `esDischargingKwh` | `battery_discharging_kwh` | Energia descarregada da bateria. |
| `itemList` | `item_list` | Detalhe devolvido, preservado sem agregação implícita. |

`powerOneselfKwh` é preservado no payload bruto, mas não é somado nem usado
como segunda medida de autoconsumo, evitando dupla contagem.

## Token, 401 e rate limit

- HTTP `401` invalida o token e faz relogin uma vez.
- HTTP `429` gera `ApiRateLimitError` para a camada comum persistir cooldown e evitar novas chamadas até ao próximo attempt.
- HTTP `5xx` e erros de rede usam backoff curto e limitado pela camada comum.
- Secrets, tokens e Bearer headers devem ser sanitizados em mensagens de erro.

## Fora de scope atual

Não existem endpoints implementados para:

- alarmes;
- inversores;
- strings;
- availability;
- controlo remoto.

O onboarding existente na app fica como compatibilidade operacional da UI atual, mas não faz parte do client de estado Sigenergy documentado aqui.

## Readiness para fecho mensal

O fecho mensal mantém FusionSolar como fonte primária quando é a fonte mensal
válida. `energyFlow` representa estado/potência live e nunca é integrado para
inventar kWh.

Um snapshot que dependa de Sigenergy fica bloqueado quando:

- o backfill não cobre o mês completo;
- o mês não passou a validação de qualidade.

Os findings mostram a remediação operacional. A aplicação não altera permissões,
não inventa tópicos MQTT e não gera valores sintéticos. Um único dia read-only
confirma o contrato e a permissão, mas não constitui um backfill mensal.

## Backfill

Não foi executado qualquer backfill nesta alteração. A aplicação ainda não
expõe um job normal de persistência histórica Sigenergy; portanto não é seguro
fornecer um comando ad-hoc que contorne fila, cooldown e auditoria. Quando esse
job for disponibilizado, o backfill deverá ser submetido pela UI/fila normal,
limitado a um System ID e intervalo explícitos, com orçamento diário confirmado.
