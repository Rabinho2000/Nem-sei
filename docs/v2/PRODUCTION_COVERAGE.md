# Cobertura de produção: porque uma central está vazia, e como a pôr a receber

Estado: **implementado na branch `v2/availability-wat`, nada deployado.**
Complementa `AVAILABILITY_MIGRATION_PLAN.md` (que trata da disponibilidade)
e `FUSIONSOLAR_OWNERSHIP_WINDOW.md` (que trata do orçamento de chamadas da
conta partilhada). Este documento é sobre o *outro* pipeline: a produção
diária.

## 1. O problema

Muitas instalações não recebem produção diária, e até aqui a única forma de
saber porquê era abrir o psql e percorrer a cadeia à mão. Sete perguntas,
por uma ordem que já era preciso conhecer:

1. a central tem mapping de planta activo hoje?
2. tem política de fonte para `source_use="production"`?
3. há exactamente uma primária aplicável?
4. a ligação está activa e configurada?
5. tem referência de credencial, e o contrato de produção verificado no
   ambiente do worker (`<PREFIX>_PRODUCTION_TIMEZONE`,
   `<PREFIX>_PRODUCTION_UNIT=kWh`)?
6. existe cursor (`production_history` / `fusionsolar-daily-production`)?
7. há agendamento para essa ligação, e o que disse a última corrida?

`src/nemsei/diagnostics/production_coverage.py` percorre essa cadeia e
devolve **o primeiro elo partido**. A ordem não é arbitrária: dizer "sem
política de fonte" a uma central que nem mapping tem manda o operador ao
ecrã errado.

O ecrã é `/system/cobertura-producao`. Não executa nada: as coisas que
consertam estes estados vivem em `/mappings`, `/source-policies` e
`/system`, cada uma com a sua auditoria. E não faz uma única chamada ao
provider — um diagnóstico que gastasse orçamento de chamadas a explicar
porque é que as chamadas falham era pior do que não existir.

## 2. Os estados

| Estado | O que significa | O que fazer |
| --- | --- | --- |
| `ok` | Facto diário há ≤ 3 dias | Nada |
| `no_provider_mapping` | Nunca foi mapeada | Mapear em `/mappings` |
| `mapping_inactive` | Há candidatura, nenhuma activa | Aprovar ou corrigir em `/mappings` |
| `no_production_source_policy` | Mapping activo, sem política | Criar em `/source-policies` |
| `ambiguous_production_source_policy` | Duas primárias na mesma prioridade | Reconciliar em `/source-policies` |
| `connection_disabled` | Ligação desactivada ou por configurar | `/system` |
| `credential_reference_missing` | Sem referência de credencial | `/system` |
| `production_contract_missing` | Falta fuso/unidade verificados no worker | Variáveis de ambiente do worker |
| `production_not_initialized` | Sem cursor e sem data inicial | Indicar `initial_production_from_date` |
| `production_cursor_missing` | Data inicial posta, bootstrap por correr | Confirmar `production_sync_enabled` |
| `scheduler_not_enabled_for_connection` | Cursor existe, ligação sem agendamento | Ligar a sincronização de produção |
| `production_cursor_stale` | Cursor mais atrasado do que um incremental pode cobrir | Bounded backfill |
| `rate_limited` | O provider recusou | Nada; recupera sozinho |
| `sync_deferred` | Cooldown da conta | Nada; esperar |
| `sync_failed` | Última corrida falhou | `/system` |
| `no_recent_fact` | Tudo saudável, esta central não recebe nada | Confirmar que o código de estação ainda existe na conta |
| `unknown` | A política primária aponta para um mapping que a central não tem activo | `/source-policies` |

`production_cursor_stale` merece uma nota. `production_max_source_days` é
31: um cursor mais atrasado do que isso não pode ser recuperado pelo
caminho incremental — a corrida falha com "window exceeds the configured
normal-sync safety limit" em cada tick, para sempre. **O comportamento não
foi alterado** (a corrida continua a correr e a falhar alto); o que mudou é
que a condição passou a ter nome e o ecrã recomenda o backfill em vez de
deixar o operador a decifrar a mensagem do serviço. Recuperar
automaticamente seria uma decisão de gastar chamadas que ninguém pediu.

## 3. Agendar produção em mais do que uma ligação

Até agora era uma variável de ambiente,
`NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_CONNECTION_ID`, e portanto uma só
ligação. A contenção estava certa — uma conta partilhada e limitada não é
sítio para varrer a frota — mas no sítio errado: "que ligações são
sincronizadas" era um facto de deploy que nada na base de dados sabia
responder.

Migração **0044** põe três colunas em `provider_connections`:

| Coluna | Default | Para quê |
| --- | --- | --- |
| `production_sync_enabled` | `false` | Elegibilidade explícita, uma ligação de cada vez |
| `production_sync_interval_hours` | `NULL` | Cadência própria; `NULL` usa a global |
| `initial_production_from_date` | `NULL` | Onde começa a história desta conta |

`NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_ENABLED` continua a ser o interruptor
global. O id do ambiente continua a valer como **alvo adicional**, para um
deployment que ainda não tocou na coluna sincronizar exactamente o mesmo
que hoje.

Não há, e não passa a haver, um modo "todas as ligações": três factos têm
de coincidir — a ligação existir activa e configurada, e alguém ter ligado
a sincronização de produção nela.

Cada alvo mantém tudo separado: `ScheduleState`
(`production.incremental:{id}` / `production.bootstrap:{id}`), chave de
dedupe, cursor (`sync_cursors` é por ligação), cooldown
(`provider_request_states` idem), intervalo próprio e linha própria no ecrã
de automações. Rate limit, session cache, chunking e restart safety ficam
onde estavam.

## 4. Bootstrap de uma ligação nova

Um incremental sem cursor recusa-se a arrancar:

```
The first production sync requires an explicit start date.
```

E essa recusa está certa — adivinhar a data inicial é ou um ano de chamadas
que ninguém pediu, ou um buraco silencioso no início. A segurança de bounds
não foi removida.

O que existe agora é um caminho explícito:

1. pôr `initial_production_from_date` na ligação;
2. pôr `production_sync_enabled = true`;
3. o primeiro job é um `production.bounded_backfill` com `bootstrap: true`,
   dessa data até **ontem em hora local do provider** — resolvido dentro do
   serviço (`sync_bootstrap_backfill`), porque só ele carrega o contrato de
   produção com o fuso verificado;
4. quando o backfill cria o cursor, a ligação passa a `incremental` e o
   bootstrap nunca mais é agendado para ela.

Sem `initial_production_from_date`, a ligação aparece como **"Produção não
inicializada"** e não faz chamada nenhuma.

O bootstrap é idempotente por três vias: `ScheduleState` persistido, chave
de dedupe por slot, e o modo desaparecer assim que há cursor. Repetir o
backfill também não duplica factos — `production_facts` é append-only com
idempotência canónica.

Uma história mais longa do que `production_backfill_max_source_days` (366)
é percorrida em pedaços: cada bootstrap leva a fatia mais antiga permitida e
o cursor que deixa é de onde o tick seguinte recomeça. Nada marca a ligação
como concluída e o ecrã de cobertura continua a dizer até onde chegou.

## 5. Como pôr uma ligação a receber produção

Por SQL, enquanto não houver formulário (a página de cobertura é só
observabilidade nesta fase):

```sql
UPDATE provider_connections
   SET production_sync_enabled = true,
       initial_production_from_date = DATE '2026-01-01'
 WHERE id = <connection_id>;
```

Depois, no ecrã `/system/cobertura-producao`, a ligação deve passar de
`production_not_initialized` para `production_cursor_missing` e, assim que
o primeiro backfill fechar, as suas centrais passam a `ok`.

**Uma ligação de cada vez.** A conta FusionSolar é partilhada com o V1 e é
o orçamento de chamadas que decide o ritmo; ligar duas ao mesmo tempo é
como se perde o dia todo em `rate_limited`.
