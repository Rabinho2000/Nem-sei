# Produção esperada e indicador de performance

Na pipeline nova de reporting, a produção esperada tem como única origem o
modelo financeiro ativo aplicável à instalação e ao período. O valor mensal do
modelo, o fator de degradação, o valor ajustado usado nos cálculos e a
identificação/versão/data efetiva do modelo são congelados no snapshot.

HelioScope é uma fonte legacy: os imports, tabelas e dados históricos são
preservados, mas não são consultados pelo fecho mensal, snapshots, quality gate
ou rendering novo.

## Definições

- `specific_yield = actual_production_kwh / installed_power_kwp`.
- `expected_specific_yield = adjusted_expected_kwh / installed_power_kwp`.
- `performance_vs_expected_pct = actual_production_kwh /
  adjusted_expected_kwh * 100`.

`performance_vs_expected_pct` mede cumprimento do modelo financeiro. Não é um
Performance Ratio técnico. A plataforma não dispõe, nesta pipeline, da
irradiância no plano do gerador e dos restantes dados necessários para calcular
um PR técnico. Por isso, o indicador não deve ser apresentado ao cliente como
“PR” ou “Performance Ratio”.

O indicador só é calculado quando produção real e produção esperada ajustada
existem e o denominador é estritamente positivo. Um valor ausente nunca é
substituído por zero.
