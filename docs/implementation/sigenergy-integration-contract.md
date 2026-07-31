# Contrato de integração Sigenergy

## Fluxo anterior

No histórico Git, autenticação, descoberta, IDs configurados, `energyFlow`,
matching local, snapshots, onboarding e sync convergiam em
`run_sigenergy_check`. O scheduler chamava o mesmo caminho usado pelo botão de
teste e a discovery era uma pré-condição implícita.

Fluxo observado no commit anterior a `7b1627b`:

```text
config/system_ids ou mappings
  -> SigenergyClient.list_systems()
  -> linhas reais de discovery ou linhas sintéticas dos IDs configurados
  -> energyFlow para cada linha
  -> snapshot (mesmo sem asset_id)
  -> matching local / unresolved
  -> last_error global
```

Depois de `7b1627b`, o bypass por `system_ids` foi removido, mas o check passou
a iniciar sempre por discovery. Assim, `code=1201` bloqueava validação direta,
importação e sync.

## Fluxo atual

```text
credentials ──> login ──> estado credentials
discovery   ──> /system ──> inventário (enriquecimento opcional)
access(ID)  ──> energyFlow(ID) ──> inventário + auditoria do ID
mapping     ──> asset_integrations + auditoria
sync        ──> mappings enabled ──> energyFlow(ID) + snapshot por ID
history     ──> mapping + data ──> fact diário + produção diária/mensal
onboarding  ──> pedido remoto explícito + estado formal preservado
report      ──> fonte primária confirmada + mês complete
```

Nenhuma seta entre discovery e access/sync/history/report é obrigatória.

## Responsabilidades

| Camada | Responsabilidade |
| --- | --- |
| `sigenergy_client.py` | HTTP, token, endpoints e parsing do envelope; não conhece mappings nem IDs configurados. |
| `sigenergy_contracts.py` | Enums, dataclasses, validação de ID e classificação central de falhas. |
| `sigenergy_operations.py` | Operações explícitas de credenciais, discovery, acesso e sync. |
| `sigenergy_history.py` | Ingestão direta de um dia e planeamento idempotente de backfill. |
| `sigenergy_mapping.py` | Associação/desassociação auditável, sem selecionar fonte primária. |
| `sigenergy_onboarding.py` | Workflow remoto explícito e idempotente. |
| `repositories/sigenergy.py` | Inventário, mappings, snapshots e erros com scope. |
| `app_factory.py` | Wiring Flask, fila, scheduler e rotas finas. |

## Estado e persistência

`provider_operation_state` guarda o último estado por
`(provider, operation, external_id)` e `provider_operation_events` mantém a
auditoria. As áreas globais usam `external_id=''`; operações por instalação
usam o ID explícito.

`provider_system_inventory` separa:

- `access_status`;
- `operational_status`;
- `sync_status`;
- `data_quality`.

O mapping vem de `asset_integrations` e é apresentado separadamente como
`unassociated`, `associated` ou `disabled`.

`integration_configs.last_error` é compatibilidade legacy. No upgrade, um erro
existente é migrado uma vez para o event store, com o ID inferido apenas quando
o path o permite, e deixa de ser fonte da UI.

As alterações de schema são aditivas. Reverter o código não exige apagar
tabelas ou colunas; as versões anteriores ignoram as novas estruturas. O
rollback operacional restaura o backup SQLite criado antes do deployment e o
commit anterior.

## Auditoria do ID `25062000156`

### Evidência Git

1. `082bc3b4ba88e774087944670242b2dd1dfdce56` introduziu
   `integration_configs.system_ids`, a variável `SIGENERGY_SYSTEM_IDS` e a UI
   correspondente.
2. `038046c462367e199e8f28842052b0ce08fcb65e` fez
   `SigenergyClient.list_systems()` devolver primeiro
   `configured_system_rows(system_ids)`. Cada ID configurado era transformado
   em `{"systemId": ID, "systemName": ID}` sem discovery.
3. Antes de `7b1627b22ded2c8e7591dd10d8a48a8ad2dc5273`,
   `run_sigenergy_check` usava mappings ativos e, na ausência destes,
   `integration_configs.system_ids`. `run_sigenergy_sync` inseria o snapshot
   antes de resolver o asset, pelo que uma linha sintética podia gerar
   snapshots repetidos com `asset_id=NULL`, mesmo sem mapping.
4. `7b1627b` removeu o uso de `system_ids` e as linhas sintéticas, mas manteve
   o fluxo universal dependente de discovery.

### Evidência do backup de produção

O administrador executou o auditor sobre
`before-expertcom-repair-20260730-171225.db`. O backup tinha o SHA-256
`86de6aaf77d8a80a9a3a44eac63ca26955a21a11ea32fd4a4502a6705bcbd20b`,
passou `PRAGMA quick_check` e manteve o mesmo hash antes e depois da auditoria
imutável read-only.

A evidência sanitizada demonstra que:

- existiam exatamente 116 snapshots Sigenergy, IDs 1 a 116, entre
  `2026-07-23T16:44:45` e `2026-07-30T15:12:09`;
- todos tinham `asset_id=NULL` e não existia mapping, registo de produção,
  facto energético ou background job parametrizado com este ID;
- os payloads inicial e final eram byte a byte iguais (SHA-256
  `ff034d0f694e91841f3c8f913b8945999fc1fa225e08c2ca7b5ee4e0b3b0a38b`)
  e continham a identidade sintética
  `systemId == systemName == 25062000156`, objetos realtime/energy-flow vazios
  e um erro de fetch;
- o ID ocorria em 116 linhas de `integration_sync_runs.summary_json` e no erro
  global legacy, mas não num mapping ou job dirigido ao ID;
- `integration_configs.system_ids` já estava vazio neste backup.

### Causa raiz

O fluxo que inseriu os snapshots foi, portanto, o fallback legacy de
`system_ids`, e não discovery, parser de outro provider, mapping, onboarding,
teste ou migração. Sem mapping ativo, `run_sigenergy_check` selecionava os IDs
configurados; `SigenergyClient.list_systems()` não chamava discovery e
`configured_system_rows()` fabricava a linha de identidade; `energyFlow`
falhava e `run_sigenergy_sync()` persistia essa linha antes de resolver o
asset. Execuções agendadas e manuais em background repetiram o snapshot com
`asset_id=NULL`.

Depois de a coluna ter sido limpa, já não é possível reconstruir como o valor
entrou na configuração: o código antigo aceitava a coluna da base (preenchida
pela UI ou pelo seed a partir do ambiente) e também o override runtime
`SIGENERGY_SYSTEM_IDS`. Isto não deixa o fluxo que criou os snapshots
ambíguo; impede apenas atribuir quem introduziu o valor ou qual superfície de
configuração o forneceu. O auditor regista esta distinção como
`configured_value_source=database_or_environment_not_recoverable`.

O módulo `monitoring_board.sigenergy_legacy_audit` recolhe a evidência em modo
imutável e reconhece este fingerprint persistido mesmo quando o valor da
configuração legacy já foi removido antes do backup.

Não existe cleanup genérico por ID. Qualquer limpeza futura exige backup,
dry-run, evidência de origem e alvo exato.
