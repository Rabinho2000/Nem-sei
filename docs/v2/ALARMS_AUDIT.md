# Alarms: contract audit only (Prioridade 7), 2026-08-20

Auditoria, não implementação — por pedido explícito e porque a evidência
real não chega para implementar com confiança. Nada abaixo cria uma tabela,
uma migração, ou um adaptador novo.

## O que a V1 realmente guarda: nada

`grep` directo ao schema SQLite da V1 (via um snapshot `VACUUM INTO`, não o
ficheiro live) não encontra nenhuma tabela com "alarm" no nome. V1 **nunca
persistiu um único alarme**, de nenhum provider, alguma vez. Isto muda a
forma como esta auditoria tem de ser lida: ao contrário de
`device_status_facts` (Fatia 1), que importou 51 289 linhas reais e podia
validar um normalizador contra elas, não existe aqui nenhum histórico real
para importar nem para validar um porte contra.

## O que a V1 realmente faz: uma leitura live, on-demand, nunca guardada

`FusionSolarClient.alarms()` (`monitoring_board/services/fusionsolar_client.py:329`)
chama o endpoint de alarmes da FusionSolar (`stationCodes`, `beginTime=0`,
`endTime=agora` — pede o histórico completo de sempre até agora, não uma
janela) e devolve os alarmes activos por central. Isto só é chamado a partir
de `app_factory.py:18235`, dentro do refresh da lista de centrais admin, e
só quando `include_diagnostics=True` — uma opção explícita do operador, não
um sync agendado. O resultado (`alarm_map`) é usado **só para construir a
resposta dessa chamada** (contagens, resumo textual, nível de severidade
mais alto para ordenar/colorir a lista) e depois é descartado — nunca escrito
em nenhuma tabela. Confirmado pelo código, não assumido: não há nenhum
`INSERT`/`UPDATE` envolvendo alarmes em `app_factory.py`.

## Forma do payload bruto (evidenciada pelo código, nunca por uma linha real guardada)

`normalize_fusionsolar_alarm` (`app_factory.py:16366`) lê, por alarme:

| Campo V2 pretendido | Candidatos no payload FusionSolar |
| --- | --- |
| nome | `alarmName`/`alarm_name`/`name`/`alarmType`/`alarmTypeName`/`faultName`/`eventName`/`cause` |
| identidade do dispositivo | `devName`/`deviceName`/`device_name`/`equipmentName`/`inverterName`/`devAlias`/`devTypeName`/`devDn`/`deviceDn` |
| severidade | `lev`/`level`/`severity`/`alarmLevel`, mapeado por `fusionsolar_alarm_severity_rank` para uma escala ordinal 1-4 (crítico/major/minor/warning), com vocabulário parcial só coberto por strings PT/EN observadas no código, não por uma lista oficial do provider |
| início | `raiseTime`/`startTime`/`occurTime`/`happenTime` |
| estado | `status`/`alarmStatus`/`state`, lido mas nunca interpretado — nenhum valor real alguma vez foi visto e guardado para confirmar o vocabulário |

**Duas lacunas estruturais, não de detalhe:**

1. **Identidade do dispositivo é só por nome, nunca por id estável.** Todo
   o resto desta sessão (`device_status_facts`, a canonicalização do M1)
   ancora identidade em `device_id`/identificadores externos resolvidos, não
   em texto livre. Ligar um alarme a um `Device` canónico da V2 precisaria
   de um matching por nome — exactamente o tipo de heurística frágil que
   `classify_fusionsolar_inverter_availability` (código de estado) evitou ao
   copiar vocabulário exacto em vez de adivinhar.
2. **Nenhum lifecycle observado.** Sem uma única linha alguma vez guardada,
   não há evidência do que `status`/`alarmStatus`/`state` realmente
   contém em produção, nem se a FusionSolar expõe "cleared_at" nalgum lado,
   nem como agrupar re-ocorrências do mesmo alarme ao longo do tempo. V1
   nunca precisou de responder a isto porque cada leitura era efémera.

## Sigenergy: sem contrato, re-confirmado

`docs/apis/sigenergy.md` linha 114-116 lista alarmes e strings
explicitamente fora de âmbito, tal como inversores/disponibilidade ao nível
de dispositivo (já auditado em `DEVICE_TELEMETRY.md`). `grep` a
`sigenergy_contracts.py` não encontra nenhum endpoint de alarmes — a única
ocorrência de "alarm" é um valor possível de um campo de estado textual
(`"warning"`, `"alarm"`, `"degraded"`), não um feed de eventos. **BLOCKED**
continua a ser a resposta correcta, sem qualquer novidade desde a auditoria
anterior — não há comportamento FusionSolar a assumir aqui, porque
FusionSolar também não tem evidência suficiente para implementar hoje.

## Decisão de arquitectura (se/quando isto avançar)

Consistente com o padrão já estabelecido nesta sessão em todo o lado —
`production_facts` bruto vs. `reporting/` derivado,
`device_status_facts` bruto vs. `diagnostics/findings.py` derivado —
**duas camadas separadas, não uma**:

- Uma camada de **factos brutos do provider** (`provider_alarm_facts` ou
  equivalente), append-only como tudo o resto nesta base de dados, ancorada
  em identidade resolvida (não em nome livre — precisaria primeiro de um
  passo de resolução nome→`device_id`, auditável e revisável, tal como
  `asset_provider_mappings` já faz para outras identidades).
- Findings operacionais **derivados** dessa camada, no mesmo módulo
  `diagnostics/findings.py` ou um irmão dele — nunca alarmes brutos do
  provider a alimentar um relatório ou uma decisão directamente.

## Porque não implementar agora

Não por preguiça — porque a evidência real não chega:

- Zero linhas históricas para validar qualquer normalizador contra.
- Identidade só por nome, sem um caminho de resolução já construído.
- Vocabulário de estado nunca confirmado com um valor real.
- Nenhuma chamada `alarms()` foi feita durante os canários da Fatia 2/3 —
  mesmo o contrato ao vivo desta sessão nunca tocou este endpoint.

Se este trabalho avançar, o primeiro passo não é uma migração — é uma
chamada `alarms()` real e isolada contra a conta canário já validada,
guardando o payload bruto para inspecção, tal como a Fatia 2 fez para
`getDevRealKpi` antes de desenhar qualquer schema.
