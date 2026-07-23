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
leases expirados são libertados e os jobs vencidos voltam à fila. Um erro 407
aplica cooldown apenas a `production_kpi`, sem bloquear estado ou diagnósticos.

A estrutura equivalente para Sigenergy é criada separadamente por credencial e
endpoint. `SIGENERGY_PRODUCTION_MIN_INTERVAL_SECONDS` e
`SIGENERGY_PRODUCTION_DAILY_BUDGET` permanecem vazios até os limites oficiais
serem medidos e validados.
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
