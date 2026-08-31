# Nem-sei V2 decisions

- Flask and direct SQLAlchemy are retained; Flask-SQLAlchemy is not used.
- PostgreSQL is the only V2 operational database. SQLite is limited to frozen
  V1 and the V1 importer's read-only source/fixtures.
- Alembic is the only schema migration mechanism; web does not auto-migrate.
- Runtime data and Git worktrees are isolated from V1.
- External capabilities default to deny.
- Jobs are at-least-once and all future side effects require explicit
  idempotency strategies.

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
