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

Este histórico identifica o caminho de código capaz de selecionar e persistir
o ID. A atribuição factual do valor `25062000156` a configuração, mapping, job,
teste ou outra origem depende da evidência do backup
`before-expertcom-repair-20260730-171225.db`.

O módulo `monitoring_board.sigenergy_legacy_audit` recolhe essa evidência em
modo imutável: todas as tabelas/colunas com o ID, contagem e intervalo dos
snapshots, `asset_id`, runs/jobs/mappings/configuração, e shape/hash sanitizado
dos payloads de fronteira. A causa só deve ser declarada fechada depois de
guardar e rever esse JSON.

Não existe cleanup genérico por ID. Qualquer limpeza futura exige backup,
dry-run, evidência de origem e alvo exato.
