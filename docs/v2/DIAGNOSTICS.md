# Diagnóstico operacional: estudo e proposta (M7 foundation)

Este documento é o resultado do estudo pedido antes de qualquer implementação:
o que a arquitetura atual já suporta, o que a V1 realmente prova, e o que fica
deliberadamente por fazer nesta sessão. Não foi escrita nenhuma migração, modelo
ou UI para diagnóstico — o âmbito pedido era estudar e propor, e implementar
apenas o que encaixasse naturalmente sem inventar dados.

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

## A proposta: duas fatias, nesta ordem

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

## O que fica deliberadamente fora

- **Disponibilidade ponderada** já está portada e validada contra a V1
  (`rules/availability.py`) — falta-lhe só o facto de dispositivo da Fatia 1
  para deixar de estar bloqueada. Não é trabalho novo, é o mesmo bloqueio já
  documentado em `KNOWN_GAPS.md`.
- **Nenhuma métrica financeira entra aqui.** Diagnóstico é estado de
  equipamento; reporting é dinheiro e energia contratada. `production_facts`
  já separa isto por `metric_kind`, e um facto de dispositivo seguiria a mesma
  regra — nunca uma linha que sirva as duas camadas.
- **Strings/MPPT não são propostos como coluna nem como métrica** até um
  provider realmente os entregar. A coluna vazia na V1 é o argumento contra
  modelá-los agora, não a favor.

## Porque não foi implementado nesta sessão

Os quatro pontos anteriores (Portfolios, Reporting de portfolio, workflow
mensal, Reporting UI) somaram cinco commits, uma migração aplicada à base viva
e testada em dados reais. Uma quinta fatia deste tamanho — schema, importador,
serviço e UI — nesta mesma sessão arriscaria a mesma pressa que já produziu dois
bugs estruturais nas fatias anteriores (ambos corrigidos, mas só porque foram
apanhados por teste). A Fatia 1 acima está suficientemente estudada para
arrancar directamente na próxima sessão sem repetir este levantamento.
