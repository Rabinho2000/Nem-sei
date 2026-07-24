# FusionSolar API

Esta app isola a comunicação FusionSolar em `monitoring_board/services/fusionsolar_client.py`.
O resto da aplicação deve chamar o client ou os wrappers compatíveis do `app_factory.py`, sem montar endpoints FusionSolar diretamente.

## Sessão e autenticação

- Endpoint: `/thirdData/login`
- Payload: `userName` e `systemCode`.
- O client extrai `XSRF-TOKEN` primeiro dos headers da resposta e, se não existir, dos cookies `XSRF-TOKEN`/`xsrf-token`.
- A sessão é guardada em cache por configuração (`FUSIONSOLAR_SESSION_CACHE_MINUTES`, default atual do app).

## Fila persistente dos KPIs de produção

Todas as chamadas aos KPIs diário e mensal passam por uma fila SQLite, isolada
por fornecedor, credencial e endpoint. Para FusionSolar, os valores predefinidos
são:

- `FUSIONSOLAR_PRODUCTION_KPI_MIN_INTERVAL_SECONDS=65`;
- `FUSIONSOLAR_PRODUCTION_KPI_DAILY_BUDGET=20`;
- `FUSIONSOLAR_PRODUCTION_KPI_DAILY_RESERVED_CALLS=2`;
- `FUSIONSOLAR_PRODUCTION_KPI_MONTH_CLOSE_RESERVED_CALLS=2`.

O orçamento usa o dia civil `Europe/Lisbon`. As quatro chamadas reservadas
protegem separadamente a produção diária e o fecho mensal: uma prioridade não
pode consumir a reserva da outra. Backfills e pedidos de relatórios
só usam o orçamento não reservado. Quando não existe slot, o job fica em
`waiting_api_slot` e é retomado automaticamente. Ao atingir o orçamento diário,
`wait_reason=daily_budget` e a retoma ocorre na meia-noite local seguinte.

Reservas, contadores, cooldowns e leases são persistentes. Após um reinício,
leases expirados são libertados e os jobs vencidos voltam à fila.

## Coordenação global da conta e WAT

Produção, estado e diagnósticos partilham uma lease persistente por conta
FusionSolar, impedindo chamadas concorrentes e bursts entre jobs. O WAT e os
diagnósticos usam uma área e orçamento próprios,
`FUSIONSOLAR_WAT_DAILY_BUDGET=36`, sem consumir o orçamento KPI. Não é
configurado um intervalo WAT artificial enquanto o limite oficial não estiver
validado.

Um 407 aplica cooldown à conta FusionSolar inteira e adia todos os jobs
FusionSolar. Sigenergy permanece isolado. A prioridade é: produção diária,
fecho mensal, backfill manual, pedidos de relatórios e, por fim, WAT/diagnóstico.
O WAT é resumível: guarda data e inversores já tentados, processa no máximo dez
inversores por chamada e retoma pelo scheduler sem dormir dentro do pedido.

O inventário de centrais é atualizado uma vez por dia. Nos restantes syncs
horários são reutilizados os IDs de `asset_integrations` e apenas o realtime é
pedido em blocos até 100 centrais.

A estrutura equivalente para Sigenergy é criada separadamente por credencial e
endpoint. `SIGENERGY_PRODUCTION_MIN_INTERVAL_SECONDS` e
`SIGENERGY_PRODUCTION_DAILY_BUDGET` permanecem vazios até os limites oficiais
serem medidos e validados.

## Retenção de realtime

`FUSIONSOLAR_REALTIME_SNAPSHOT_RETENTION_DAYS=30` controla a retenção dos
payloads JSON pesados. Antes da limpeza, a disponibilidade amostrada é
materializada localmente. As linhas normalizadas, agregados, WAT real,
produção, snapshots de relatórios e ficheiros gerados não são apagados. Não é
executado `VACUUM` automático.
- Se uma resposta devolver `failCode=305` ou mensagem `USER_MUST_RELOGIN`, o client invalida a cache e tenta login mais uma vez.
- Passwords, tokens e `systemCode` não devem ser escritos em logs.

## Validação comum

Todas as respostas JSON passam por validação centralizada:

- `success` tem de ser `true`.
- `failCode` tem de ser `0`.
- Quando o método precisa de dados, a chave `data` tem de existir.
- `failCode=407`, mensagens equivalentes de limite de chamadas, ou HTTP `429` são tratados como rate limit.
- HTTP `5xx` e erros de rede usam backoff curto e limitado pela camada comum.

## Endpoints usados

| Área | Endpoint | Método do client | Notas |
| --- | --- | --- | --- |
| Login | `/thirdData/login` | `login` | Cria sessão e token XSRF. |
| Centrais | `/thirdData/stations` | `stations` | Usa paginação com `pageNo` e `pageCount`. |
| Estado central | `/thirdData/getStationRealKpi` | `station_realtime_kpi` | Chamadas de estado/monitorização. |
| Dispositivos | `/thirdData/getDevList` | `device_list` | Lista equipamentos por `stationCodes`. |
| Estado dispositivo | `/thirdData/getDevRealKpi` | `device_realtime_kpi` | Agrupa por `devTypeId` e envia `devIds`. |
| Histórico dispositivo | `/thirdData/getDevHistoryKpi` | `device_history_kpi` | Dados pesados/diagnóstico por intervalo fechado do dia. |
| Alarmes | `/thirdData/getAlarmList` | `alarms` | Usa `beginTime=0`, `endTime=now`, `language`. |
| KPI diário central | `/thirdData/getKpiStationDay` | `station_day_kpi_map`, `station_day_kpi_rows` | Produção diária. |
| KPI mensal central | `/thirdData/getKpiStationMonth` | `station_month_kpi_map` | Produção mensal. |

## `collectTime`

O comportamento atual foi preservado para evitar alterar relatórios existentes:

- `station_day_kpi_map`: usa início do dia local do processo (`00:00:00`) para a data pedida.
- `station_month_kpi_map`: usa início do dia local do processo (`00:00:00`) no primeiro dia do mês.
- `station_day_kpi_rows`: preserva o comportamento anterior e usa meio-dia local (`12:00:00`) no primeiro dia do mês da data pedida.
- `device_history_kpi`: usa janela local fechada `[00:00:00.000, 23:59:59.999]` para o dia alvo.

TODO: confirmar na documentação oficial/ambiente real se a FusionSolar espera `collectTime` na timezone do portal, da central, do browser, ou UTC. Os testes atuais fixam apenas o comportamento já usado pela app.

## Rate limit

O client só identifica a condição técnica (`FusionSolarRateLimitError`). A persistência do cooldown e a exposição na UI ficam na camada comum `api_rate_limit`/jobs, para manter o client reutilizável e sem dependência de Flask/SQLite.
