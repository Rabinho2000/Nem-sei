# Diagnóstico operacional: estudo, proposta e Fatia 1 (M7 foundation)

Este documento começou como o estudo pedido antes de qualquer implementação —
o que a arquitetura atual já suporta, o que a V1 realmente prova — e passou,
na continuação da mesma sessão, a registar também a Fatia 1 que esse estudo
propôs: schema, importador e um teste dourado, aplicados à base viva.
A Fatia 2 (leitura live ao nível de dispositivo) continua deliberadamente por
fazer, pelas razões abaixo.

## O que já existe e encaixa

A identidade canónica de dispositivo já está completa e em produção: 325
dispositivos, todos `inverter`, cada um com um `asset_provider_mappings` de
`resource_kind='device'` já reclamado. `MonitoringObservation` e
`ProductionFact` já modelam `condition`/`freshness`/`quality`/`completeness`
como estados de primeira classe, chaveados por `provider_mapping_id` — o mesmo
mecanismo que, apontado a uma claim de dispositivo em vez de planta, dá
naturalmente um facto ao nível do inversor sem inventar um novo vocabulário de
qualidade.

Ou seja: a fundação de identidade e o padrão de modelação de factos já existem.
O que falta é só a camada de factos ao nível de dispositivo — e essa é
exactamente a peça que este documento propõe, sem a construir ainda.

## O que a V1 realmente prova (e o que não prova)

Auditoria read-only de `/opt/server/apps/Nem-sei/data/monitoring_board.db` em
2026-08-19:

| Tabela V1 | Linhas | O que guarda |
| --- | --- | --- |
| `provider_devices` | 325 | Identidade do dispositivo no provider — já importada para `devices` |
| `inverter_power_samples` | 54 593 | Potência activa por inversor, amostrada |
| `device_realtime_snapshots` | 51 289 | Estado, potência, energia do dia, disponibilidade, comunicação, por inversor |
| `inverter_availability_sampled_daily` | 6 720 | Disponibilidade diária amostrada por inversor |
| `telegram_alerts` | 30 105 | Alarmes efectivamente enviados, com tipo, mensagem e estado de envio |

`device_realtime_snapshots` já tem exactamente os campos que a lista do pedido
nomeia — `inverter_state`, `active_power_kw`, `day_energy_kwh`,
`availability_status`, `communication_status` — e **duas colunas para
strings/MPPT**, `pv_current_json` e `pv_voltage_json`.

Essas duas últimas estão **vazias em todas as 51 289 linhas**. A coluna existe;
o dado nunca foi capturado. Isto é dito aqui em vez de ser silenciado: string/
MPPT não é uma métrica que a V1 prove, apesar de o schema sugerir que sim, e
não deve ser modelada como se estivesse disponível até um provider a entregar
de facto.

`inverter_state` é um código numérico do provider (`40960` no exemplo
verificado), decodificado pela lógica própria da V1 — decodificar sem essa
lógica seria inventar um significado, não portá-lo.

## O que nenhum adaptador V2 ainda lê ao nível de dispositivo

`integrations/fusionsolar/` e `integrations/sigenergy/` falam hoje só ao nível
da planta. Nenhum dos dois tem um pedido de leitura de dispositivo verificado —
o mesmo portão que já existe para a produção diária (`PVYield` verificado por
ligação antes de qualquer persistência) ainda não tem equivalente ao nível do
inversor. Construir a tabela de factos sem essa verificação repetiria
exactamente o erro que esse portão existe para prevenir.

## A proposta original: duas fatias, nesta ordem

**Fatia 1 — histórico de dispositivo importado da V1 (valor imediato, risco
baixo).** Estender `production_facts`/`monitoring_observations` — ou uma tabela
irmã dedicada, dado o volume (~106 mil linhas combinadas) — para aceitar um
`device_id` opcional, e importar `inverter_power_samples` +
`device_realtime_snapshots` como histórico, pelo mesmo padrão já provado três
vezes nesta sessão (`v1_reporting_import.py`, `portfolios/v1_import.py`):
idempotente, com proveniência, sem adivinhar nada. Isto dá um ecrã de
diagnóstico com dados reais — "este inversor esteve assim em Julho" — sem
depender de nenhuma leitura live nova.

**Fatia 2 — leitura live ao nível de dispositivo (bloqueada até um contrato
verificado).** Estender `FusionSolarProductionContract` (ou equivalente) com um
pedido de dispositivo, verificado por ligação, antes de qualquer persistência
live. Alarmes ficam nesta fatia: `telegram_alerts` é histórico do que a V1
enviou, não um feed de alarmes do provider — um alarme "ao vivo" precisa do seu
próprio contrato verificado, tal como a produção precisou.

## O que a Fatia 1 entregou, e onde a proposta original estava incompleta

A ideia de "estender `production_facts`/`monitoring_observations`" não
sobreviveu ao primeiro facto real: **nenhuma das 325 claims de dispositivo
está `active`** — todas ficaram `pending_review` no import do M1, em ligações
desativadas e sem credenciais. `production_facts`/`monitoring_observations`
estão ambas ancoradas em `provider_mapping_id`; uma tabela que exigisse uma
claim utilizável não podia aceitar uma única linha hoje. Isto só apareceu ao
tentar escrever a primeira linha, não na leitura do schema — exactamente o
tipo de coisa que "propõe" não podia ter previsto sem chegar a "implementa".

`device_status_facts` (migração `0015_device_status_facts`) ficou ancorada em
`device_id` em vez disso — a identidade que o M1 já resolveu para os 325
dispositivos, independente de qualquer ligação de provider estar utilizável.
Guarda leituras pontuais (`observed_at`, não um período): `availability_status`
no vocabulário da própria V1 (`available`/`standby`/`unavailable`/`unknown`,
não o `OBSERVATION_CONDITIONS` de cinco valores do V2, que não tem
correspondência honesta para "standby"), mais `active_power_kw` e
`day_energy_kwh`, ambos NULL quando ausentes e nunca zero por omissão.

A classificação `inverter_state -> availability_status` foi portada, não
reinventada, de `monitoring_board/services/fusionsolar.py`
(`classify_fusionsolar_inverter_availability`) — os três conjuntos de códigos
(`AVAILABLE_INVERTER_STATES`, `UNAVAILABLE_INVERTER_STATES`,
`STANDBY_INVERTER_STATES`) são código real da V1, copiados, não adivinhados a
partir dos números. `tests_v2/test_diagnostics_golden.py` compara a função
portada contra a coluna `availability_status` que a V1 já tinha calculado, para
cada `inverter_state` distinto real — só corre a partir do host
(`/opt/server/apps/Nem-sei/.venv/bin/python`), pela mesma razão de WAL que os
outros testes dourados desta sessão.

`inverter_power_samples` (54 593 linhas, só potência, um único dia) não foi
importado nesta fatia — `device_realtime_snapshots` já cobre estado, potência
e energia numa janela muito mais larga, e importar as duas seria trabalho
duplicado sem uma pergunta nova a responder. Fica registado, não esquecido.

`communication_status` também não foi importado: lê `"recent"` nas 51 289
linhas, sem excepção — `test_diagnostics_golden.py` verifica isto directamente
em vez de assumir. "Última comunicação" é respondida pelo próprio
`observed_at`, não por um rótulo que não varia.

### Provado com dados reais

Contra a base viva, depois de aplicada a migração `0015`: importação real de
`device_realtime_snapshots`, seguida de uma segunda importação do mesmo
período para confirmar zero linhas novas. Ver o resumo da sessão para os
números exactos.

## O que fica deliberadamente fora

- **Disponibilidade ponderada** já está portada e validada contra a V1
  (`rules/availability.py`), e agora tem uma fonte de factos reais em
  `device_status_facts` — mas as duas ainda não estão ligadas. Calcular
  disponibilidade a partir destes factos, e expô-la no relatório individual em
  vez de `availability_pct: None`, é o passo seguinte concreto, não mais
  levantamento.
- **Nenhuma UI de diagnóstico foi construída.** `current_device_status()` em
  `diagnostics/service.py` é a leitura mínima que uma futura página
  precisaria — "o que é que cada dispositivo desta instalação está a fazer
  agora" — mas não há rota nem template. Fundação, não a funcionalidade.
- **Nenhuma métrica financeira entra aqui.** Diagnóstico é estado de
  equipamento; reporting é dinheiro e energia contratada. `production_facts`
  já separa isto por `metric_kind`, e `device_status_facts` vive no seu
  próprio pacote (`nemsei/diagnostics/`), nunca uma linha que sirva as duas
  camadas.
- **Strings/MPPT não são propostos como coluna nem como métrica** até um
  provider realmente os entregar. A coluna vazia na V1 é o argumento contra
  modelá-los agora, não a favor.

## Próximo passo concreto

Ligar `device_status_facts` ao cálculo de disponibilidade (`rules/availability.py`)
e ao payload do relatório individual, substituindo `availability_pct: None`
por um valor real onde os factos existirem. Depois disso: uma página mínima de
diagnóstico por instalação, lendo `current_device_status()` — a leitura já
existe, falta só a rota e o template, seguindo exactamente o padrão já usado em
`/reports`.
