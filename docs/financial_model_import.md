# Importação de modelos financeiros

A importação aceita livros OpenXML `.xlsx` e `.xlsm` com valores calculados guardados pelo Excel. O parser lê os ficheiros sem executar macros nem recalcular fórmulas.

## Formatos suportados

### Resumo mensal por métricas

- Uma linha de cabeçalho com os 12 meses.
- Linhas para produção FV, consumo e autoconsumo; exportação e importação da rede são opcionais.
- Unidades `kWh` e `MWh` são reconhecidas. Valores em `MWh` são convertidos para `kWh`.
- A folha `Prod month` tem prioridade quando existe.

### Resumo mensal por linhas de mês

- Uma coluna com os meses 1 a 12 ou nomes de meses; a coluna pode estar deslocada dentro da tabela.
- Colunas identificadas por cabeçalhos de produção, consumo, autoconsumo, exportação e importação da rede.
- As folhas `Prod month` e `Monthly Production` têm prioridade. São aceites as variações de maiúsculas/minúsculas observadas.

### Financial Automatic — geração UPAC

- Livros com as folhas `UPAC`, `Data PV Proposal` e, quando existe, `Detalhes da fatura`.
- O resumo mensal completo pode estar numa tabela deslocada de `Data PV Proposal`, com cabeçalhos equivalentes a consumo, PV, SC, excesso e rede.
- `UPAC` fornece os dados do projeto, potência, totais anuais, tarifa, custos, preço de venda e indicadores de benefício.
- `Detalhes da fatura` fornece repartição por período, preços de energia/rede e totais de benefício quando estiver preenchida.

### Financial Automatic — As sold

Família identificada pelas folhas `Projeto` e `Savings Yr1` e pelos cabeçalhos da tabela, sem depender de colunas fixas:

- `Projeto`: mês, produção mensal, autoconsumo e taxa de autoconsumo.
- `Savings Yr1`: consumo mensal, autoconsumo, excedente e valores financeiros mensais. São suportadas as gerações onde autoconsumo e excedente mudam de coluna.
- `Projeto!C5`, `Projeto!H8` e `Projeto!G39`: nome, potência instalada e ano de referência da tarifa de acesso.
- `Projeto!P5:P10`, `Projeto!H14`, `Projeto!D26:E28`: resumo UPAC, rendimento específico, custo e preço de venda.
- A tabela de períodos tarifários é localizada pelos cabeçalhos `Período`, `Energia`, `Redes` e `Total`; nos ficheiros reais aparece em linhas diferentes conforme a geração.
- `Savings Yr1!B41:K52`: energia e valores de fatura agregados por período tarifário.

Neste formato, a importação mensal da rede é calculada como consumo menos autoconsumo quando não existe uma coluna mensal explícita. Quando existe `Exc. ESS [kWh]`, o excedente armazenado na bateria é retirado da exportação mensal. A origem, a célula de ajuste e os campos calculados ficam registados no preview.

## Campos extraídos

- Produção, consumo, autoconsumo, exportação, importação da rede e taxas mensais derivadas.
- Nome do projeto, potência instalada, ano base e resumo anual UPAC.
- Custos de instalação, preço de venda e indicadores financeiros disponíveis nas células suportadas.
- Esquema tarifário, preços de energia/rede por período e tarifa total evitada.
- Totais e repartição de fatura por período, quando o livro contém valores calculados.

## Limitações e validações

- As fórmulas têm de ter valores calculados guardados no ficheiro. O servidor não executa macros nem recalcula o modelo de 15 minutos.
- Os ficheiros binários `.xls` não são suportados; é necessária uma cópia `.xlsx` ou `.xlsm` guardada pelo Excel.
- O ano de `Projeto!G39` é tratado como ano base apenas quando segue o formato `AAAA/revisão`. O ano indicado no formulário de importação continua a ter prioridade.
- Alguns livros `Financial Automatic` não incluem NIF. A ausência gera um aviso, mas não bloqueia a importação; nunca se deve inferir um NIF a partir do nome.
- Nos modelos com bateria, a carga proveniente do excedente corrige a exportação. A descarga para autoconsumo, carga da rede e injeção posterior não são somadas aos cinco KPI mensais porque algumas gerações não guardam esses fluxos por mês de forma consistente.
- Pequenas diferenças entre o consumo mensal somado e o total anual do próprio livro podem existir por arredondamento, intervalos incompletos ou tabelas dinâmicas desatualizadas. O parser mantém os valores mensais guardados e não força o total anual.
- Se produção e autoconsumo não tiverem 12 meses completos, ou se o mesmo formato for detetado de forma ambígua fora da variante explícita, a importação é recusada.

Na auditoria de julho de 2026, 306 dos 311 livros identificados pela estrutura como modelos financeiros tinham 12 meses e valores calculados suficientes para importação. Os cinco restantes estavam incompletos ou sem resultados de fórmula guardados nas tabelas mensais.
