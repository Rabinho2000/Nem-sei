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

O client normal descobre os sistemas autorizados através de `/openapi/system`.
O worker isolado da preview usa, adicionalmente,
`SIGENERGY_ALLOWED_SYSTEM_IDS`, que é uma allowlist obrigatória e não um
fallback de descoberta.

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
| `powerOneselfKwh` | `self_use_kwh` | Energia verde fornecida à carga, usada no balanço dos relatórios. |
| `powerToGridKwh` | `export_kwh` | Energia exportada para a rede. |
| `powerFromGridKwh` | `grid_import_kwh` | Energia importada da rede. |
| `esChargingKwh` | `battery_charge_kwh` | Energia carregada na bateria. |
| `esDischargingKwh` | `battery_discharge_kwh` | Energia descarregada da bateria. |

Os nomes legacy sem `Kwh` continuam aceites apenas como fallback. Quando as
duas variantes existem, vence a variante `Kwh` e os valores nunca são somados.
Campos ausentes permanecem `NULL`, nunca zero.

`powerSelfConsumptionKwh` é um contador distinto do lado da produção. É
preservado, juntamente com `itemList` e todos os restantes campos originais, no
`payload_json` sanitizado, mas não é convertido numa segunda métrica de
autoconsumo nem somado a `self_use_kwh`.

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

## Persistência e backfill

A aplicação expõe o job `sigenergy_energy_sync` e a UI usa
`enqueue_sigenergy_energy_backfill` para criar, de forma idempotente, um job por
dia terminado. Cada resposta é persistida em `energy_interval_facts`,
materializada como registo diário em `production_records` e volta a
materializar o total mensal.

Na preview, o comando administrativo `sigenergy-backfill` executa o mesmo
contrato de parsing e persistência diretamente num worker one-shot. O intervalo
fica limitado a 31 dias terminados, as chamadas são sequenciais e respeitam o
intervalo mínimo de 300 segundos já usado pela política de produção Sigenergy.
O worker para novas tentativas quando recebe rate limit. Nenhum destes
mecanismos foi executado durante a alteração do código.
