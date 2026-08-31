# Diagnóstico operacional: estudo, proposta e Fatia 1 (M7 foundation)

Este documento começou como o estudo pedido antes de qualquer implementação —
o que a arquitetura atual já suporta, o que a V1 realmente prova — e passou,
na continuação da mesma sessão, a registar também a Fatia 1 que esse estudo
propôs: schema, importador e um teste dourado, aplicados à base viva.
A Fatia 2 (leitura live ao nível de dispositivo) continua deliberadamente por
fazer, pelas razões abaixo.

## O que já existe e encaixa

A identidade canónica de dispositivo já está completa e em produção: 325
dispositivos, todos `inverter`, cada um com um `asset_provider_mappings` de
`resource_kind='device'` já reclamado. `MonitoringObservation` e
`ProductionFact` já modelam `condition`/`freshness`/`quality`/`completeness`
como estados de primeira classe, chaveados por `provider_mapping_id` — o mesmo
mecanismo que, apontado a uma claim de dispositivo em vez de planta, dá
naturalmente um facto ao nível do inversor sem inventar um novo vocabulário de
qualidade.

Ou seja: a fundação de identidade e o padrão de modelação de factos já existem.
O que falta é só a camada de factos ao nível de dispositivo — e essa é
exactamente a peça que este documento propõe, sem a construir ainda.

## O que a V1 realmente prova (e o que não prova)

Auditoria read-only de `/opt/server/apps/Nem-sei/data/monitoring_board.db` em
2026-08-19:

| Tabela V1 | Linhas | O que guarda |
| --- | --- | --- |
| `provider_devices` | 325 | Identidade do dispositivo no provider — já importada para `devices` |
| `inverter_power_samples` | 54 593 | Potência activa por inversor, amostrada |
| `device_realtime_snapshots` | 51 289 | Estado, potência, energia do dia, disponibilidade, comunicação, por inversor |
| `inverter_availability_sampled_daily` | 6 720 | Disponibilidade diária amostrada por inversor |
| `telegram_alerts` | 30 105 | Alarmes efectivamente enviados, com tipo, mensagem e estado de envio |

`device_realtime_snapshots` já tem exactamente os campos que a lista do pedido
nomeia — `inverter_state`, `active_power_kw`, `day_energy_kwh`,
`availability_status`, `communication_status` — e **duas colunas para
strings/MPPT**, `pv_current_json` e `pv_voltage_json`.

Essas duas últimas estão **vazias em todas as 51 289 linhas**. A coluna existe;
o dado nunca foi capturado. Isto é dito aqui em vez de ser silenciado: string/
MPPT não é uma métrica que a V1 prove, apesar de o schema sugerir que sim, e
não deve ser modelada como se estivesse disponível até um provider a entregar
de facto.

`inverter_state` é um código numérico do provider (`40960` no exemplo
verificado), decodificado pela lógica própria da V1 — decodificar sem essa
lógica seria inventar um significado, não portá-lo.

## O que nenhum adaptador V2 ainda lê ao nível de dispositivo

`integrations/fusionsolar/` e `integrations/sigenergy/` falam hoje só ao nível
da planta. Nenhum dos dois tem um pedido de leitura de dispositivo verificado —
o mesmo portão que já existe para a produção diária (`PVYield` verificado por
ligação antes de qualquer persistência) ainda não tem equivalente ao nível do
inversor. Construir a tabela de factos sem essa verificação repetiria
exactamente o erro que esse portão existe para prevenir.

## A proposta original: duas fatias, nesta ordem

**Fatia 1 — histórico de dispositivo importado da V1 (valor imediato, risco
baixo).** Estender `production_facts`/`monitoring_observations` — ou uma tabela
irmã dedicada, dado o volume (~106 mil linhas combinadas) — para aceitar um
`device_id` opcional, e importar `inverter_power_samples` +
`device_realtime_snapshots` como histórico, pelo mesmo padrão já provado três
vezes nesta sessão (`v1_reporting_import.py`, `portfolios/v1_import.py`):
idempotente, com proveniência, sem adivinhar nada. Isto dá um ecrã de
diagnóstico com dados reais — "este inversor esteve assim em Julho" — sem
depender de nenhuma leitura live nova.

**Fatia 2 — leitura live ao nível de dispositivo (bloqueada até um contrato
verificado).** Estender `FusionSolarProductionContract` (ou equivalente) com um
pedido de dispositivo, verificado por ligação, antes de qualquer persistência
live. Alarmes ficam nesta fatia: `telegram_alerts` é histórico do que a V1
enviou, não um feed de alarmes do provider — um alarme "ao vivo" precisa do seu
próprio contrato verificado, tal como a produção precisou.

## O que a Fatia 1 entregou, e onde a proposta original estava incompleta

A ideia de "estender `production_facts`/`monitoring_observations`" não
sobreviveu ao primeiro facto real: **nenhuma das 325 claims de dispositivo
está `active`** — todas ficaram `pending_review` no import do M1, em ligações
desativadas e sem credenciais. `production_facts`/`monitoring_observations`
estão ambas ancoradas em `provider_mapping_id`; uma tabela que exigisse uma
claim utilizável não podia aceitar uma única linha hoje. Isto só apareceu ao
tentar escrever a primeira linha, não na leitura do schema — exactamente o
tipo de coisa que "propõe" não podia ter previsto sem chegar a "implementa".

`device_status_facts` (migração `0015_device_status_facts`) ficou ancorada em
`device_id` em vez disso — a identidade que o M1 já resolveu para os 325
dispositivos, independente de qualquer ligação de provider estar utilizável.
Guarda leituras pontuais (`observed_at`, não um período): `availability_status`
no vocabulário da própria V1 (`available`/`standby`/`unavailable`/`unknown`,
não o `OBSERVATION_CONDITIONS` de cinco valores do V2, que não tem
correspondência honesta para "standby"), mais `active_power_kw` e
`day_energy_kwh`, ambos NULL quando ausentes e nunca zero por omissão.

A classificação `inverter_state -> availability_status` foi portada, não
reinventada, de `monitoring_board/services/fusionsolar.py`
(`classify_fusionsolar_inverter_availability`) — os três conjuntos de códigos
(`AVAILABLE_INVERTER_STATES`, `UNAVAILABLE_INVERTER_STATES`,
`STANDBY_INVERTER_STATES`) são código real da V1, copiados, não adivinhados a
partir dos números. `tests_v2/test_diagnostics_golden.py` compara a função
portada contra a coluna `availability_status` que a V1 já tinha calculado, para
cada `inverter_state` distinto real — só corre a partir do host
(`/opt/server/apps/Nem-sei/.venv/bin/python`), pela mesma razão de WAL que os
outros testes dourados desta sessão.

### O estado da instalação (2026-08-25)

Faltava a resposta de nível acima: o que é que **esta central** está a fazer
agora. O badge ao lado do nome de uma central era o `lifecycle_status` — um
campo administrativo (ativa/inativa/desativada) que nada calcula e que o
importador da V1 deixou em `unknown` em 264 de 267 centrais. Uma central a
produzir dizia "Desconhecida", que é exactamente a coisa errada para um
produto de O&M dizer sobre uma central que está a trabalhar.

`monitoring/installation_state.py` deriva esse estado — nunca o guarda,
recalcula-o a cada chamada, como `diagnostics/findings.py` — a partir de duas
fontes, por esta ordem: a `MonitoringObservation` da instalação (o que o
provider diz do sistema todo) e, quando essa não diz nada de útil, as
`DeviceStatusFact` dos seus equipamentos (um site cujos inversores estão todos
disponíveis está a trabalhar, diga o que disser o endpoint da central).

Duas respostas são deliberadamente distintas de `unknown`, porque confundi-las
faz perder tempo a quem opera: `no_evidence` (nunca se leu nada desta central)
e `stale` (leu-se, mas há tempo de mais para descrever "agora" — janela de 24 h,
a mesma de `diagnostics.findings`). Só "temos leitura fresca e ela não diz" é
`unknown`. Os factos de produção não são consultados: um total diário é
evidência sobre ontem, e usá-lo aqui deixaria uma central que morreu de manhã
continuar a dizer "operacional" o resto do dia.

O badge aparece na ficha da central, na lista de centrais e no separador de
instalações de um portfolio; a coluna que dizia "Estado" e mostrava o ciclo de
vida passou a chamar-se "Ciclo de vida", que é o que sempre foi.

### O alerta de queda de central (2026-08-25)

A funcionalidade que a V1 tinha e a V2 não: "esta central caiu" / "voltou", no
Telegram. Ao investigar porque é que uma mensagem da V1 dizia que uma central
estava a trabalhar enquanto a V2 dizia "sem leitura recente", o corte apareceu
em três sítios, nenhum deles onde se esperava:

1. **Nada agendava a leitura de estado.** Não existia tipo de job
   `monitoring.current`. O `FusionSolarMonitoringService.sync_current_monitoring`
   estava escrito e testado desde sempre, e tinha sido chamado **uma vez**, a
   2026-08-19. Daí as 2 linhas em `monitoring_observations`.
2. **Os incidentes eram só de equipamento.** `evaluate_and_persist_incidents`
   percorria `select(Device.asset_id).distinct()` — as centrais que têm
   dispositivos importados. 132 das 134 centrais mapeadas não têm nenhum, por
   isso uma central a cair não gerava incidente, logo não gerava alerta.
3. A política de avisos não notifica à abertura; a de críticos sim, à abertura
   **e** à resolução — que é exactamente o par `⚠️`/`✅` da V1.

**O que foi construído:** o job `monitoring.current` (despachado por
`provider_code` desde o primeiro dia, para não repetir o bug que a produção
teve em 094a40a), um agendamento por conta de provider a 15 minutos, e as
regras de nível central em `findings.py` — `plant_offline` e `plant_fault`
(críticas), `plant_warning` e `plant_state_stale` (avisos). O avaliador de
incidentes passou a percorrer a união das centrais com dispositivos e das
centrais com leitura de estado.

**Duas decisões que valem mais do que o código:**

*Uma central nunca lida não gera finding nenhum.* "Nunca ligada" é lacuna de
onboarding, não avaria; abrir incidente por cada instalação não mapeada
enterrava as que realmente caíram debaixo das que ninguém ligou.

*Uma leitura fresca com condição `unknown` também não.* É o caso da Sigenergy
todas as noites — payload completo em que o provider não declara estado. "Não
sabemos" não é a mesma afirmação que "está mal", e só a segunda justifica
interromper alguém.

E a garantia que o código já dava e que se mantém: uma leitura falhada ou
travada por limite **nunca** vira `offline`. Nenhum dos dois serviços escreve
observação em erro. Só o provider a dizer offline é offline.

**Custo real, medido:** o FusionSolar responde até 100 centrais por chamada, o
que põe as 134 em 2 chamadas mais um login em cache. A `current_monitoring` é
família de endpoint própria em `provider_request_states` — sem cooldown, ao
contrário do `device_discovery` e do `device_current_monitoring`, que são os
que apanham `rate_limited` hoje. Não compete com o orçamento da sonda de
dispositivos.

### A divergência do 40960 (2026-08-25)

A paridade com a V1 tem uma excepção declarada. O código `40960` (0xA000) era
o buraco maior da classificação portada: 10 736 das 51 289 leituras, e a
**última** leitura de 220 dos 325 dispositivos — ou seja, a razão pela qual a
tabela de diagnóstico mostrava "desconhecido" em quase toda a frota. A V1
também nunca o classificou; caía no `unknown` por omissão dos conjuntos.

Foi resolvido a partir da evidência nas próprias linhas da V1, não de
documentação da Huawei (que não temos):

* `active_power_kw` é 0 nas 10 736 linhas — nunca nulo, nunca positivo;
* todas as observações caem entre as 19:00 e as 05:59 UTC (20h–06h de Lisboa
  nos dados de Junho–Julho de 2026), zero ocorrências entre as 06:00 e as
  18:59 UTC;
* 4 384 das linhas ainda trazem a energia acumulada do dia, até 874,7 kWh.

Um inversor desligado à noite depois de ter produzido é `standby` no
vocabulário da própria V1. `V2_STANDBY_INVERTER_STATES` em
`diagnostics/rules.py` regista a decisão, e `test_diagnostics_golden.py`
re-deriva as três observações da base da V1 em cada corrida — se a evidência
deixar de se verificar, o teste falha em vez de o produto continuar a dar uma
resposta que já não consegue justificar. A migração
`0025_inverter_state_40960` reclassificou as linhas já importadas
(`availability_status` é coluna derivada; `metadata_json->>'v1_inverter_state'`,
que é a evidência, não é tocada).

`inverter_power_samples` (54 593 linhas, só potência, um único dia) não foi
importado nesta fatia — `device_realtime_snapshots` já cobre estado, potência
e energia numa janela muito mais larga, e importar as duas seria trabalho
duplicado sem uma pergunta nova a responder. Fica registado, não esquecido.

`communication_status` também não foi importado: lê `"recent"` nas 51 289
linhas, sem excepção — `test_diagnostics_golden.py` verifica isto directamente
em vez de assumir. "Última comunicação" é respondida pelo próprio
`observed_at`, não por um rótulo que não varia.

### Provado com dados reais

Contra a base viva, depois de aplicada a migração `0015`: importação real de
`device_realtime_snapshots`, seguida de uma segunda importação do mesmo
período para confirmar zero linhas novas. Ver o resumo da sessão para os
números exactos.

## O que fica deliberadamente fora

- **Disponibilidade ponderada** já está portada e validada contra a V1
  (`rules/availability.py`), e agora tem uma fonte de factos reais em
  `device_status_facts` — mas as duas ainda não estão ligadas. Calcular
  disponibilidade a partir destes factos, e expô-la no relatório individual em
  vez de `availability_pct: None`, é o passo seguinte concreto, não mais
  levantamento.
- **Nenhuma UI de diagnóstico foi construída.** `current_device_status()` em
  `diagnostics/service.py` é a leitura mínima que uma futura página
  precisaria — "o que é que cada dispositivo desta instalação está a fazer
  agora" — mas não há rota nem template. Fundação, não a funcionalidade.
- **Nenhuma métrica financeira entra aqui.** Diagnóstico é estado de
  equipamento; reporting é dinheiro e energia contratada. `production_facts`
  já separa isto por `metric_kind`, e `device_status_facts` vive no seu
  próprio pacote (`nemsei/diagnostics/`), nunca uma linha que sirva as duas
  camadas.
- **Strings/MPPT não são propostos como coluna nem como métrica** até um
  provider realmente os entregar. A coluna vazia na V1 é o argumento contra
  modelá-los agora, não a favor.

## Próximo passo concreto

Duas peças, e afinal não do mesmo tamanho — verificado ao tentar ligar a
primeira, não assumido.

**A disponibilidade não é uma simples ligação.** `weighted_sampled_availability`
em `rules/availability.py` já está portado, mas é a parte pequena: só pesa um
`availability_pct` por dispositivo que já lhe é dado. Calcular esse valor a
partir de leituras pontuais é `services/sampled_availability.py`,
~250 linhas por si só — uma janela de operação por dia (do primeiro ao último
instante com potência positiva, com tolerância de `OPERATING_EDGE_MINUTES=30`
nos extremos), um mínimo de amostras derivado do intervalo máximo aceite entre
leituras (`MAX_SAMPLE_GAP_MINUTES=90`), regras de completude por dispositivo
(`late_first_sample`, `early_last_sample`, `sample_gap_over_90_minutes`,
`insufficient_sample_count`), e o dia definido no fuso de Lisboa, não em UTC.
Depende ainda de `provider_device_configuration_history` — "que dispositivos
se esperava que reportassem nesta data" — que o M1 decidiu explicitamente não
importar (`DECISIONS.md`: "as suas 325 linhas são todas de versão única e
abertas, não há histórico de dispositivo para migrar"), o que simplifica mas
não elimina a pergunta.

**A página de diagnóstico é, essa sim, uma ligação.** `current_device_status()`
já existe e já responde à pergunta "o que está cada dispositivo desta
instalação a fazer agora"; falta-lhe rota e template, seguindo exactamente o
padrão de `/reports`. Feito nesta mesma continuação — ver
`web/diagnostics_routes.py`.

## Porque a disponibilidade ponderada não avançou: um problema de dados, não de código

Antes de portar o algoritmo, verifiquei quanto valor ele realmente produziria
sobre a evidência que existe — e a resposta muda a prioridade desta peça.

V1 já correu este cálculo sobre os seus próprios dados e guardou o resultado em
`inverter_availability_sampled_daily` e `plant_availability_sampled_daily`.
Consultado directamente:

| Tabela | Linhas | Com `availability_pct` real |
| --- | --- | --- |
| `inverter_availability_sampled_daily` | 6 720 | **3** (0,045%) |
| `plant_availability_sampled_daily` | 5 121 | **3** (0,059%) |

6 717 das 6 720 linhas por dispositivo ficam `sampled_partial` com
`availability_pct` **NULL**, na sua esmagadora maioria por
`sample_gap_over_90_minutes` e/ou `insufficient_sample_count`. Isto não é um
limiar demasiado apertado do algoritmo — é a densidade real da amostragem:
`device_realtime_snapshots` tem, para a maioria dos pares dispositivo/dia,
entre **1 e 6 leituras no dia inteiro** (964 dias com 1 leitura, 1619 com 2,
3524 com 3...). Com um intervalo máximo aceite de 90 minutos e um dia inteiro
de luz solar para cobrir, seis leituras dispersas quase nunca chegam.

A conclusão é dura mas honesta: **a disponibilidade ponderada não é calculável
a partir do histórico da V1**, nem pela implementação da V1 nem por um porte
fiel dela — os dois usam a mesma evidência esparsa e chegam à mesma conclusão
vazia. Portar o algoritmo agora produziria uma funcionalidade que parece
existir mas devolve `None` em 99,95% dos casos, o que é exactamente o tipo de
número silenciosamente ausente que esta sessão inteira tentou evitar, só que
ao nível da funcionalidade em vez de ao nível de uma linha.

Isto não invalida `weighted_sampled_availability` nem o desenho de
`device_status_facts` — ambos ficam correctos e prontos para o dia em que
existir uma leitura live com uma cadência de amostragem real (a Fatia 2, ainda
bloqueada por falta de contrato de provider verificado ao nível de
dispositivo). Só não há hoje dados suficientes para os alimentar
retroactivamente, e isso só se soube ao consultar os números, não ao ler o
schema.

## Re-estudo de `sampled_availability.py`, 2026-08-20 (sessão posterior)

Reli o ficheiro real (`monitoring_board/services/sampled_availability.py`, não
o resumo acima) especificamente para confrontar as suas regras de
completude com o que a V2 consegue realmente recolher hoje (Fatia 2/3), não
para reconfirmar a conclusão de densidade já registada. Três factos mudam
como este canário deve ser lido, e nenhum estava explícito acima:

**1. A janela operacional não é meia-noite a meia-noite — é definida pelas
próprias leituras de potência positiva.** `window_start`/`window_end` vêm de
`positive_times`, o conjunto de instantes em que **qualquer** inversor
esperado teve `active_power_kw > 0` nesse dia (linha 266-309). As regras de
completude (`late_first_sample`, `early_last_sample`,
`sample_gap_over_90_minutes`, `insufficient_sample_count`) só se aplicam
**dentro** dessa janela. Isto significa que a noite — sem produção — nunca
entra na avaliação: um canário que arranque de noite não precisa de "provar"
cobertura nocturna nenhuma, porque o algoritmo nem olha para lá. O que
precisa de provar é: (a) a primeira amostra de cada inversor cai a ≤30 min do
início da janela real de produção, (b) a última cai a ≤30 min do fim, (c)
nenhum intervalo dentro da janela excede 90 min, (d) pelo menos
`max(4, ceil(duração_min / 90) + 1)` amostras por inversor — para uma janela
de ~15h (900 min) isso são **11 amostras mínimas**; a cadência de 30 min já
prevista dá ~30, larga margem.

**2. `deduplicate_observed_at` (Fatia 2/3, `diagnostics/service.py`) não
grava uma nova linha quando o conteúdo de uma leitura repete o anterior —
isto interage com a regra dos 90 min, mas o risco prático é baixo, não
inexistente.** Durante a noite isto é irrelevante (ponto 1). Durante produção
real, `day_energy_kwh` é um total diário estritamente crescente e
`active_power_kw` varia com nuvens/ângulo solar, pelo que três ciclos
consecutivos (90 min) com valores byte-idênticos é improvável mas **não
impossível** — um inversor preso num valor fixo, ou um período de céu limpo e
carga estável a meio-dia arredondado sempre ao mesmo valor pelo provider,
criaria um `sample_gap_over_90_minutes` **falso**: o poller correu na mesma,
só não gravou porque nada mudou. Isto não é um bug a corrigir hoje — o
`device_status_facts` não é uma série temporal pura, é um facto com
supersessão (tal como `production_facts`), e alterar isso para "gravar sempre,
mesmo sem mudança" trocaria um problema por outro (crescimento de revisões
sem qualquer valor novo). É uma advertência a aplicar na leitura do
resultado do canário: se aparecer um `sample_gap_over_90_minutes` dentro da
janela de produção, confirmar primeiro se os polls realmente aconteceram
(`provider_request_attempts`/`job_events`) antes de concluir que a cadência
falhou.

**3. `expected_devices_for_date` depende de
`provider_device_configuration_history`, uma tabela específica do SQLite da
V1 — a V2 não precisa de a portar literalmente.** O equivalente em V2 já
existe: a identidade temporal de `Device` (`valid_from`/`valid_to`, do M1) já
responde "que dispositivos eram esperados neste asset nesta data", pela
mesma razão estrutural que já suporta `device_status_facts` — não é uma peça
em falta, é uma peça já construída sob um nome diferente. Confirmar isto
antes de portar `expected_devices_for_date` é o próximo passo concreto
quando o gate de densidade passar, não uma segunda auditoria de dados.

**O que isto muda na leitura do canário mais longo (§10,
`docs/v2/DEVICE_TELEMETRY.md`):** o teste relevante não é "cobriu a
meia-noite às duas pontas", é "a partir do primeiro instante de potência
positiva até ao último, todos os inversores têm ≤90 min de intervalo e
≥11 amostras, com a primeira/última a ≤30 min das bordas". Um canário
capado a ~22,5h a partir de qualquer hora do dia cobre isso com folga desde
que atravesse pelo menos um dia inteiro de produção — não precisa de
começar exactamente à meia-noite nem à hora do nascer-do-sol para ser
válido.

## Fatia 4: um primeiro motor de findings determinístico, 2026-08-20 (sessão posterior)

Com a Fatia 3 fechada e o re-estudo acima feito, a próxima peça independente
que não depende do canário bloqueado (§10 de `DEVICE_TELEMETRY.md`) é
tornar `/diagnostics` útil para além de uma tabela: responder "o que está
mal nesta instalação agora" a partir de dados que já existem.

**Desenho deliberado: regras, não uma tabela persistida.** `diagnostics/findings.py`
avalia um conjunto de regras determinísticas sobre `current_device_status()`
(mais o histórico de cada dispositivo, para "desde quando") e devolve uma
lista de `DiagnosticFinding` recalculada em cada carregamento da página —
não existe uma tabela nova, nem migração, nem um ciclo de vida
open/acknowledged/resolved. Isto não é uma simplificação apressada: sem essa
tabela, o mesmo problema persistente nunca pode acumular-se em centenas de
findings independentes, porque não há nada a acumular — o finding
desaparece da lista sozinho assim que a condição deixa de ser verdadeira na
leitura mais recente. Um ciclo de vida persistido fica para quando existir
uma necessidade operacional real de o "reconhecer" (a instrução do próprio
milestone), não antes.

**Regras implementadas**, todas com evidência, severidade e "que dados
faltam para confirmar" explícitos:

| Regra | Severidade | Precisa de |
| --- | --- | --- |
| `device_no_history` | aviso | nenhuma leitura alguma vez registada |
| `device_unavailable` | crítico | última leitura com `availability_status='unavailable'` |
| `device_unknown_status` | aviso | última leitura com `availability_status='unknown'` |
| `stale_reading` | aviso | última leitura há mais de 24h (configurável) |
| `zero_power_while_peers_active` | crítico | potência zero enquanto um dispositivo comparável (mesmo `device_kind`, leitura a ≤45 min de distância) produz >0,5 kW |
| `power_disparity_among_peers` | aviso | potência <50% da de um dispositivo comparável, acima do limiar de ruído |
| `daily_energy_disparity_among_peers` | aviso | energia do dia <50% da de um dispositivo comparável, com o par já a produzir ≥1 kWh |
| `partial_device_coverage` | informação | nem todos os dispositivos do asset têm alguma leitura (finding ao nível do asset, não de um device específico) |

**Duas escolhas de desenho que valem a pena registar:**

- **Comparação entre pares só dentro de uma tolerância temporal (45 min).**
  Sem isto, um dispositivo genuinamente `stale` (leitura antiga) pareceria
  também estar em "disparidade" contra um dispositivo com leitura fresca —
  o mesmo sintoma teria duas causas confundidas num só finding.
  `test_readings_too_far_apart_in_time_are_not_compared_to_each_other`
  prova isto directamente.
- **`active_since` vem do histórico real quando disponível, não é inventado.**
  Para as regras de estado por dispositivo (indisponível/desconhecido), o
  módulo percorre `history_for_device` para trás até a condição deixar de
  ser verdadeira — não assume que "desde agora" é o mesmo que "desde
  sempre". Para as regras de comparação entre pares, `active_since` é
  apenas a leitura mais recente (reconstituir a disparidade ao longo do
  histórico recalcularia a mesma comparação em cada ponto passado, um custo
  não justificado ainda) — e cada finding desse tipo diz isso explicitamente
  em `missing_data`, em vez de fingir uma precisão que não tem.

**Testado, não apenas escrito**: 18 testes puros (`test_diagnostics_findings.py`,
sem base de dados) cobrindo cada regra, os dois limiares de ruído (potência
mínima do par, tolerância temporal), a ordenação worst-first, e o caso "sem
problemas" — mais 2 testes de integração web
(`test_diagnostics_web.py`) confirmando que os findings chegam mesmo à
página `/diagnostics/assets/<id>`, antes da tabela de dispositivos, worst
first. Suite completa da V2 sem regressões (ver relatório da sessão).

**O que fica deliberadamente fora desta fatia**: um resumo de severidade ao
nível do asset na página de índice `/diagnostics` (Prioridade 4 do pedido
original, UI mais rica), qualquer coisa ao nível do portfolio, e qualquer
noção de "motor de IA" — tudo isto seria construir em cima de uma fundação
ainda não exercida com dados operacionais reais para além do canário de um
asset.
