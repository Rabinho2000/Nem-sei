# Fiabilidade da produção mensal em relatórios

Os relatórios individuais e de portefólio avaliam a qualidade da produção de cada instalação e mês antes de publicarem um valor final. O subtotal diário pode ser conservado para diagnóstico, mas nunca substitui a produção final quando faltam dias.

## Estados

- `complete`: existe um valor mensal válido, ou existem valores diários válidos para todos os dias do mês fechado.
- `partial`: não existe valor mensal válido e só alguns dias têm produção diária válida.
- `missing`: não existe produção válida para o mês, ou o período ainda é futuro.
- `conflict`: o mensal e o diário divergem para além da tolerância, ou uma soma diária ainda parcial já excede o mensal para além dessa tolerância.
- `in_progress`: o mês coincide com a data de referência. Nunca é publicado como produção mensal final.

Valores válidos são numéricos, finitos e não negativos. O valor real `0` é válido e conta para a cobertura. Valores `None`, texto não numérico, `NaN`, infinitos e negativos geram avisos e não contam como dias disponíveis. A cobertura usa datas distintas.

## Fonte e cobertura diária

Um valor mensal válido é a fonte final (`source=monthly`) mesmo quando a cobertura diária está parcial. Nesse caso, o resultado continua `complete`, mas conserva `daily_coverage=partial`, os dias disponíveis e as datas em falta para rastreabilidade.

Sem mensal válido, a soma diária só é final (`source=daily`) quando todos os dias esperados estão disponíveis. Uma soma parcial fica apenas em `raw_daily_total_kwh`.

## Reconciliação

A tolerância central entre o mensal e a soma diária é:

`max(1 kWh, 1% do valor mensal)`

A comparação normal só é feita quando a cobertura diária está completa. Há uma exceção conservadora: se uma soma ainda parcial já for superior ao mensal acima da tolerância, o mês é imediatamente `conflict`.

## Efeito nos relatórios

- Relatório individual: `partial`, `missing` e `conflict` podem acionar o fallback FusionSolar já existente. `in_progress` não o aciona apenas por estar em curso. Se a fonte continuar insuficiente, a produção e os resultados financeiros dependentes ficam indisponíveis e o PDF/Excel apresenta um aviso de rascunho.
- Relatório de portefólio: cada instalação apresenta estado, fonte e cobertura. Se existir pelo menos uma instalação sem produção final, o total de produção do portefólio fica indisponível/rascunho. O subtotal das instalações completas é mostrado separadamente com a contagem `X de Y`.

## Gate financeiro do portefólio

Apenas `production_quality_status=complete` permite publicar métricas financeiras derivadas da produção. Nos estados `partial`, `missing`, `conflict` e `in_progress`, autoconsumo, repartições tarifárias, valores por período, valor estimado, receita de excedente, pagamento ESCO e benefício líquido ficam vazios/indisponíveis. Consumo, previsões, disponibilidade e os campos de diagnóstico da produção são preservados.

Se pelo menos uma instalação incluída não estiver `complete`, os totais financeiros do portefólio também ficam indisponíveis; os valores das instalações completas não são apresentados como se fossem o total. O relatório recebe o aviso `production_financials_not_final` e HTML/PDF apresentam a indicação visível `Indisponível — rascunho`.

Snapshots novos guardam os valores já sujeitos ao gate. Snapshots antigos não são reescritos e, quando não têm `production_quality_status`, mantêm a compatibilidade anterior.

A avaliação recebe explicitamente uma data de referência. Não depende de um relógio escondido e pode ser reproduzida nos testes.
