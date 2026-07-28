# Sigenergy API — matriz de capacidades

Esta integração é read-only. Comandos remotos, controlo de bateria e alterações
de modo estão explicitamente fora de âmbito.

A matriz abaixo usa apenas endpoints já presentes e cobertos por fixtures na
repo. Não foram encontrados documentos oficiais nem payloads validados para
histórico energético, dispositivos, alarmes ou Data Subscription. Essas
capacidades não são inferidas.

| Capacidade | Endpoint / canal | Permissões / onboarding | Dados e granularidade | Limites confirmados | Utilização | Estado |
| --- | --- | --- | --- | --- | --- | --- |
| Autenticação | `POST /openapi/auth/login/key` | App Key e App Secret; região no header `sigen-region` | Token Bearer com validade indicada no payload | Não documentados na repo | Acesso à API | Confirmado por implementação e fixture |
| Descoberta de sistemas | `GET /openapi/system` | A App Key tem de estar autorizada para os sistemas | Lista de System ID, nome, estado e metadados presentes no payload | Paginação não confirmada; não implementada | Inventário e onboarding | Confirmado por implementação e fixture |
| Estado operacional atual | `GET /openapi/systems/{system_id}/energyFlow` | Acesso ao System ID descoberto | Potências instantâneas, SOC e campos opcionais do fluxo atual | Não documentados na repo | Monitorização, UI e alertas | Confirmado por implementação e fixture |
| Data Subscription — System Data | Não disponível na repo | Desconhecidas | Desconhecidos | Desconhecidos | Telemetria | Disponível mas não validado; não implementado |
| Data Subscription — Telemetry | Não disponível na repo | Desconhecidas | Desconhecidos | Desconhecidos | Telemetria e eventual energia por intervalo | Disponível mas não validado; não implementado |
| Data Subscription — Alarms | Não disponível na repo | Desconhecidas | Desconhecidos | Desconhecidos | Alarmes | Disponível mas não validado; não implementado |
| Histórico energético | Nenhum endpoint confirmado | Desconhecidas | Desconhecidos | Desconhecidos | Relatórios e backfill | Não suportado sem documentação/payload real |
| Dispositivos / inversores / strings | Nenhum endpoint confirmado | Desconhecidas | Desconhecidos | Desconhecidos | Diagnóstico e disponibilidade | Não suportado |
| Escrita operacional | Fora de âmbito | — | — | — | — | Não suportado por decisão de produto |

## Regras implementadas

- A descoberta vem sempre da API. `SIGENERGY_SYSTEM_IDS`, `SIGENERGY_SYSTEM_ID`
  e a coluna legado `integration_configs.system_ids` são ignorados.
- O inventário externo fica em `provider_system_inventory`; o
  `asset_integrations` mantém apenas o mapeamento para um asset local.
- Um pedido de onboarding só passa a aprovado quando o System ID aparece numa
  descoberta real.
- “Testar ligação” autentica e lista sistemas, sem chamar `energyFlow`.
- “Atualizar instalações” executa apenas descoberta.
- “Sincronizar todas agora” executa uma descoberta completa e depois consulta
  o estado atual de cada sistema devolvido. Uma falha isolada não bloqueia os
  restantes sistemas.
- Um HTTP 403 no estado atual é guardado como “Sem autorização para esta
  instalação”. Não há retry agressivo.
- HTTP 429 respeita `Retry-After` quando presente; na ausência desse header é
  aplicado um cooldown conservador de 60 minutos.
- HTTP 5xx e falhas de rede usam o backoff curto e limitado do cliente.
- Token, App Secret e headers de autorização são sanitizados.
- Não existe paginação porque a forma do cursor/página não está confirmada.

## Quotas e scheduler

Descoberta e estado usam áreas separadas na fila persistente, mas partilham uma
lease de conta. O reset diário usa `Europe/Lisbon`.

Os limites ficam vazios por defeito até serem confirmados. Podem ser
configurados operacionalmente com:

- `SIGENERGY_API_MIN_INTERVAL_SECONDS`
- `SIGENERGY_API_DAILY_BUDGET`
- `SIGENERGY_DISCOVERY_MIN_INTERVAL_SECONDS`
- `SIGENERGY_DISCOVERY_DAILY_BUDGET`
- `SIGENERGY_STATE_MIN_INTERVAL_SECONDS`
- `SIGENERGY_STATE_DAILY_BUDGET`

Não existe `sleep(0.2)` entre instalações. A sincronização periódica executa
uma descoberta completa em cada ciclo, garantindo pelo menos uma descoberta
diária quando o scheduler está ativo.

## Energia e relatórios

`energyFlow` é potência instantânea e só alimenta
`integration_realtime_snapshots`. O sinal de `gridPower` e `batteryPower` não é
usado para calcular energia.

`energy_interval_facts` é a base genérica reservada para energia documentada por
intervalo, com provider, unidade implícita em kWh, timezone, proveniência e
qualidade. Não recebe dados de `energyFlow`.

Os relatórios escolhem uma única fonte energética ativa por asset. Uma escolha
explícita em `asset_integrations.is_primary_energy_source` vence; instalações
legadas continuam a preferir FusionSolar até a fonte ser alterada. Relatórios
leem apenas factos persistidos (`production_records` e
`production_hourly_records`) e nunca chamam a API Sigenergy durante a geração.

Sem histórico ou telemetria energética Sigenergy confirmados, meses anteriores
não podem ser reconstruídos a partir de snapshots de potência. O resultado é
“dados insuficientes para cálculo financeiro”; kWh não são inventados.

## Data Subscription e configuração manual

Não há endpoints de webhook ativos porque a repo não contém o contrato oficial
de autenticação/assinatura, IDs de evento, timestamps ou payloads. Quando essa
documentação e payloads anonimizados forem fornecidos, devem ser criados
receptores separados para System Data, Telemetry e Alarms, com idempotência e
retenção segura. Até lá não há URLs a configurar no portal Sigenergy.
