# Fase 0 — eliminar os falsos sinais de sucesso

Execução de AUD-001, AUD-002, FIX-001, FIX-002, FIX-003 e FIX-004 do
`NEMSEI_V2_IMPLEMENTATION_PLAN.md`. Nada de ING-001 ou posterior.

A regra que decidiu cada escolha aqui: **uma falha explícita é aceitável; um
sucesso falso ou uma lacuna silenciosa não é.**

---

## Baseline

Verificado antes de qualquer alteração, e não herdado do documento.

| | |
|---|---|
| Repositório auditado | `Rabinho2000/Nem-sei` |
| SHA auditado | `7815cb7aa316c7ee073a1b46aafc183c7b724a89` |
| Onde está esse SHA hoje | `origin/v2/operacional` (tronco) e `Nem-sei-v2-rollout` em detached HEAD |
| Branch desta execução | `v2/phase0-reliability`, criada a partir de `7815cb7` |
| Worktree | `/opt/server/apps/Nem-sei-v2-phase0`, criada de propósito para esta execução |
| Migration head (código) | `0044_production_scheduling`, um único head |
| Migration head (BD de produção) | `0044_production_scheduling` — coincide |
| PostgreSQL | 16.11 (Debian), `nemsei-v2-postgres-1`, up 2 semanas |
| Interpretador | Python 3.14.4, venv em `/opt/server/apps/Nem-sei-v2/.venv` |
| PostgreSQL de testes | `nemsei-v2-test-pg`, `127.0.0.1:55432`, alcançável |

### O código mudou desde a auditoria?

Sim, e nada do que mudou toca nesta correção. Existem dois commits em
`origin/v2/fix-production-coverage` (`d7dfee0`, `03e8064`) à frente de
`7815cb7`, ambos em `diagnostics/production_coverage.py` e no `docker-compose`.
Não há sobreposição com nenhum ficheiro alterado aqui, por isso esta branch
parte do tronco auditado e não do ramo por fundir.

Nenhum dos findings tratados aqui tinha sido corrigido entretanto. Cada um foi
demonstrado a falhar em `7815cb7` antes de ser corrigido — a lista está em
*Tests before/after*.

### Estado do deployment (AUD-001)

`scripts/v2_audit_manifest.py`, novo, produz isto em leitura pura
(`SET TRANSACTION READ ONLY`), sanitizado por allowlist, e recusa-se a emitir
se o documento corresponder a uma forma de segredo. O manifest completo está
em anexo; o essencial:

- **Serviços a correr:** `web`, `scheduler`, `worker`, `scada-listener`,
  `postgres` — projeto Compose `nemsei-v2`, a construir de
  `/opt/server/apps/Nem-sei-v2-rollout`.
- **Timer de backup:** `nemsei-v2-backup.timer` instalado e **ativo**; última
  execução 2026-09-07 03:39 WEST, próxima 2026-09-08 03:35 WEST.
- **A configuração do ficheiro e a dos contentores discordam.** `.env.v2` diz
  `NEMSEI_V2_PROVIDER_READS=false`; `scheduler` e `worker` correm com `true`
  (override do Compose). O manifest reporta as duas vistas, porque reportar só
  o ficheiro descreveria um deployment que não faz chamadas ao provider, ao
  lado de uma base de dados cheia de chamadas ao provider. `NEMSEI_V2_ENV` é
  `preview`, não `production`.
- **Capacidades efetivas nos contentores:** `provider_reads` on (scheduler,
  worker), `notifications` on, `provider_mutations` off,
  `report_distribution` off.
- **Contagens verificáveis:** 214 organizações, 267 instalações, 267 ativos,
  325 dispositivos, 6 ligações provider (4 ativas), 738 mappings (278 ativos),
  416 políticas de fonte, 225 974 factos de produção, 4 838 observações,
  14 106 sync runs, 7 556 jobs (0 ativos), 248 snapshots de relatório.
- **132 dos 267 ativos têm dois mappings de planta ativos.** É a exposição
  real do FIX-003.
- **Não verificável, e declarado `unknown`:** o diretório de backups
  (`/opt/server/apps/Nem-sei-v2-data/backups`) não é legível por esta conta, e
  a data do último restore validado não é dedutível de uma listagem — o
  manifest diz `unknown` nos dois casos em vez de assumir.

---

## Findings confirmed

Cada um confirmado no código **e**, onde a base de dados o permitia, em dados
reais deste deployment.

| Finding | Confirmação |
|---|---|
| **F01** — vazio Sigenergy avança cobertura | `production.py:250` incrementava `accepted` para uma `ParsedDay` com `quality='missing'`; sem `last_error`, o run terminava `success` e o cursor avançava |
| **F02** — dia em curso fechado como total diário | `sync_incremental` usava `utc_now().date()` e incluía hoje. **Em dados: os 145 factos Sigenergy desta base foram todos escritos por um run iniciado às 10:40 UTC *dentro* do dia que estava a guardar** (14 dias, 2 ativos, desde 2026-08-25). O cursor está em `last_completed_day: 2026-09-07` — hoje |
| **F03** — sucesso do job incorreto | `_execute_sigenergy_production` mapeava `partial → success`; `_execute_current_monitoring` devolvia `success` incondicionalmente |
| **F04** — outcome `failed` recusado por `finish` | `finish` só aceitava success/partial; `availability.history_sync` devolve `failed`, o `except Exception` do worker registava `ValueError` e repetia mais duas vezes |
| **F09** — contadores removidos | `safe_metadata` não tinha `expected`/`received`/`accepted`/`rejected`/`facts_written`/`error_code` na allowlist |
| **F14** — frescura entre fontes | `current_installation_states` lia a observação mais recente de qualquer mapping e, em separado, o `max(last_confirmed_at)` de qualquer mapping |
| **F15** — soma de fontes duplicadas | Redução por `(provider_mapping_id, source_fact_key)` seguida de soma. **Em dados: ativo 180, 2026-07-24 — 59,55 kWh de um mapping e 59,56 de outro, somados em 119,11 para um dia que fez ~59,56** |
| **F21** — mock Telegram em runtime | `default_client_factory` sem token devolvia `MockTelegramClient`, cujo `send_message` devolve `delivered=True`. `notifications` está **on** neste deployment |
| **F23** — backup parcial elegível | `pg_dump ... > "$archive"` escrevia diretamente no nome final |
| **F28** — saúde otimista ao começar | `start_sync_run` chamava `record_health` sem erro → `last_success_at = now`. **Em dados: a ligação 5 reportava sucesso às 16:10 quando a última sincronização realmente bem-sucedida foi às 10:40** |

Nenhum finding do plano foi encontrado já corrigido.

---

## Changes implemented

### FIX-001 — Sigenergy (`integrations/sigenergy/production.py`)

- A janela resolve-se **depois** do contrato e no calendário da fonte, e
  termina no último dia fechado. `sync_daily_production` também está limitado:
  um chamador não pode comprar um total final para um dia que ainda não
  aconteceu.
- `max_days <= 0` é recusado em vez de produzir uma janela invertida.
- Completude contada em unidades coerentes: `expected`, `accepted` e
  `rejected` contam todos **mapping-dias**. Antes `expected_items` era
  `len(days)` e `accepted` contava mapping-dias — uma conta com dois sistemas
  podia reportar aceitar cinco de três.
- Um dia só é aceite com as cinco métricas. Payload vazio → falha; dia
  parcialmente lido → `partial` e retryable; nenhum dos dois move o cursor.
- Um zero genuíno continua a ser uma leitura: 0,0 é dado.
- Um dia cuja política de fonte ninguém consegue resolver é contado e
  reportado, em vez de desaparecer silenciosamente do run.
- Factos `missing` continuam a ser escritos: são prova durável de que o dia
  foi pedido e veio vazio. O que deixaram de ser é um dia recolhido.

### FIX-002 — outcomes e saúde (`jobs/`, `sync/service.py`)

- `_execute_sigenergy_production`: `partial`/`failed`/`rate_limited`/`deferred`
  seguem o mesmo caminho da produção FusionSolar — defer contra um cooldown
  real quando existe, senão retry.
- `_execute_current_monitoring`: `rate_limited` defere, recusa por política
  falha visivelmente, `partial` reporta `partial`. Deixou de devolver
  `success` incondicionalmente.
- `finish` aceita `failed`, e o evento leva a razão e os contadores do
  handler, não apenas o veredito.
- `safe_metadata` mantém os contadores, coagidos como inteiros e não truncados
  como texto; a allowlist continua a recusar tudo o resto.
- `start_sync_run` regista uma tentativa através de `record_attempt`, que não
  consegue escrever nenhum campo de resultado.

### FIX-003 — fonte canónica (`monitoring/`, `web/series.py`)

- `monitoring.repository.canonical_facts` toma a decisão de fonte uma vez — a
  de `resolve_source_policy`, expressa como ordenação para poder ser aplicada a
  toda a frota numa consulta em vez de uma chamada por ativo por dia — e o
  leitor por ativo, os totais de frota e o gráfico de portfolio usam-na. Os
  datasets de relatório já liam pelo leitor por ativo, por isso gráfico,
  tabela, totais e relatório respondem com o mesmo número.
- Um mapping sem política fica **em último lugar** em vez de excluído. Excluir
  seria a regra mais estrita e apagaria história: 140 ativos aqui têm factos de
  produção e nenhuma política de produção.
- O dia é lido no fuso em que o facto foi ancorado, porque a validade das
  políticas é expressa em dias e um dia de Lisboa começa às 23:00 UTC do dia
  anterior.
- Estado da instalação: observação e confirmação vêm agora do **mesmo** mapping,
  o que a política de monitorização seleciona.

### FIX-004 — sinais de segurança falsos

- `UnconfiguredTelegramClient` responde a "capacidade ligada, sem token" com um
  resultado falhado e uma razão. O mock só é alcançável a partir de uma
  execução que se declara teste (`NEMSEI_V2_TESTING`).
- O dump escreve para `$archive.partial`, é verificado com `pg_restore --list`
  — comprimento não nulo não é integridade — e só depois é renomeado
  atomicamente. Um `trap` remove o parcial se falhar. Modo 600 verificado nos
  dois nomes.
- A âncora do nome na retenção já tornava os parciais invisíveis nos dois
  sentidos; passou a estar declarada como a regra estrutural que é, e testada.

### Testes existentes que fixavam comportamento errado

Quatro, todos sobre o mesmo finding (F21) e todos atualizados com o finding
que fixavam. Passavam por `default_client_factory` sem token e esperavam
`sent`:

- `test_telegram_client.py::test_the_factory_falls_back_to_the_mock_when_no_token_is_configured`
  → `test_a_runtime_with_no_token_gets_a_client_that_cannot_claim_delivery`.
  A propriedade que protegia — nada sai do processo — continua a valer;
  reportar uma entrega nunca fez parte dela.
- `test_notifications.py::test_evaluate_and_process_notifications_decides_and_delivers_in_one_call`
- `test_worker.py::test_worker_executes_a_real_notification_processing_cycle_end_to_end`
- `test_worker.py::test_a_worker_cycle_delivers_nothing_when_the_kill_switch_is_off`

Os três últimos passaram a declarar `NEMSEI_V2_TESTING=true`, que é como se
pede o mock de propósito. Os dois de `test_worker.py` só apareceram na suite
completa, depois de os alvos óbvios já estarem tratados — razão suficiente
para nunca dar por concluída esta fase com base nas suites dirigidas.

---

## Tests before/after

### Suite completa V2, em `7815cb7`, antes de qualquer alteração

```
pytest -q tests_v2      =>  1794 passed, 0 failed, 0 skipped   (18m46s)
alembic heads           =>  0044_production_scheduling (head)
```

Nota sobre o ambiente: a auditoria original registou 62 passed com 31 skipped
porque `test_quality_rules_golden.py` fixa `V1_ROOT=/opt/server/apps/Nem-sei`,
que não existia naquela máquina. Aqui existe, por isso os golden correm e a
suite completa é executável. **Nenhum teste foi contado como PASS sem correr.**

### AUD-002 — regressões congeladas antes das correções

`tests_v2/test_reliability_regressions.py`, 26 testes, executado em `7815cb7`:

```
21 failed, 5 passed
```

Os 21 que falharam, um por finding, e o que cada um provou em `7815cb7`:

| Teste | Comportamento em `7815cb7` |
|---|---|
| `an_empty_sigenergy_payload_never_reports_a_successful_collection` | `success`, cursor avançado |
| `a_partly_read_sigenergy_day_is_partial_and_holds_the_cursor` | `success`, cursor avançado |
| `one_unread_day_holds_back_coverage_for_the_whole_window` | cobertura reclamava o dia em falta |
| `sigenergy_never_closes_the_day_that_is_still_running` | pedia o dia de hoje |
| `an_explicit_window_cannot_be_used_to_close_the_open_day` | pedia o dia de hoje |
| `a_handler_that_failed_ends_the_job_failed_with_its_own_reason` | `ValueError` do contrato de `finish` |
| `a_monitoring_read_that_failed_is_not_a_successful_job` | `success` |
| `a_partial_sigenergy_production_run_is_not_a_successful_job` | `success` |
| `a_job_result_keeps_the_counters_that_explain_the_outcome` | contadores removidos |
| `a_job_result_still_refuses_anything_it_was_not_asked_to_keep` | (par do anterior) |
| `a_device_status_job_persists_its_counters` | `result_json` sem contadores |
| `starting_a_sync_run_does_not_create_a_record_of_success` | `last_success_at` escrito ao começar |
| `a_failed_run_leaves_the_previous_success_time_alone` | (par do anterior) |
| `two_sources_for_one_day_are_not_added_together` | 119,11 em vez de 59,55 |
| `the_installation_chart_reads_the_same_single_source` | duas linhas para um dia |
| `the_portfolio_chart_reads_the_same_single_source` | 2,0 MWh em vez de 1,0 |
| `a_fresh_read_of_one_source_does_not_refresh_another` | `operational` em vez de `stale` |
| `a_runtime_without_a_telegram_token_cannot_report_a_delivery` | `delivered=True` |
| `an_undeliverable_digest_is_not_recorded_as_delivered` | `delivered=True` |
| `the_dump_is_written_under_a_name_retention_cannot_mistake_for_a_backup` | escrevia no nome final |
| `the_archive_is_verified_before_it_is_renamed_into_place` | sem verificação |

Os 5 que já passavam são as guardas do outro lado de cada correção, e
continuam a passar: um zero genuíno é uma leitura, `max_days=0` é recusado, um
dia só do fallback continua a reportar, uma execução declarada de teste ainda
usa o mock, e a retenção não conta nem apaga um `.partial`. Foi acrescentado um
27.º depois das correções — um tick sem nada em dívida não gasta chamada
nenhuma, nem sequer o login.

### Depois das correções

```
pytest -q tests_v2/test_reliability_regressions.py  =>  27 passed
pytest -q tests_v2/test_audit_manifest.py           =>  14 passed
pytest -q tests_v2                                  =>  1836 passed, 0 failed, 0 skipped (25m02s)
ruff check src/nemsei scripts tests_v2              =>  All checks passed
```

**Nada foi declarado PASS sem correr, e não houve testes skipped.**

---

## Database / data impact

**Nenhuma migration. Nenhuma escrita. Nenhum facto apagado.** Todas as
consultas contra a base de produção nesta execução correram dentro de
`SET TRANSACTION READ ONLY`.

### O que os dados existentes têm de errado

**Sigenergy — 145 factos, 14 dias, 2 ativos, desde 2026-08-25.** Todos escritos
por uma execução iniciada às 10:40 UTC dentro do dia que estavam a guardar, ou
seja, um contador cumulativo apanhado a meio da manhã e arquivado como total
do dia. O código deixou de os produzir; **repará-los é uma decisão com um
orçamento de chamadas ao provider agarrado**, porque significa re-ler os dias
da fonte para que uma nova revisão substitua o valor.
`scripts/v2_sigenergy_day_diagnosis.sql` lista exatamente quais, em leitura
pura. Não foi executada nenhuma reparação.

O cursor Sigenergy está em `last_completed_day: 2026-09-07` — hoje. Depois
desta correção o serviço não avança para lá do último dia fechado, mas
**também não recua**: os dias já reclamados continuam reclamados até serem
re-lidos. É trabalho de reconciliação, não desta fase.

### O que muda nos números que a interface mostra

`scripts/v2_canonical_source_before_after.sql` calcula as duas respostas contra
os dados reais e faz a diferença, porque um total a **descer** é o resultado
correto aqui e precisa de ser visto antes de ser acreditado:

```
linhas antes:   56 242
linhas depois:  56 241        (uma linha deixa de ser contada)

ativo   nome                 antes        depois       delta
180     Entre Vinhas e Mar   19 442,79    19 383,24    -59,55

dia duplicado: ativo 180, 2026-07-24 — 119,11 somado antes, 59,56 mantido depois
ativos sem política de produção, mantidos e ordenados em último: 140
```

Um único ativo muda, e muda 0,3%. A correção do estado da instalação não altera
dados — o estado é derivado a cada leitura, nunca guardado.

---

## Remaining P0 blockers

Por ordem de importância. Nenhum foi introduzido aqui; todos estavam já no
plano e continuam abertos.

1. **Não existe inventário de obrigações de recolha.** Continua a não haver
   forma de provar que nenhuma recolha esperada desapareceu — contar
   `sync_runs` ou `jobs` não responde a isso, e nenhuma correção desta fase
   muda isso. É exatamente o que ING-001/ING-002 (`collection_runs`) existe
   para resolver, e é a única razão determinante para não promover isto a
   fonte operacional principal. (F07)
2. **Lease de worker de 30 s sem heartbeat, e ownership só na linha do job.**
   Um segundo worker pode recuperar um job cujo handler ainda está vivo, e o
   token protege o `UPDATE` da fila mas não os commits de dados que o handler
   faz. (F05, F06)
3. **Revisão e cursor fazem read-modify-write sem lock por chave.** Sequencial
   é seguro; concorrente ainda não foi ensaiado. (F13)
4. **Os 145 factos Sigenergy já escritos continuam errados.** Diagnosticados,
   não reparados.
5. **`reporting/readiness.py::_coverage` conta um dia como coberto mesmo quando
   só a fonte não canónica o tem.** Não soma duas vezes — conta dias distintos
   — mas a decisão de fonte não é a mesma que a dos totais.
6. **Políticas primárias em conflito** (mesma prioridade, mesmo período) são
   resolvidas deterministicamente pelos leitores em vez de produzirem um
   finding explícito. O serviço já as recusa; os leitores escolhem.
7. **Nenhum restore ensaiado.** O timer corre e o dump passou a ser verificado
   antes de contar como backup, mas a última recuperação validada é `unknown`,
   e não há cópia fora deste disco. (F24)

---

## Runtime checks not performed

Declarado, não presumido:

- **Nenhuma chamada real a FusionSolar ou Sigenergy.** Toda a prova de
  provider é contra stubs nas fronteiras de I/O.
- **Nenhum alerta Telegram enviado.**
- **Nenhum backup executado nem restaurado neste turno.** O timer está
  instalado e ativo (última execução 2026-09-07 03:39 WEST), mas o novo
  caminho `.partial` → `pg_restore --list` → `mv` **não foi exercitado numa
  execução real** — só o seu contrato está testado. A primeira execução real é
  a de 2026-09-08 03:35 WEST.
- **Nenhum ensaio de concorrência.** Sem dois workers, sem SIGKILL, sem
  expiração de lease com um handler vivo.
- **Nada deployado.** A branch `v2/phase0-reliability` não foi fundida nem
  publicada; a produção continua a correr `7815cb7` a partir de
  `Nem-sei-v2-rollout`.
- **O diretório de backups não é legível por esta conta**, por isso o número e
  a idade das cópias existentes é `unknown`.
- **Não foi feita reconciliação V1/V2** nem verificado o inventário contratual
  de instalações que deveriam estar cobertas.

---

## Files changed

```
AGENTS.md                                        distingue as regras de V1 e V2
docs/v2/KNOWN_GAPS.md                            o que esta fase fechou e o que não

scripts/v2_audit_manifest.py                     NOVO  baseline read-only, sem segredos
scripts/v2_sigenergy_day_diagnosis.sql           NOVO  dias potencialmente errados
scripts/v2_canonical_source_before_after.sql     NOVO  comparação antes/depois
scripts/v2_postgres_backup.sh                    .partial → verificar → renomear
scripts/v2_backup_retention.py                   a âncora do nome declarada e testada

src/nemsei/integrations/sigenergy/production.py  janela, completude, cursor
src/nemsei/jobs/handlers.py                      outcomes que dizem o que aconteceu
src/nemsei/jobs/repository.py                    finish aceita failed; contadores
src/nemsei/sync/service.py                       record_attempt
src/nemsei/monitoring/repository.py              canonical_facts
src/nemsei/monitoring/installation_state.py      observação e confirmação do mesmo mapping
src/nemsei/web/series.py                         frota e portfolio pela mesma decisão
src/nemsei/notifications/telegram_client.py      UnconfiguredTelegramClient

tests_v2/test_reliability_regressions.py         NOVO  27 testes
tests_v2/test_audit_manifest.py                  NOVO  14 testes
tests_v2/test_telegram_client.py                 expectativa corrigida
tests_v2/test_notifications.py                   expectativa corrigida
tests_v2/test_worker.py                          expectativa corrigida
```

## Commits

```
32abb60  test(v2): freeze reliability regressions
4efa8e7  fix(sigenergy): reject incomplete daily production
bfc6b7b  fix(jobs): preserve truthful collection outcomes
3c7d381  fix(sources): enforce canonical source selection
a62422b  fix(ops): remove false delivery and backup success
560fb08  docs(v2): separate V1's rules from V2's, and pin the manifest's contract
a3f82ef  test(v2): two more worker tests were pinning the mock delivery
55886b4  fix(sigenergy): decide the window before spending a login
3024aea  docs(v2): Phase 0 result
```

Branch `v2/phase0-reliability`, a partir de `7815cb7`. Não fundida, não
deployada.

---

## Recommendation

### É seguro avançar para ING-001/ING-002?

## **YES WITH BLOCKERS**

Sim para construir `collection_runs`. Não para tratar o que existe hoje como
fonte operacional principal, e não sem tratar primeiro os dois pontos abaixo.

**Porquê sim.** O propósito desta fase era retirar os sinais que fariam a
próxima fase medir-se contra uma realidade falsa, e isso está feito. Uma
arquitetura de obrigações de recolha é construída *sobre* o que os handlers
dizem ter recolhido: se `success` significar "a execução terminou" em vez de
"os dados chegaram", `collection_runs` regista obrigações cumpridas que nunca
o foram, e a cadeia verificável que ING-001 existe para criar nasce a mentir.
Depois desta fase, `success` significa uma coisa só, o cursor só se move sobre
dias realmente lidos, e os contadores que explicam um resultado sobrevivem até
à linha que um operador lê. Nada disto exigiu abstrações novas — a base
existente estava certa; o que faltava era o contrato ser respeitado — e a
suite completa continua verde, o que diz que a fundação sobre a qual ING-001
assenta não foi abalada.

**Os dois bloqueadores a tratar dentro de ING-001/ING-002, não depois.**

1. **Ownership dos commits, não só da linha do job** (F05/F06). Uma obrigação
   de recolha marcada cumprida por um worker que já perdeu o lease é
   exatamente o falso sucesso que esta fase eliminou, reintroduzido numa
   tabela nova e mais autoritária. Heartbeat, deadline e fencing das escritas
   têm de entrar com `collection_runs`, não como ING-003/ING-004 depois de o
   modelo já estar em produção.
2. **Lock por chave em revisão e cursor** (F13). `collection_runs` multiplica
   os escritores concorrentes por alvo; o read-modify-write atual só é seguro
   sequencialmente, e nada nesta fase o ensaiou sob concorrência real.

**E dois que podem seguir em paralelo, mas devem ser explícitos.**

- Os 145 factos Sigenergy errados estão diagnosticados, não reparados. Se
  `collection_runs` for materializado retroativamente sobre eles, marcará
  como cumpridas obrigações cujos valores estão errados. Reparar antes, ou
  materializar só a partir da data da correção — mas decidir, não deixar
  acontecer.
- O cursor Sigenergy reclama até 2026-09-07. O serviço corrigido não avança
  para lá do último dia fechado, mas também não recua.

**O que continua a não ser defensável, e não muda com ING-001 sozinho:**
deixar de verificar manualmente os fornecedores. Nenhum restore foi ensaiado,
não há cópia fora deste disco, e o novo caminho de backup só será exercitado
a sério às 03:35 de 2026-09-08. A classificação conservadora do plano —
**Level 1, Shadow** — mantém-se.

### Não avançar sem nova instrução

Esta execução parou aqui, como pedido. `v2/phase0-reliability` está por fundir
e por deployar.
