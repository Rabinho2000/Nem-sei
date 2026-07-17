---
quick: 2
mode: quick
title: "Adicionar AGA, Pires Lourenço 1, Alto dos Moinhos e Alirações ao pipeline de intervenções"
status: planned
autonomous: true
files_modified:
  - monitoring_board.db
---

# Quick Task 2: Adicionar intervenções operacionais ao pipeline

## Objective

Registar cinco intervenções abertas no pipeline real da plataforma, preservando todos os detalhes fornecidos e tornando claras as necessidades de material, os próximos passos e a incerteza sobre o segundo lote de Alto dos Moinhos.

## Context

- O pipeline de intervenções é persistido na tabela SQLite `tickets`, associada obrigatoriamente a `assets` por `tickets.asset_id`; a interface correspondente é a rota `/tickets` em `monitoring_board/app_factory.py`.
- A base ativa local é `monitoring_board.db`, resolvida por `monitoring_board/runtime.py` quando `DATA_DIR` não está definido. Antes de qualquer escrita, confirmar o caminho efetivo através de `monitoring_board.runtime.DB_PATH` e trabalhar apenas nessa base.
- Correspondências já confirmadas na base: `AGA (EPC)` (id 1956), `Pires Lourenço 1` (id 1853), `Alto dos Moinhos Lote 11` (id 1962) e `Alirações` (id 2065). Resolver novamente por nome no momento da execução, sem codificar os IDs.
- Assunção controlada: `Alto dos Moinhos Lote 1` corresponde à única central existente com esse nome-base, `Alto dos Moinhos Lote 11`. Como o utilizador não sabe qual é o lote dos dois painéis com cabos rasgados, associar essa intervenção à mesma central, mas usar `installation_ref = "Lote por confirmar"` e repetir a incerteza nas notas. Não criar nem renomear assets.
- Não existem atualmente tickets associados a estas quatro centrais. Ainda assim, a execução deve voltar a verificar duplicados abertos imediatamente antes de inserir.
- A base e os backups são artefactos de runtime e não podem ser adicionados ao Git.

## Tasks

### Task 1: Proteger a base e validar os destinos das intervenções

**files:**
- `monitoring_board.db`
- `backups/` (artefacto de runtime, não versionado)

**action:**
- Importar `DB_PATH` de `monitoring_board.runtime`, confirmar que o ficheiro existe e abrir a base com `sqlite3`.
- Criar um backup consistente e datado antes da escrita, usando a API `sqlite3.Connection.backup()` para um ficheiro sob `backups/`; não copiar diretamente uma base possivelmente ativa.
- Numa leitura antes da transação, resolver exatamente os quatro assets pelos nomes acima e abortar sem inserir nada se algum estiver ausente ou se qualquer nome devolver mais de uma correspondência.
- Consultar tickets não fechados dessas centrais e tratar como duplicado uma intervenção com o mesmo asset e finalidade inequívoca (painel/string AGA, oito painéis Pires Lourenço, limpeza Alto dos Moinhos, cabos rasgados Alto dos Moinhos, ou meter/TIs Alirações). Se existir, preservá-la e incluí-la na verificação final em vez de criar uma segunda.

**verify:**
- Confirmar que o backup existe, abre em modo read-only e passa `PRAGMA integrity_check` com resultado `ok`.
- Confirmar que a resolução produz exatamente um id para cada um dos quatro nomes e listar os eventuais tickets reutilizados como duplicados.

**done:**
- Existe um backup consistente recuperável, os quatro destinos estão resolvidos sem ambiguidade e a lista de inserções necessárias está definida sem duplicar intervenções abertas.

### Task 2: Inserir e verificar as cinco intervenções numa única transação

**files:**
- `monitoring_board.db`

**action:**
- Inserir, numa única transação parametrizada, apenas as intervenções ainda inexistentes. Usar `status = "Aberto"`, datas de criação/atualização ISO no fuso `Europe/Lisbon`, sem inventar data planeada, data limite, responsável ou duração diferente do default da plataforma.
- Registar os seguintes conteúdos, mantendo os factos nas notas e as ações nos campos operacionais:
  1. `AGA (EPC)`: título `Substituir painel partido e repor string`; `urgency = "Alta"`, `work_type = "String"`, `material_status = "Bloqueado"`, `installation_ref = "Cobertura / string encurtada em 1 painel"`; notas sobre a fuga de corrente e string encurtada; `next_action` para encontrar um painel com características compatíveis com o ZNShine 460 W indisponível; `planning_notes` a exigir validação elétrica/mecânica do substituto antes da montagem.
  2. `Pires Lourenço 1`: título `Substituir 8 painéis danificados e reativar string`; `urgency = "Alta"`, `work_type = "String"`, `material_status = "Bloqueado"`, `installation_ref = "1 string desligada"`; notas sobre os danos da tempestade e os oito módulos antigos de 375 W indisponíveis; `next_action` para encontrar substitutos compatíveis e planear a reposição da string.
  3. `Alto dos Moinhos Lote 11`: título `Agendar limpeza dos painéis`; `urgency = "Media"`, `work_type = "Limpeza"`, `material_status = "Sem material"`; notas de que a gestão já não é da Urbicare e de que a gestora pediu o agendamento; `next_action = "Contactar filomenakellen@gmail.com para agendar a limpeza"`.
  4. `Alto dos Moinhos Lote 11`: título `Substituir 2 painéis com cabos rasgados`; `urgency = "Alta"`, `work_type = "String"`, `material_status = "Necessario"`, `installation_ref = "Lote por confirmar"`; notas a declarar expressamente que o número do lote ainda tem de ser confirmado; `next_action` para confirmar o lote e identificar/adquirir os dois painéis de substituição.
  5. `Alirações`: título `Concluir instalação do meter no local correto`; `urgency = "Media"`, `work_type = "Outro"`, `material_status = "Necessario"`, `installation_ref = "Cabos de entrada da rede"`; notas sobre a necessidade de TIs de núcleo aberto capazes de abraçar esses cabos; `next_action` para confirmar dimensões e pedir preços à Invitécnica e à Sisacol.
- Fazer rollback integral perante qualquer falha. Não alterar código, schema, assets, contratos ou outros tickets.
- Depois do commit SQLite, reler as cinco intervenções por asset/título e apresentar ids, estado, urgência, tipo, material, próxima ação e notas essenciais para auditoria.

**verify:**
- Executar `PRAGMA integrity_check` na base ativa e confirmar `ok`.
- Confirmar que existem exatamente cinco intervenções abertas correspondentes às cinco finalidades acima, ligadas aos assets esperados, sem pares duplicados de asset/finalidade.
- Confirmar especificamente que o email `filomenakellen@gmail.com`, `ZNShine 460W`, `375W`, `Lote por confirmar`, `Invitécnica` e `Sisacol` ficaram persistidos nos respetivos registos.

**done:**
- As cinco intervenções aparecem no pipeline da plataforma com associação, prioridade, material, próximo passo e contexto corretos; a incerteza do lote está visível e nenhum dado alheio foi alterado.

## Success Criteria

- O pipeline contém uma intervenção para AGA, uma para Pires Lourenço 1, duas para Alto dos Moinhos e uma para Alirações.
- Todos os factos fornecidos pelo utilizador estão preservados em campos pesquisáveis/visíveis da intervenção.
- Não existem duplicados abertos criados por esta execução.
- A alteração é transacional, a integridade SQLite passa e existe backup prévio recuperável.
- Nenhum ficheiro de runtime é preparado para commit no Git.

