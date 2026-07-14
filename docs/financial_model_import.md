# Importação de modelos financeiros

A importação aceita livros OpenXML `.xlsx` e `.xlsm` com valores calculados guardados pelo Excel. O parser lê os ficheiros sem executar macros nem recalcular fórmulas.

## Formatos suportados

### Resumo mensal por métricas

- Uma linha de cabeçalho com os 12 meses.
- Linhas para produção FV, consumo e autoconsumo; exportação e importação da rede são opcionais.
- Unidades `kWh` e `MWh` são reconhecidas. Valores em `MWh` são convertidos para `kWh`.
- A folha `Prod month` tem prioridade quando existe.

### Resumo mensal por linhas de mês

- Uma coluna inicial com os meses 1 a 12 ou nomes de meses.
- Colunas identificadas por cabeçalhos de produção, consumo, autoconsumo, exportação e importação da rede.

### Financial Automatic — As sold

Variante identificada pelas folhas e cabeçalhos abaixo:

- `Projeto!J5:M18`: mês, produção mensal, autoconsumo e taxa de autoconsumo.
- `Savings Yr1!B3:N16`: consumo mensal, autoconsumo, excedente e valores financeiros mensais.
- `Projeto!C5`, `Projeto!H8` e `Projeto!G39`: nome, potência instalada e ano de referência da tarifa de acesso.
- `Projeto!P5:P10`, `Projeto!H14`, `Projeto!D26:E28`: resumo UPAC, rendimento específico, custo e preço de venda.
- `Projeto!E41:H44`: períodos tarifários e componentes de energia/rede.
- `Savings Yr1!B41:K52`: energia e valores de fatura agregados por período tarifário.

Neste formato, a importação mensal da rede é calculada como consumo menos autoconsumo quando não existe uma coluna mensal explícita. A origem e os campos calculados ficam registados no preview.

## Limitações e validações

- As fórmulas têm de ter valores calculados guardados no ficheiro. O servidor não executa macros nem recalcula o modelo de 15 minutos.
- O ano de `Projeto!G39` é tratado como ano base apenas quando segue o formato `AAAA/revisão`. O ano indicado no formulário de importação continua a ter prioridade.
- Alguns livros `Financial Automatic` não incluem NIF. A ausência gera um aviso, mas não bloqueia a importação; nunca se deve inferir um NIF a partir do nome.
- Nos modelos com bateria, os fluxos específicos de carga/descarga continuam disponíveis nos detalhes agregados do livro, mas não são incorporados nos cinco KPI mensais principais.
- Se produção e autoconsumo não tiverem 12 meses completos, ou se o mesmo formato for detetado de forma ambígua fora da variante explícita, a importação é recusada.
