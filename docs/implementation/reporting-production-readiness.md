# Reporting production readiness

Documento vivo da implementação da pipeline operacional de relatórios.

## Estado inicial

- Aplicação Flask server-rendered, SQLite e APScheduler no mesmo processo.
- `app.py` é uma camada de compatibilidade; a composição principal está em
  `monitoring_board/app_factory.py`.
- O schema é criado de forma idempotente por `ensure_database()` e por funções
  `ensure_*_schema()`. Não existe um framework externo de migrations.
- `assets` representa instalações e contém atualmente `nif` e `kwp`.
- `asset_integrations` liga instalações a IDs externos FusionSolar/Sigenergy.
- `portfolio_groups` e `portfolio_assets` representam portefólios e membros.
- Aliases são persistidos em `asset_aliases`; decisões de mapping são auditadas
  em `portfolio_mapping_events`.
- Não existe ainda uma entidade persistente `customers`.
- Não existe histórico temporal de potência instalada.
- `portfolio_report_runs` contém snapshots configuráveis de portefólio, mas não
  implementa um contrato comum de validação/aprovação para relatórios
  individuais e de portefólio.
- `report_generation_runs`, `report_generated_files` e `report_automations`
  preservam runs, SHA-256, ficheiros e agendamentos.
- `background_jobs` é a queue persistente executada pelo APScheduler.
- A qualidade mensal de produção distingue `complete`, `partial`, `missing`,
  `conflict` e `in_progress`.
- FusionSolar suporta produção histórica, estado e diagnóstico. Sigenergy
  suporta apenas descoberta/estado live (`energyFlow`); potência live não é
  energia e não será integrada para criar kWh.
- Reporting inclui templates/perfis versionados, billing, tarifas,
  disponibilidade, importação HelioScope e modelos financeiros.
- Existe CI GitHub Actions com Python 3.12, lint, compile, smoke imports e pytest.
- Foram encontrados nomes, NIFs e subcontas operacionais hardcoded em
  `monitoring_board/portfolio_reports.py`.

## Decisões de arquitetura

1. Alterações de schema serão aditivas, idempotentes e não destrutivas.
2. Cliente e instalação serão entidades distintas; `assets.customer_id` será
   opcional.
3. Potência histórica será resolvida por período, mantendo `assets.kwp` como
   fallback.
4. Será introduzido um snapshot comum e imutável, referenciando payload,
   template, perfil, billing e fontes congelados.
5. Quality findings terão estrutura (`code`, `severity`, `scope`, `asset_id`,
   `message`, `source`, `remediation`).
6. Fecho mensal e novas rotas serão extraídos do monólito sempre que possível.
7. Automatizações gerarão apenas de snapshots aprovados.
8. Geração e distribuição serão processos separados. Nesta fase não haverá
   envio externo.
9. Todas as avaliações temporais aceitarão uma data/relógio explícito quando a
   decisão dependa do tempo.

## Contratos de compatibilidade

- Não apagar nem recriar bases existentes.
- Preservar `portfolio_report_runs`, downloads e histórico de geração.
- Preservar mappings manuais, aliases, templates e perfis/versionamento.
- Preservar interfaces FusionSolar, Sigenergy, filas e instalação import.
- Não chamar APIs durante validação, aprovação ou geração por snapshot.
- Não inferir energia a partir de potência instantânea.
- Dados já persistidos por seeds antigas permanecem na base.

## Riscos

- O import de `app` executa bootstrap de schema e inicia APScheduler.
- O deployment requer exatamente um processo enquanto o scheduler for
  in-process.
- SQLite exige writes curtos e transações explícitas nas transições críticas.
- Documentação histórica ainda descreve partes da arquitetura anterior.
- A remoção de PII da árvore atual não a remove do histórico Git.

## Plano e progresso

| Fase | Estado | Ficheiros principais |
| --- | --- | --- |
| Auditoria inicial | Concluída | este documento, schema, repositories, testes |
| A — dados hardcoded | Concluída | `portfolio_reports.py`, bootstrap, testes |
| B — clientes e mapping | Concluída | `customer_repository.py`, assets/portfolios |
| C — capacidade histórica | Concluída | capacity repository, assets, relatórios |
| D — snapshots/aprovação | Concluída | reporting snapshots, quality gate, auditoria |
| E — fecho mensal/quality gate | Concluída | blueprint, services, templates, tests |
| F — automatizações | Concluída | automation repository/jobs/UI/tests |
| G — distribuição | Pendente | repository, routes, templates |
| H — Sigenergy/Expertcom | Pendente | quality findings e documentação |
| I — UI/arquitetura/CI | Pendente | navegação, CSS, docs, workflow |

## Bloqueios externos

- Permissão `System history` da Expertcom/Sigenergy.
- Confirmação oficial da unidade de energia histórica.
- Orçamento/rate limit diário Sigenergy.
- Parâmetros e credenciais MQTT.
- Integração real de envio.
- Tornar a repository privada.
- Eventual limpeza do histórico com `git filter-repo`.

Estas ações são manuais e ficam fora desta implementação. Não será executada
limpeza do histórico Git.

## Registo de implementação

### Fase A

- Removida a constante operacional `PORTFOLIO_EXTERNAL_ROWS`.
- Removida a seed automática de portefólios, membros, nomes, NIFs e subcontas.
- Removida a ação UI que voltava a aplicar a seed.
- Mantido `ensure_portfolio_seed_data()` como hook de compatibilidade sem writes,
  evitando quebra de imports externos.
- Bases existentes não sofrem deletes nem updates sobre portefólios existentes.
- Novos ambientes começam sem portefólios e usam importação Excel/CSV ou UI.
- Testes com entidades operacionais foram substituídos por dados sintéticos.
- Verificação focada: 72 testes passaram antes do teste adicional de
  preservação; a suite focada será repetida antes do commit.

### Fase B

- Criada a entidade `customers` e a ligação opcional `assets.customer_id`.
- Backfill idempotente agrupa assets por NIF normalizado sem fundir instalações.
- Assets sem NIF permanecem sem cliente.
- Nomes divergentes no mesmo NIF ativam `review_required` e uma nota de revisão.
- Mapping por NIF único mantém compatibilidade.
- NIF com vários assets restringe candidatos e exige alias, nome, external ID
  ou subconta suficiente para identificar a instalação.
- Identificadores exatos contraditórios com o cliente produzem
  `mapping_conflict`.
- Mappings manuais existentes não são alterados pelo auto-mapping.
- UI de portefólios mostra cliente, instalação, NIF, IDs externos, método,
  confiança e estado do mapping.
- Verificação focada: 68 testes passaram, incluindo schema antigo, idempotência,
  dois assets do mesmo cliente, alias, System ID, conflito e mapping manual.

### Fase C

- Criada `asset_capacity_periods` com validade, potência positiva, motivo,
  origem e auditoria temporal.
- Backfill inicial só é criado quando existem potência válida e uma data segura
  (`mounting_date` ou `start_contract`); nenhuma data é inventada.
- `assets.kwp` permanece como fallback de compatibilidade.
- Períodos sobrepostos são rejeitados e uma expansão fecha o período anterior.
- Um período de relatório atravessado por mais de uma capacidade fica ambíguo,
  em vez de escolher silenciosamente uma potência.
- Reporting de portefólio resolve a capacidade histórica para specific yield e
  ponderação de disponibilidade.
- UI do asset permite consultar, corrigir e adicionar expansões.
- Verificação focada: 70 testes passaram. A primeira execução encontrou apenas
  uma coluna errada na nova fixture, corrigida para o schema real.

### Fase D

- Criado contrato comum `report_snapshots` para scope individual e portefólio.
- Payload, template, perfil, billing, fontes, versões, cobertura, período e
  engine version integram um envelope canónico com SHA-256 determinístico.
- Criados estados de validação/aprovação e `report_snapshot_events`.
- Trigger SQLite impede alterações ao conteúdo congelado após aprovação.
- Validação confirma o hash antes de qualquer transição.
- Aprovação exige validação sem blockers; rejeição exige motivo.
- Seleção de snapshot final devolve exclusivamente snapshots aprovados e com
  hash válido.
- As estruturas históricas `portfolio_report_runs`,
  `report_generation_runs` e `report_generated_files` não foram removidas nem
  reescritas.
- Verificação focada: 59 testes passaram.

### Fase E

- Criada a área operacional `/reporting/monthly-close`, isolada numa blueprint.
- O fecho permite selecionar mês e âmbito, criar snapshots individuais ou de
  portefólio, validar, aprovar, rejeitar e consultar versões anteriores.
- O preview identifica explicitamente rascunhos, dados não aprovados e valores
  financeiros não finais.
- O quality gate usa findings estruturados com código, severidade, âmbito,
  instalação, origem e remediação.
- A obrigatoriedade de billing, tarifa e disponibilidade é derivada das secções
  e métricas do template congelado.
- Produção incompleta/conflitante, mapping, fonte energética, cliente exigido,
  capacidade ambígua e limitações Sigenergy bloqueiam aprovação.
- Disponibilidade e invoice opcionais geram avisos sem transformar subtotais em
  resultados finais.

### Fase F

- O scheduler e a ação `Executar agora` procuram exclusivamente o snapshot
  aprovado do mês fechado anterior, com scope, template e perfil compatíveis.
- A geração valida o hash ao carregar o snapshot e reconstrói template, perfil
  e payload apenas a partir da configuração congelada.
- Sem snapshot aprovado, a execução fica `blocked`, persiste o motivo e não
  cria qualquer ficheiro.
- Runs suportam `queued`, `running`, `completed`, `partial`, `failed`,
  `blocked` e `skipped`.
- `report_automations` mantém o último snapshot e o último motivo de bloqueio,
  sem fixar permanentemente um snapshot mensal.
- A deduplicação usa automatização + snapshot; retries de runs falhados
  continuam possíveis e runs interrompidos preservam a recuperação existente.
