# Nem-sei V2 decisions

- Flask and direct SQLAlchemy are retained; Flask-SQLAlchemy is not used.
- PostgreSQL is the only V2 operational database. SQLite is limited to frozen
  V1 and the V1 importer's read-only source/fixtures.
- Alembic is the only schema migration mechanism; web does not auto-migrate.
- Runtime data and Git worktrees are isolated from V1.
- External capabilities default to deny. As of 2026-08-31 that is true of
  `notifications` as well: the switch existed from the start and was read by
  nothing, so `NEMSEI_V2_NOTIFICATIONS=false` delivered 316 real Telegram
  messages. See `PIPELINE_HEALTH.md` for the switch hierarchy it now sits at
  the top of.
- Jobs are at-least-once and all future side effects require explicit
  idempotency strategies.
- A deploy declares its components. `deploy/v2_deployment_components.json`
  names the compose files a canonical deploy carries and asserts what must
  reach the running containers; forgetting a `-f` is no longer possible, and
  no longer a silent way to turn an automation off. See `PIPELINE_HEALTH.md`.
- Scheduler health and execution health are separate claims and are never
  merged into one status. "The scheduler enqueued a job" is not "the work
  succeeded"; conflating them hid a week of daily FusionSolar failures behind
  a green row.

## Canonical device level (M1)

Justification and V1 evidence: `DEVICE_MODEL.md`.

- `assets` remains the physical installation. No `Plant` tier is introduced
  between `organizations` and `assets`, because V1 has exactly one device tier
  below the installation and every one of its 325 devices is an inverter.
- `devices` is a child of `assets` with an open `device_kind` vocabulary
  (`inverter`, `meter`, `datalogger`, `string_box`) and an optional
  `parent_device_id`. Only `inverter` is populated by migration; the remaining
  kinds are enabled by adding rows, not by adding tables.
- Canonical device identity is the hardware serial number, model and rated
  power. Provider identifiers (`external_device_id`, `dev_dn`, station codes)
  are provider evidence and live in the mapping layer only.
- Serial numbers are unique per asset, not globally. A duplicate within an asset
  is a review condition, never an import failure or an automatic merge.
- `asset_provider_mappings` is extended with a nullable `device_id` and accepts
  `resource_kind='device'`; there is no separate device mapping table. Existing
  plant claim, supersede and conflict rules are unchanged. A device mapping
  records the plant mapping it was discovered under.
- Every device-level fact references `devices.id`. No V2 fact table keys a
  device on a provider identifier string, which is V1's pattern in
  `inverter_power_samples` and `inverter_availability_daily`.
- `provider_device_configuration_history` is not imported: its 325 rows are all
  single-version and open, so there is no device history to migrate.

## Future side-effect idempotency contracts

Every side-effecting handler must persist an idempotency key before invoking an
external system, define replay behavior, and record an uncertain external
outcome for reconciliation rather than blindly repeating the call.

| Future operation | Idempotency key | Replay behavior | Uncertain external outcome |
| --- | --- | --- | --- |
| Production/history import | provider, external reference, observed period, fact version | Durable natural-key upsert | Re-fetch and reconcile by external reference/period |
| Report generation | approved snapshot or artifact specification/version | Return the existing immutable artifact | Retain pending artifact state and regenerate only from the same snapshot |
| Report distribution | snapshot, artifact, recipient, channel, template version | Return the existing delivery record | Query provider delivery status where available; otherwise mark for manual reconciliation |
| Notification | business event, destination, template version | Reuse the durable delivery record | Mark delivery uncertain; never silently resend |
| Provider mutation | persisted operation key and provider idempotency key | Return recorded provider outcome | Query provider operation/status before a retry; require manual reconciliation if unavailable |

## Reporting inputs (M5, Fatia B)

Evidence: V1's own `production_records`, `asset_tariffs`, `asset_billing_configs`
and `assets`, surveyed read-only on 2026-08-19.

- **The canonical energy vocabulary is five metrics, not one.**
  `production_facts.metric_kind` accepts `production_energy`, `self_use_energy`,
  `export_energy`, `consumption_energy` and `grid_import_energy`. Each is a
  signal a provider states; none is stored as a derived value.
- **No metric is ever derived from another.** The identity
  `production = self_use + export` holds exactly on 40 303 of 41 503 FusionSolar
  daily rows, and does **not** hold for Sigenergy, where a battery absorbs the
  difference. Deriving would have invented a number for every plant that stores
  energy. The identity is used only to reject the impossible: a row whose export
  exceeds its production is persisted with `quality='invalid'`, carrying the
  value it claimed, rather than being dropped or silently corrected.
- **Live reads and historical import are gated separately.** The FusionSolar
  adapter still accepts only the operator-verified `PVYield` for live calls.
  The additional metrics enter V2 through the V1 importer, which reads payloads
  V1 already stored, and every fact records `origin: v1_production_records` in
  its metadata. Verifying the live contract for the other signals is separate
  work and does not block reporting.
- **Tariffs and billing configuration are temporal, and the database enforces
  it.** A GiST exclusion constraint refuses two rows covering the same day for
  one asset. V1 permitted the overlap and resolved it by taking the newest row,
  which is how a customer can be billed at a price nobody chose. This is the one
  place a contrib extension (`btree_gist`) was added, because the guarantee
  belongs in the database rather than in a code path someone can forget.
- **Prices are `Numeric(28, 18)`.** V1 stores them as TEXT and parses through
  `Decimal`; its one real billing row carries seventeen decimal places.
- **Contract attributes are free text, deliberately.** `contract_type`,
  `asset_type`, `coverage_type` and `sell_to` are copied as V1 wrote them,
  because normalising would lose distinctions its operators made ("EPC (O&M)",
  "ESCO BUYOUT"). Report type is resolved from them by the already-ported rule.
- **A defaulted report type is reported as defaulted.** `detect_report_type`
  answers EPC both when it reads "EPC" and when it reads nothing.
  `report_type_resolved` and `report_type_source` separate the two, because
  sending an ESCO customer an EPC document is a commercial error, not a
  formatting one.

## Portfolios (M8)

Evidence: V1's `portfolio_groups`, `portfolio_assets`, `portfolio_mapping_events`
and `portfolio_report_profiles`, surveyed read-only on 2026-08-19 and recorded in
`PORTFOLIOS.md`. V1's screens were deliberately not consulted.

- **Portfolios are flat.** There is no parent column, and a test asserts none
  appears. V1 has no nesting either.
- **A member is not always an asset.** 23 of V1's 80 members carry a name, a NIF
  and a sub-account but no installation. `asset_id` is nullable and
  `resolution_state` distinguishes `resolved`, `unresolved` and `placeholder`.
  Dropping them would have lost real evidence and silently shrunk two portfolios.
- **Membership is temporal and may overlap between portfolios.** Two assets and
  four NIFs belong to both of V1's portfolios. A GiST exclusion constraint stops
  one asset being counted twice inside the *same* portfolio, which would
  double-weight a plant in every total.
- **Nothing is matched fuzzily.** An exact NIF match names a customer, not an
  installation, so it is reported as a candidate for an operator to confirm.
  `resolve_member_to_asset` is the only path to `resolved` and it records who
  decided.
- **Country, region and provider are rule attributes and UI filters, never
  structure.** A rule with no values selects nothing rather than everything.
- **Snapshots freeze the exact list**, append-only by trigger, so a report keeps
  naming what it covered after the portfolio changes.
- **The aggregate reuses the individual reporting.** `PortfolioDataset` builds
  one `ReportingDataset` per member and sums it; `portfolio_dataset_members`
  stores the per-asset dataset id, so a disagreement between a portfolio total
  and an asset's own report is a bug rather than a difference of method.
- **A partial total says partial.** A portfolio of 20 with 18 reporting shows the
  sum of the 18 and a coverage of 18/20. A smaller total that looks complete is
  the most expensive wrong number a portfolio report can carry.

## The monthly reporting workflow and the top-level Reporting UI (2026-08-19)

- **A run has no stored "draft" state.** Validating coverage is a read against
  data that already exists (`freeze_snapshot` + `build_portfolio_dataset`,
  both idempotent); a `portfolio_report_runs` row only starts existing once an
  operator actually generates it. `generated -> reviewed -> approved`.
- **Regenerating before approval is expected, not an error.** It replaces the
  run's members against current facts and resets a `reviewed` run back to
  `generated`, because a review is a statement about the numbers it was shown.
- **An approved run is locked by a database trigger**, not only by the service
  layer — the same append-only guarantee `report_snapshots` and
  `portfolio_snapshots` already give the records beneath it, extended to the
  decision itself and to which members it covered.
- **A member with zero production facts is `blocked` with a stated reason,
  not generated with an empty document.** Partial coverage, a missing tariff,
  a defaulted report type still generate — those are exactly what
  `unavailable_fields` and the coverage counts exist to surface.
- **`snapshot_dataset`'s digest excludes `payload["dataset_id"]`.**
  `ReportingDataset` is never deduplicated, so two builds of the same
  unchanged facts get two different row ids; hashing that id meant "re-freezing
  identical input reuses it" was never actually true for a real payload. The
  row id stays in the *stored* payload for provenance and is excluded only
  from what decides whether a new snapshot is needed.
- **The Reporting UI is one blueprint (`reporting_bp`) reading what already
  exists.** It computes nothing: individual generation goes through
  `assemble_asset_report` + `snapshot_dataset`, the same path the portfolio
  workflow and the golden tests use. Downloads render on request from the
  frozen `payload_json`, rehydrated through `rehydrate_snapshot_payload`.

## Device status facts, Fatia 1 of M7 (2026-08-20)

- **Anchored on `device_id`, not `provider_mapping_id`.** Every one of the 325
  device-scoped provider mappings the M1 import created sits at
  `pending_review` on a disabled, credential-free legacy connection — none is
  `active`. A table requiring a usable mapping, the way `production_facts` and
  `monitoring_observations` do, could not accept a single row today. `device_id`
  is what M1 already resolved for every device independent of any provider
  connection being usable, so it is the identity this table is keyed on
  instead.
- **A new table, not an extension of `production_facts`/`monitoring_observations`.**
  The original proposal considered extending one of them with a nullable
  `device_id`. Neither fits: `ProductionFact` represents energy summed over a
  period (`period_start`/`period_end`), and a device reading is a point-in-time
  instant; `MonitoringObservation`'s `condition` enum
  (`operational`/`warning`/`fault`/`offline`/`unknown`) has no honest mapping
  for V1's `standby`. `device_status_facts` keeps V1's own four-value
  vocabulary rather than force-fitting it.
- **The classification is ported code, not re-derived.** V1's raw
  `inverter_state` codes are provider bitmask values with no documented
  meaning beyond what V1's own operators encoded in
  `classify_fusionsolar_inverter_availability`. That function's three state
  sets are copied into `diagnostics/rules.py` verbatim and pinned by a golden
  test against every real state code V1 ever saw, rather than the availability
  status being re-derived or guessed at.
- **`communication_status` is not imported.** It reads `"recent"` on all
  51 289 real rows — checked by a test, not assumed — so "última comunicação"
  is answered by `observed_at` itself.
- **`inverter_power_samples` (54 593 rows, one day, power only) is not
  imported in this slice.** `device_realtime_snapshots` already covers status,
  power and energy over a much wider window; importing both would duplicate
  evidence without answering a new question.


## Huawei SCADA, receção direta (2026-08-25)

- **Um provider novo, não uma variante do FusionSolar.** Partilham o fabricante e
  nada mais: sem conta, sem credencial, sem chamada de saída, e a identidade é o
  número de série que o dongle anuncia. Enfiá-lo no adaptador do FusionSolar
  obrigaria esse adaptador a ter dois modelos de identidade e dois modelos de
  erro.
- **A identidade é o número de série anunciado, e nada mais.** Não existe caminho
  no código que mapeie um dongle pelo endereço de onde ligou. Um série
  desconhecido vai para `huawei_scada_pending_dongles` e a sessão fecha. A razão é
  concreta: NAT editado, lease de DHCP, mudança de ISP, ou duas centrais atrás do
  mesmo endereço público atribuiriam a produção de um cliente a outro.
- **Processo próprio, não uma thread do gunicorn.** O web worker pode reiniciar ou
  multiplicar-se; cada um desses eventos ou disputa a porta ou abre um segundo
  listener. Além da porta, um advisory lock do PostgreSQL sobre o id da ligação
  apanha o caso que a porta não apanha: um segundo listener noutra máquina.
- **`huawei_scada_power_samples` guarda potência, e as colunas trazem a unidade no
  nome.** Não é uma segunda tabela de energia. A conversão para kWh é explícita,
  documentada e marcada como estimativa.
- **As tabelas vivem no pacote da integração, não num domínio canónico.** Uma
  linha é um bloco de registos Modbus com número de série e endereços; não existe
  conceito neutro de "amostra de potência instantânea" na V2, e inventar um para
  albergar uma forma que só este provider produz seria dívida de esquema. O que é
  canónico — estado corrente e energia diária — é escrito em
  `monitoring_observations` e `production_facts`, no vocabulário deles.
- **A recusa do `unit=1` é evidência, não avaria.** `0x83`/`0x04` é descodificado
  com nome, guardado na sessão, e nunca interrompe a recolha do agregado.
- **O que não foi verificado recusa-se a correr.** Unidade de potência e registo
  de produção são obrigatórios; convenção de sinal da rede e derivação de
  autoconsumo são opcionais e, sem elas, as métricas correspondentes não são
  escritas. Mesma disciplina do contrato do `PVYield` e do histórico Sigenergy.
- **O rollup respeita a política de origem.** `build_dataset` soma os factos de
  todos os mappings de um asset, por isso escrever à revelia da política
  duplicaria a produção de uma central que também tenha FusionSolar ou Sigenergy.
- **O rollup não escreve `IntegrationHealth` nem abre `SyncRun`.** Não sincroniza
  nada: lê linhas que a base já tem. Marcar a ligação como saudável a partir de um
  rollup bem sucedido mascararia um listener morto há dias.

## Installation, Contract scopes, WorkOrder (2026-09-02)

O pedido do produto ("Solcor O&M") trazia uma hierarquia `Organization →
Installation → Asset` com `Installation` como entidade nova. A primeira análise
propôs não a criar, com o argumento de que `AssetProviderMapping` já resolvia a
única multiplicidade observável nos dados (267 assets, 1 com dois providers).
O pedido corrigiu essa análise: a razão para `Installation` nunca foi resolver
mappings — é separar o local físico/operacional da central técnica canónica,
uma distinção que a proveniência de provider não substitui. Decisão final:

- **`Installation` existe, incremental.** Migração `0031`: tabela `installations`
  mais `assets.installation_id` nullable. Nenhuma linha se move; nenhuma fact
  table (`production_facts`, `device_status_facts`, `asset_provider_mappings`)
  muda de FK. `installations/service.py::backfill_installations_from_assets` cria
  uma Installation por Asset, à parte da migração — mesma divisão de trabalho
  que os importadores V1 já usam (schema na migração, dados num script
  idempotente e revisto).
- **Coordenadas movem-se para `Installation`.** `Asset.latitude`/`.longitude`
  (desde a `0001`, sempre NULL em produção) ficam por compatibilidade, nunca
  mais escritas. `monitoring/production_window.py` não mudou nada: já era uma
  função pura sobre `(latitude, longitude)`, nunca sobre um `Asset` — essa
  decisão pagou-se sozinha no dia em que o dono das colunas mudou.
- **`AssetServiceContract.service_kind` alarga para `esco`/`monitoring`.** Cada
  função de leitura/fecho em `contracts/service.py` passou a ser âmbito por
  `service_kind` — antes disso existia só `om` e nada precisava de filtrar.
  Sem o âmbito, registar o primeiro contrato ESCO de uma instalação fecharia
  silenciosamente o O&M aberto, e `om_status_map` contaria um período ESCO
  como cobertura O&M.
- **`ex_asset_service_contracts_no_overlap` (0027) tinha o mesmo defeito ao
  nível da base de dados**, e mais grave: excluía por `(asset_id, daterange)`
  sem `service_kind`, por isso a base recusaria um contrato ESCO datado dentro
  de uma janela O&M aberta — o caso normal que esta mudança existe para
  permitir, não uma exceção. Apanhado por
  `test_recording_an_esco_engagement_does_not_close_an_open_om_one` a falhar
  com `ExclusionViolation`, não por inspeção. Corrigido na `0032`, adicionando
  `service_kind WITH =` à exclusão.
- **`AssetBillingConfig.contract_id`** (nullable) liga uma configuração
  comercial ao contrato que a determina, sem duplicar o mecanismo temporal que
  a tabela já tinha nem mudar `resolve_billing_config`, que continua a resolver
  por `(asset_id, on)`.
- **`WorkOrder`/`Visit` são entidades novas; `DiagnosticIncident` não muda.**
  O "Incident" formal pedido já existe — `DiagnosticIncident` já tem
  deduplicação por `(rule_code, asset_id, device_id)`, `handling_state`
  (new/acknowledged/investigating/visit_scheduled/done) e notas de auditoria.
  O que faltava era só `WorkOrder`↔`Incident` N:N e `Visit`. `WorkOrder` liga-se
  a `Installation`, não a `Asset` — um trabalho é despachado a um sítio, não a
  uma central. Vocabulário de `work_type`/`status` dimensionado pela evidência
  real da V1 (13 tickets, 4 estados realmente usados de 6 declarados,
  `field_route_plans` com zero linhas alguma vez), não pelo esquema antigo.

  **Gap conhecido, deliberadamente não fechado nesta sessão**: `findings.py`
  não tem limiar de duração nem consulta `production_window`, por isso uma
  regra `severity="info"` (ex.: `partial_device_coverage`) criaria hoje um
  incidente na primeira ocorrência — contra o pedido explícito
  "alarme informativo não cria incidente". Não aconteceu em produção (0 dos
  1100+ incidentes reais é `info`), mas é uma lacuna real do motor, não um
  caso hipotético, e mexer no motor que classifica os incidentes reais exige a
  sua própria passagem cuidadosa contra a população real, não uma alteração
  apressada a seguir a um bloco de schema.

## Timeline como projeção, e o gap das regras de incidentes fechado (2026-09-02)

Sem migração em nenhum dos dois blocos.

- **`timeline/service.py` é só leitura**, sem tabela nova. Junta
  `monitoring_observations`, `device_status_facts` (colapsado a transições,
  não uma linha por sondagem), `diagnostic_incidents`, `incident_notes`,
  `work_orders` e `visits`, por `Installation`. `Visit.visit_date` é uma data,
  não uma hora, e o evento correspondente fica marcado `precision: "day"` e
  ordena à meia-noite UTC — nunca inventa uma hora que os dados não têm, o que
  tem uma consequência real e testada: um evento de dia único pode ordenar
  *antes* de um evento com hora do mesmo dia.

- **A análise das regras existentes contra "janela produtiva" mostrou que
  nenhuma precisava dela.** As regras de comparação entre pares
  (`power_disparity_among_peers`, etc.) já só comparam dispositivos com
  produção real acima de um limiar — de noite, sem pares acima do limiar, a
  regra não corre. Um inversor em repouso já lê `standby`, não `unavailable`,
  na camada de classificação (`diagnostics/rules.py`), antes deste módulo ver
  o dado. A peça que faltava de facto era a única que o próprio pedido nomeia
  sem existir ainda: **`zero_production_in_productive_window`** — produção
  nula, evidenciada (nunca por ausência de leitura, isso já é
  `device_no_history`/`stale_reading`), em todos os equipamentos com leitura,
  há mais de 30 minutos, dentro do período produtivo. Como exige leituras
  frescas para sequer avaliar, é auto-limitada à sondagem em tempo real que
  hoje só existe para o asset da canary — não pode disparar para as 265
  instalações sem monitorização ao vivo.

- **Persistência por severidade, fechada em `diagnostics/incidents.py`**:
  `info` nunca persiste (`partial_device_coverage` nunca disparou em
  produção, mas o caminho existia e teria criado incidente no dia em que
  disparasse). `warning` espera que o `active_since` da própria finding — já
  andado a partir de histórico real, nunca "desde que o avaliador arrancou" —
  tenha pelo menos 15 min, `WARNING_PERSISTENCE_THRESHOLD`. `critical`
  continua imediato, como sempre foi.

  **Um defeito de design apanhado pelo teste do `device_no_history`, não por
  inspeção**: a proteção `since = finding.active_since or now` fazia o tempo
  decorrido ser sempre zero para uma finding sem evidência de duração
  nenhuma (`active_since=None`) — nunca haveria um `now` anterior para
  comparar, por isso nunca cruzaria os 15 minutos, em avaliação alguma,
  para sempre. Corrigido: sem evidência de duração, trata-se como acionável
  já, tal como `critical` — a proteção existe para filtrar picos transitórios
  usando evidência real; sem essa evidência não há o que medir.

- **Um segundo problema, este só no teste, não no código de produção**: ao
  reescrever `test_an_asset_level_finding_opens_one_incident_with_a_null_device_id`
  para usar `plant_offline` em vez do agora-nunca-persistente
  `partial_device_coverage`, o teste falhava por faltar `session.flush()`
  antes de `evaluate_and_persist_incidents` ler de volta, na mesma sessão
  `autoflush=False`, uma linha `MonitoringObservation` ainda só adicionada,
  nunca gravada. Corrigido no teste.

- **Recorrência é uma consulta, nunca uma coluna.** `recurrence_count`/
  `is_recurring` contam episódios (`DiagnosticIncident.opened_at`) da mesma
  identidade num`RECURRENCE_WINDOW` de 24h, terminando na abertura do
  próprio incidente — nunca "agora", para que ser recorrente não deixe de o
  ser só porque o tempo passou e a janela deslizou. Mesma razão que
  `contracts.om_status` deriva em vez de guardar: o facto (`opened_at` de
  linhas passadas) já existe, e uma segunda coluna derivada convida a
  divergir dela.

Verificado com uma leitura sem escrita (transação revertida) contra uma
cópia restaurada da produção real: 136 assets avaliados, 445 incidentes
abertos confirmados, **0 incidentes novos** (nenhuma instalação real tem
ainda coordenadas importadas, por isso a nova regra de produção nula não
pode disparar), **0 diferidos**, e 1 dos 445 incidentes abertos qualifica
como recorrente hoje. Cópia descartável, sem escrita real.

## UI operacional, e o que não se calculou (2026-09-02)

- **`Installation` continua ancorada em `Asset` no read-model da UI, nunca o
  contrário.** `installations` está vazia em produção — o backfill existe e
  está testado desde o Bloco 1, mas o deploy continua deliberadamente por
  fazer. `web/installation_queries.py` lê sempre por `Asset.id` e trata
  `Installation` como enriquecimento opcional: em falta, mostra "sem
  Instalação associada" em vez de esconder a funcionalidade ou rebentar.
- **Avaria / comunicação / cobertura são três números em toda a UI nova**
  (`diagnostics/incident_categories.py`), nunca um só. Resposta direta ao
  achado que abriu esta sessão: 94% de 447 incidentes reais eram
  `stale_reading`/`device_unknown_status` — falta de monitorização, não
  avaria.
- **Sem segundo algoritmo de prioridade.** A meio da sessão confirmou-se que
  outro contexto está a construir `notifications/priority.py`
  (`score_episode`, `PriorityInputs`) para o Telegram e o resumo matinal.
  `web/operational_priority.py` é o adapter — uma ordenação mínima e
  explicitamente temporária, construída só com regras que já existem e já
  têm teste (`contracts.priority.service_priority`, o próprio split de
  categorias), nunca inventando um peso novo. Nunca inclui cobertura na
  ordenação: GOAL.md é explícito que uma pilha de lacunas de monitorização
  não pode subir uma instalação na lista de atenção.
- **"Impacto económico das falhas" foi ligado, não recalculado.**
  `notifications/impact.py` do outro contexto (`estimate_energy_impact`,
  `estimate_financial_impact`) entretanto ficou commitado (`f64cd7a` e
  seguintes) — produção perdida a partir da última potência real conhecida e
  da janela produtiva, nunca de uma capacidade nominal, com a mesma
  distinção None/zero que este código já pratica noutros sítios. A tab
  Operação passou a chamar essa função por incidente (`_incident_impact`),
  nunca por `coverage` — lacuna de monitorização não tem produção perdida
  associada, só avaria/comunicação têm. A tab Performance já não mostra
  "ainda não disponível": aponta para a Operação, onde o número por
  incidente já existe, em vez de duplicar um agregado ali. Construir uma
  segunda versão do cálculo continua a ser exatamente o erro que a instrução
  sobre prioridade já tinha avisado — por isso a tab chama a função do outro
  contexto, não reimplementa.
- **"Esperado vs Real" foi calculado, porque não há sobreposição nenhuma**:
  compara a produção medida do mês com `FinancialModelMonth
  .expected_production_kwh` do modelo confirmado — uma pergunta diferente de
  "quanto custou este incidente", e uma que os dados de produção (2 modelos
  confirmados, 267 assets) já respondem para quem tem modelo.
- **`confirmed_financial_model` deixou de estar escrito três vezes.** A
  mesma query (`FinancialModel` do asset, `status='confirmed'`, versão mais
  alta) existia em `reporting/service.py`, `web/commercial_routes.py` e
  `reporting/datasets.py`, sempre para a mesma pergunta com propósitos
  ligeiramente diferentes (quem substituir vs. de onde ler o esperado).
  Extraída para `reporting/commercial.py`, os três sítios existentes
  passaram a chamá-la; o quarto uso (esta tab) não escreveu uma quarta
  cópia. Os três conjuntos de testes que já cobriam esses três sítios
  continuam verdes sem alteração — o comportamento não mudou, só deixou de
  estar em três sítios.

## Produção, ESCO e Planeamento (2026-09-03)

Fase 3 do plano original (`lista de instalações; detalhe; incidentes;
trabalhos; planeamento; dashboard`) e o início da Fase 4 tinham ficado
incompletas: a navegação já listava "Planeamento", "Produção" e "ESCO" como
itens próprios desde o bloco da UI operacional, mas os três apontavam para
nada — só existiam as páginas por instalação. Este bloco fecha esse gap,
escolhido em vez de ligar o `score_episode` real ao dashboard ou construir
`ModuleGroup`: os outros dois cruzam área do outro contexto (episódios de
notificação em WIP pesado; uma migração nova quando já há uma migração do
outro contexto por commitar na mesma branch), e este não precisa de tocar
em nenhum dos dois.

- **"Instalação ESCO" é a mesma pergunta que `calculate_billing` já faz.**
  `esco_queries.py` filtra por `AssetBillingConfig.report_type == "esco"`
  em vigor hoje — não por `contracts.priority.commercial_family` (de quem
  é o dinheiro em risco, usado para priorizar incidentes) nem por
  `reporting.commercial.report_type_for` (a adivinha a partir do texto
  livre do contrato, usada só onde ainda não existe configuração de
  faturação para perguntar em vez disso). As três respondem perguntas
  diferentes; esta página segue a que realmente decide o ramo ESCO do
  cálculo, para nunca mostrar receita Solcor que o próprio motor de
  faturação não calcularia.
- **Nem `_performance_tab` nem esta página inventam um segundo motor de
  faturação.** `calculate_billing`/`EnergyBreakdown`/`billing_config_from`
  já existiam (Bloco C); a única peça nova é buscar as cinco métricas de
  energia à escala do parque numa query em vez de uma por instalação (ver
  abaixo) e passar o resultado pelo mesmo cálculo puro.
- **`web.series.fleet_metric_totals` é `portfolio_monthly_series`
  generalizada.** A mesma redução `DISTINCT ON (provider_mapping_id,
  source_fact_key) ORDER BY source_revision DESC` que já existia para o
  gráfico mensal do portfolio, parametrizada por `metric_kind` e agrupada
  por instalação em vez de por mês. Sem isto, a página ESCO faria
  `energy_balance` (5 queries) por cada instalação ESCO; com isto, são 5
  queries para o parque inteiro, e o cálculo por instalação passa a ser
  aritmética pura sobre o resultado. A página Produção usa a mesma função
  para "hoje" e para "este mês", pela mesma razão -- 267 centrais é
  precisamente a escala em que o padrão de uma query por instalação deixa
  de fazer sentido, mesmo sendo o padrão certo para uma página de uma
  instalação só.
- **"Esperado" ao nível do parque soma só quem tem modelo confirmado.**
  Uma instalação sem modelo não entra no total esperado como zero -- fica
  simplesmente fora da soma, e a tabela de desvio por instalação separa-a
  claramente ("sem modelo") de uma instalação que reportou zero produção.
  Confundir as duas inflacionaria ou destruiria o desvio agregado consoante
  quantas centrais ainda não têm modelo.
- **Planeamento não é uma partição de Trabalhos.** Os cinco grupos do
  dashboard original (esta semana / atrasados / bloqueados / sem data /
  próximos) são perguntas independentes sobre o mesmo trabalho em aberto,
  não mutuamente exclusivas -- um trabalho atrasado e bloqueado por
  material aparece nas duas listas, de propósito: escondê-lo de uma porque
  já apareceu na outra perderia exatamente o facto que explica porque
  continua parado. `work_order_queries.planning_page` filtra uma vez
  (estado não terminal) e particiona depois em memória, nunca cinco
  queries independentes que poderiam divergir sobre o que "em aberto"
  significa.
