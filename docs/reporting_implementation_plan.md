# Plano de Implementacao Por Prompts

## Fase 0 - Auditoria e desenho da arquitetura

Branch sugerida: `chore/reporting-architecture-audit`

Mantem todas as regras do prompt mestre.

Nesta fase nao implementar funcionalidades de negocio.

Analisar em profundidade:

- `monitoring_board/app_factory.py`
- `monitoring_board/customer_reports.py`
- `monitoring_board/portfolio_reports.py`
- templates de relatorios
- tabelas SQLite relacionadas com reporting
- testes existentes
- sincronizacao de producao e disponibilidade
- exportacao PDF e Excel

Mapear os fluxos atuais:

1. geracao de relatorio individual
2. geracao de relatorio de portfolio
3. obtencao de producao
4. obtencao de consumo, autoconsumo e excedente
5. calculo financeiro
6. WAT
7. Helioscope e degradacao
8. tarifas e faturas
9. snapshots de relatorios

Propor arquitetura concreta e incremental para `monitoring_board/reporting/`, indicando modulos, responsabilidades, funcoes a mover, funcoes a manter, dependencias permitidas, estrategia de migracao, testes, esquema futuro da base de dados e ordem recomendada das fases.

Criar `docs/reporting_architecture.md`.

Criterios de aceitacao:

- nenhuma alteracao funcional
- mapa claro da arquitetura
- identificacao de formulas duplicadas
- identificacao das queries dentro de rotas
- identificacao dos pontos onde `app_factory.py` deve ser reduzido
- plano de migracao incremental

## Fase 1 - Testes de caracterizacao e fundacoes

Branch sugerida: `refactor/reporting-foundations`

Mantem todas as regras do prompt mestre e segue `docs/reporting_architecture.md`.

Criar as fundacoes do pacote `monitoring_board/reporting/`.

Implementar apenas:

- `models.py`
- `periods.py`
- estrutura inicial de `billing.py`
- estrutura inicial de `availability.py`
- estrutura inicial de `degradation.py`
- estrutura inicial de `repositories.py`

Nao alterar ainda o resultado visual dos relatorios.

Antes de refatorar, criar testes de caracterizacao para o comportamento atual de:

- relatorio ESCO
- relatorio EPC
- relatorio mensal
- portfolio
- WAT
- degradacao
- exportacao Excel

Mover apenas funcoes puras e sem efeitos laterais.

Introduzir enums ou tipos equivalentes para:

- `ReportPeriodType`
- `BillingMode`
- `ReportType`
- `TariffType`
- `BillingEnergyBase`

Valores necessarios:

- `ReportPeriodType`: `monthly`, `quarterly`, `semiannual`, `annual`
- `BillingMode`: `energy`, `fixed_monthly_fee`
- `BillingEnergyBase`: `self_consumption`, `total_production`

Garantir que os modulos novos nao importam Flask.

Criterios de aceitacao:

- aplicacao continua a arrancar
- relatorios atuais continuam iguais
- testes anteriores continuam a passar
- novos modulos podem ser testados sem contexto Flask
- nenhuma formula duplicada nova

## Fase 2 - Corrigir WAT e degradacao

Branch sugerida: `fix/wat-and-degradation`

Implementar as regras definitivas de WAT:

1. manter o nome WAT
2. um inversor esta disponivel quando `active_power_kw > 0`
3. um slot so e valido quando pelo menos um inversor da central esta a produzir
4. sem comunicacao conta como indisponibilidade apenas durante slots validos
5. WAT da central e ponderado pela potencia nominal dos inversores
6. WAT do portfolio e ponderado pela potencia nominal instalada de cada central
7. instalacoes sem potencia nominal valida devem gerar aviso e nao distorcer o denominador

Implementar a degradacao do Helioscope:

- calculada desde a data de montagem
- primeiros 12 meses: degradacao de 2,5%
- apos os primeiros 12 meses: mais 0,55% por ano
- os 0,55% sao proporcionais mensalmente

Formula:

- meses ate 12: fator `0.975`
- meses acima de 12: `0.975 - ((meses - 12) / 12 * 0.0055)`
- fator nunca inferior a zero nem superior a um

Casos obrigatorios:

- sem data de montagem
- mes da montagem
- 6 meses
- 12 meses
- 13 meses
- 18 meses
- 24 meses
- data de relatorio anterior a montagem
- portfolio com duas centrais de potencias diferentes
- central sem potencia nominal
- inversor sem comunicacao durante e fora de slots validos

Nao alterar ainda a interface dos relatorios.

## Fase 3 - Configuracao de cobranca e logica ESCO

Branch sugerida: `feat/esco-billing-models`

Implementar configuracao de cobranca por instalacao, com:

- cobranca por energia
- cobranca por mensalidade fixa
- utilizacao da configuracao guardada
- introducao manual de valores ao gerar o relatorio

Campos por instalacao:

- `billing_mode`
- `solcor_price_per_kwh`
- `fixed_monthly_fee_eur`
- `billing_energy_base`
- `electricity_price_source`
- `default_electricity_price`
- `default_export_price`
- timestamps de criacao e atualizacao

Regras:

- ESCO por autoconsumo: `self_consumption_kwh * solcor_price_per_kwh`
- ESCO por producao total: `production_kwh * solcor_price_per_kwh`
- mensalidade: `fixed_monthly_fee_eur * numero de meses do periodo`
- compra a rede: `max(consumption - self_consumption, 0)`
- receita do excedente pertence ao cliente
- beneficio bruto: poupanca por autoconsumo + receita do excedente
- beneficio liquido: beneficio bruto - pagamento Solcor

O relatorio deve expor:

- energia fornecida pela Solcor ao cliente
- energia comprada pelo cliente a rede
- energia excedente vendida pelo cliente a rede

Usar `Decimal` para valores monetarios.

Atualizar formulario individual com configuracao guardada e valores manuais.

Nao alterar o layout principal do PDF alem do necessario.

Casos obrigatorios:

- ESCO por autoconsumo
- ESCO por producao total
- ESCO com mensalidade
- EPC sem pagamento Solcor
- periodos de tres, seis e doze meses
- consumo inferior ao autoconsumo
- preco zero
- configuracao guardada
- override manual

## Fase 4 - Periodos trimestral, semestral e anual

Branch sugerida: `feat/report-periods`

Generalizar relatorios para:

- mensal
- trimestral
- semestral
- anual

Implementar em `reporting/periods.py` representacao unica com tipo, data inicial, data final, label, numero de meses e lista de meses incluidos.

Regras:

- mensal: mes selecionado
- trimestral: T1 janeiro-marco, T2 abril-junho, T3 julho-setembro, T4 outubro-dezembro
- semestral: S1 janeiro-junho, S2 julho-dezembro
- anual: janeiro-dezembro
- periodos correntes nao usam datas futuras

Generalizar queries de producao, consumo, autoconsumo, excedente, calculos financeiros, mensalidades, labels e nomes de ficheiros exportados.

Manter compatibilidade com parametros atuais de relatorio mensal.

Casos:

- cada trimestre
- cada semestre
- ano completo
- periodo corrente
- ano bissexto
- soma dos meses igual ao total agregado
- mensalidade multiplicada pelo numero correto de meses

## Fase 5 - Dados horarios e tarifas

Branch sugerida: `feat/hourly-energy-and-tariffs`

Antes de alterar calculos tarifarios, analisar os campos horarios realmente disponiveis nas APIs FusionSolar e Sigenergy.

Expandir modelo horario para:

- `production_kwh`
- `self_use_kwh`
- `export_kwh`
- `consumption_kwh`
- `grid_import_kwh`

Quando valor nao estiver disponivel:

- guardar `NULL`
- nao substituir por zero
- adicionar aviso ao relatorio
- nao inventar autoconsumo horario a partir de producao mensal

Implementar tarifas:

- simples
- bi-horaria
- tri-horaria
- tetra-horaria

Criar modelos editaveis BTN/MT ciclo diario/semanal e personalizado.

A configuracao deve permitir dias da semana, hora inicial, hora final, periodo tarifario, preco por periodo, preco unico de venda do excedente e datas de validade.

Valor evitado da rede:

`autoconsumo horario * preco aplicavel nesse timestamp`

Nunca usar producao horaria como substituto de autoconsumo horario.

Casos:

- simples, bi, tri e tetra
- semana versus fim de semana
- periodo que atravessa meia-noite
- timestamp no limite de dois periodos
- mudanca de tarifa durante o intervalo
- dados horarios incompletos
- diferenca entre producao e autoconsumo

## Fase 6 - Faturas e extracao assistida

Branch sugerida: `feat/invoice-tariff-history`

Implementar historico de faturas por instalacao:

- ultima fatura ativa
- fatura associada a periodo de validade

Cada fatura guarda instalacao, ficheiro, nome original, data de upload, periodo inicial/final, estado ativo, tarifa associada, dados extraidos, estado de validacao e avisos.

Resolucao da tarifa:

1. procurar tarifa valida para o periodo
2. se nao existir, usar ultima fatura ativa
3. se nao existir nenhuma, permitir relatorio com aviso

Extracao assistida:

- extrair texto quando PDF contem texto
- identificar candidatos a preco EUR/kWh
- tentar identificar tarifa e ciclo
- nunca guardar automaticamente sem confirmacao
- apresentar valores extraidos em formulario de revisao
- guardar texto ou resultado estruturado para auditoria
- marcar baixa confianca quando existirem varios candidatos

Nao fazer OCR repetitivo ou automatico nesta fase.

## Fase 7 - PDF individual generico

Branch sugerida: `feat/customer-report-pdf-periods`

Refatorar PDF individual para aceitar qualquer periodo.

Titulo dinamico:

- Relatorio Mensal
- Relatorio Trimestral
- Relatorio Semestral
- Relatorio Anual

Manter aspeto visual atual sempre que possivel.

Grafico:

- mensal: diario
- trimestral/semestral/anual: mensal

PDF individual mostra apenas informacao financeira e de producao.

Nao adicionar Helioscope, degradacao, WAT, tickets ou intervencoes.

ESCO deve mostrar producao, autoconsumo, excedente, consumo, compra a rede, poupanca na rede, receita do excedente, pagamento Solcor, beneficio liquido, metodo/base de cobranca e mensalidade/tarifa aplicavel.

EPC mostra apenas valores relevantes, sem pagamento Solcor.

Labels de resumo e destaques devem ser dinamicos.

Criar testes aos dados preparados para PDF.

## Fase 8 - Relatorios de portfolio configuraveis

Branch sugerida: `feat/configurable-portfolio-reports`

Implementar selecao de colunas para relatorios de portfolio.

Colunas disponiveis:

- instalacao
- instalacao externa
- subconta
- NIF
- potencia instalada
- producao real
- producao por periodo tarifario
- Helioscope base
- producao esperada ajustada
- degradacao
- desvio kWh
- desvio percentual
- WAT
- tarifa
- valor estimado
- estado dos dados
- avisos

Presets:

- resumo executivo
- producao e performance
- financeiro
- completo
- personalizado

O mesmo conjunto de colunas controla HTML, Excel e PDF.

Linha total:

- somar energia e dinheiro
- calcular desvio atraves dos totais
- calcular WAT ponderado por potencia instalada
- nunca somar percentagens
- indicar quantas instalacoes tem dados incompletos

Todas as instalacoes aparecem, incluindo incompletas.

## Fase 9 - Mapping de portfolios e Solcorelios

Branch sugerida: `fix/portfolio-asset-mapping`

Corrigir mapping para suportar:

- mesmo NIF em Solcorelios I e II
- varias instalacoes da mesma empresa
- varias subcontas
- aliases
- escolha manual em casos ambiguos

NIF repetido entre portfolios nao e erro.

Estrategia:

1. procurar todos os assets com o NIF
2. comparar nome da instalacao
3. comparar aliases
4. comparar subconta ou referencia externa
5. mapear automaticamente apenas quando inequivoco
6. se existirem varias opcoes, marcar como ambiguo
7. disponibilizar selecao manual

Nao usar simplesmente o primeiro asset devolvido pela query.

Mudancas de constraint devem preservar mappings, incluir migracao, deteccao de duplicados e rollback documentado.

Manter subcontas 001 a 005 de Solcorelios II como incompletas enquanto nao existirem dados confirmados.

## Fase 10 - Integracao, regressao e limpeza arquitetural

Branch sugerida: `refactor/reporting-integration-hardening`

Nesta fase nao acrescentar funcionalidades.

Objetivos:

1. remover formulas duplicadas
2. remover queries de reporting das rotas
3. reduzir responsabilidades de `app_factory.py`
4. garantir que servicos nao dependem de Flask
5. garantir que templates nao fazem calculos
6. uniformizar nomes de campos
7. rever `float` versus `Decimal`
8. rever `NULL` versus zero
9. rever timezones
10. rever validacao de inputs
11. rever compatibilidade com bases existentes
12. rever mensagens e avisos
13. rever nomes de ficheiros exportados
14. rever PDFs e Excel
15. rever performance de queries

Executar matriz de regressao cobrindo ESCO, EPC, periodos, tarifas, faturas, valores manuais, configuracao guardada, portfolio, dados incompletos, mapping, WAT e degradacao.

Adicionar indices SQLite apenas onde existam queries comprovadamente pesadas.

Atualizar README, documentacao de arquitetura, instrucoes de migracao, formulas, exemplos, limitacoes conhecidas e checklist de deployment.
