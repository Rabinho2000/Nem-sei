# A pipeline funcional da V2: o que corre, o que prova que correu, e quem corta

Escrito em 2026-08-31, depois de uma auditoria que encontrou quatro coisas a
funcionar ao contrário do que o interface dizia. Não é um plano — é o registo do
que estava errado, porque é que ninguém deu por isso, e o que passou a impedir
que volte a acontecer sem se ver.

## 1. O deploy canónico e os seus componentes

**O que estava errado.** `scripts/v2_compose_up.sh` construía o Compose só com
`docker-compose.v2.yml`. Os overrides — nomeadamente
`docker-compose.v2.diagnostic-incidents.yml`, que liga o avaliador de
incidentes — dependiam de o operador se lembrar de acrescentar `-f`. Um deploy
canónico recriava `scheduler` e `worker` sem essa variável, com sucesso, sem
erro nenhum e sem registo nenhum. Aconteceu a 2026-08-21 (~12 minutos) e tinha
voltado a acontecer antes de 2026-08-31, dessa vez durante horas: os
contentores estavam de pé há cinco horas sem
`NEMSEI_V2_DIAGNOSTIC_INCIDENT_EVALUATION_ENABLED`, e
`schedule_state.diagnostics.evaluate_incidents` estava parado às 09:33 enquanto
o ecrã continuava a dizer «a correr».

**O que passou a existir.** `deploy/v2_deployment_components.json` declara de
que componentes é feito um deploy e quais deles não são opcionais. O wrapper
lê-o (`scripts/v2_deployment_components.py`) e constrói dali os `-f`. Já não há
nenhum caminho em que esquecer uma flag seja possível, porque já não há flag
nenhuma para esquecer.

Declarar o ficheiro só prova que o Compose foi *informado*. Por isso o manifesto
carrega também asserções, verificadas duas vezes:

* `check-rendered`, antes de arrancar seja o que for, contra a configuração
  fundida que o Compose vai aplicar;
* `check-live`, depois do `up -d`, contra o ambiente do contentor que ficou
  mesmo a correr — o caso que a primeira não apanha, que é um serviço que já
  estava de pé e que nada obrigou a ser recriado.

Qualquer uma das duas falha o deploy em vez de o deixar passar degradado.

**Como se desliga agora.** Editando o `ENABLED` para `"false"` no ficheiro do
próprio componente e alterando a asserção no manifesto. Tirar o `-f` deixou de
ser uma maneira de o fazer — e era exactamente por ser uma maneira acidental de
o fazer que isto aconteceu duas vezes.

## 2. A sincronização de produção do FusionSolar

Falhou **todos os dias** desde 2026-08-24, sempre com `rate_limited`, sempre
depois de três tentativas. São dois defeitos, e um agravava o outro.

### 2.1 O cooldown de rate limit tinha duração zero

`sync/service.py::record_request_result` calculava
`now + timedelta(seconds=error.retry_after_seconds or 0)`.

O FusionSolar comunica o próprio travão como `failCode 407` no corpo JSON, não
como um HTTP 429 com cabeçalho `Retry-After`. Logo `retry_after_seconds` chega
sempre a `None`, o `or 0` transformava-o em zero, e `cooldown_until` ficava
igual ao instante em que era escrito. **A protecção persistida nunca adiou
nada.** Vê-se nos dados: cada tentativa `rate_limited` tem `retry_after_at`
0,3 s depois de `occurred_at`.

O efeito prático a 2026-08-30: recusado às 15:59:07, nova chamada real às
16:00:08, outra às 16:05:10 — três pedidos HTTP contra uma conta que acabara de
dizer que não, cada um a somar à pressão de frequência que causou a recusa.

Nenhum teste apanhava isto porque **todos** passavam `retry_after_seconds=60`
explicitamente. O caso real — o provider não dizer nada — nunca era exercido.

**Correcção.** `DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 600`, aplicado quando e só
quando o provider não indica janela (`is None`, não `or`: um provider que diga
zero é acreditado). Dez minutos é medido, não escolhido — é o tempo de
recuperação observado desta conta depois de uma rajada de chamadas seguidas,
documentado no cabeçalho do `docker-compose.v2.yml`. Isto **acrescenta** uma
protecção; não relaxa nenhuma.

### 2.2 A janela incremental não cabia numa corrida, e por isso nunca avançava

O cursor estava em 2026-07-31. Cada corrida pedia toda a janela em falta de uma
vez — uma chamada por dia, encadeadas. A 2026-08-30 foram 31 chamadas em
dezasseis segundos; foi recusada na trigésima.

E aqui fecha o laço: o cursor só avança sobre uma janela **completa**. Uma
corrida parcial não avança nada. Portanto no dia seguinte a janela era um dia
mais larga e falhava um dia mais cedo. A 2026-08-31 estava exactamente nos 31
dias de `production_max_source_days`; a 2026-09-01 tê-lo-ia ultrapassado e a
sincronização passaria a falhar em `configuration`, antes sequer de tentar.

**Correcção.** `production_incremental_chunk_days` (7 por omissão). Cada corrida
pede um pedaço pequeno o suficiente para terminar; porque termina, o cursor
avança exactamente sobre os dias que foram mesmo cobertos; e a corrida diz por
onde continuar, com uma pausa
(`production_incremental_chunk_pause_seconds`, 300 s) antes do pedaço seguinte —
a pausa é o que impede que partir a rajada em pedaços a volte a juntar.

Duas coisas que **não** mudaram, de propósito:

* `production_max_source_days` continua a medir a **falha inteira**, não o
  pedaço. Uma janela larga de mais para ser trabalho normal continua a ser
  recusada e continua a exigir um backfill explícito. O que o chunking remove é
  a razão pela qual a falha crescia todos os dias.
* Um pedaço que não termine **não** pede para continuar. Continuar a partir de
  um cursor que não se mexeu seria pedir os mesmos dias outra vez, que é o laço
  que isto existe para quebrar.

## 3. «Automações» passou a ser saúde a sério

A página lia `schedule_state.last_enqueued_at` e `next_run_at` — o agendador a
dizer «pus um job na fila» — e mostrava isso como **a correr**. Se o job depois
correu, e se correu bem, nunca era perguntado.

Passaram a ser duas perguntas separadas, que falham por razões diferentes e se
resolvem de maneiras diferentes:

* **Saúde do agendador** — alguém ainda enfileira isto? Prova: `schedule_state`.
* **Saúde da execução** — o trabalho correu bem? Prova: os `jobs` que daí
  saíram, e os `sync_runs` por trás deles.

Estados: `desligada`, `nunca_executada`, `agendada`, `ok`, `a_executar`,
`degradada`, `falhou`, `atrasada`. O agendador é consultado primeiro: uma
automação que ninguém enfileira não tem saúde de execução digna de nota — a
última corrida pode ter sido um sucesso perfeito há três semanas, e mostrar
**ok** por causa disso é precisamente a falha que isto veio acabar.

**A pulsação separa dois casos que se pareciam.** `system.noop.hourly` prova que
o ciclo do agendador está vivo. Com a pulsação fresca, um agendamento em atraso
é o agendador vivo a **não** enfileirar aquele em particular — que é o aspecto,
visto de fora, de um interruptor a false ou de um override que caiu do deploy:
`desligada`. Com a pulsação parada, não se pode concluir nada sobre nenhuma
automação em particular, e dizê-lo é mais útil do que acusá-las uma a uma:
`atrasada`.

**Providers já não são colapsados.** As linhas derivam dos `schedule_state` que
existem, não de uma lista escrita no código. `production.incremental:3`
(FusionSolar) e `production.incremental:5` (Sigenergy) são duas contas, dois
limites e dois modos de falha; a versão anterior casava ambas contra uma
definição genérica e mostrava a primeira que encontrasse — uma semana de falhas
do FusionSolar representada por um sucesso do Sigenergy. Pelo mesmo motivo,
`monitoring.current:3` e `:5` corriam de quinze em quinze minutos sem linha
nenhuma no ecrã, e uma chave que o catálogo não conheça passa a ter linha com o
próprio nome — uma automação invisível é pior que uma com nome feio.

**O motivo da falha nunca repete a mensagem crua da excepção.**
`jobs.error_message` apanha excepções arbitrárias, e uma da camada de base de
dados carrega a connection string. Mostra-se o nome da classe da excepção e o
`error_code`/`safe_detail` do `sync_run` — o campo que existe justamente para
ser repetido.

## 4. A hierarquia dos interruptores de notificação

Havia indicação de que `NEMSEI_V2_NOTIFICATIONS` podia estar a `false` com
mensagens reais a serem entregues. Estava, e eram: **316 eventos `sent`, o mais
recente às 06:24 de 2026-08-31**, com o interruptor global a dizer false.

A causa não é semântica ambígua — é que a variável **não era lida por lado
nenhum**. Chegava a `Settings.capabilities` e parava aí: `provider_reads` é
consultada em oito sítios do código, `notifications` em zero. A intenção
histórica é inequívoca (`safety/external_actions.py`: *"Default-deny policy for
all V2 external capabilities"*, e `notifications` está em `CAPABILITIES` desde o
início); simplesmente nunca foi ligada ao caminho de entrega.

**A hierarquia, decidida e agora implementada, de fora para dentro:**

| # | Interruptor | Onde vive | O que decide |
|---|---|---|---|
| 1 | `NEMSEI_V2_NOTIFICATIONS` | ambiente | Se este processo **pode** entregar. Default-deny. |
| 2 | `NEMSEI_V2_NOTIFICATION_PROCESSING_ENABLED` | ambiente | Se o agendador enfileira o ciclo periódico. Decide **quando** se tenta, não se é permitido. |
| 3 | `NotificationPolicy.enabled` | base de dados | Que incidentes se tornam eventos. |
| 4 | `NotificationChannel.enabled` | base de dados | Para onde um evento pode ir. |
| 5 | Token do bot montado | segredo | Cliente real ou mock. Sem token não sai nada. |

O nível 1 corta a rede a sério, em dois sítios independentes:
`deliver_pending_notifications` e `deliver_digest` não tentam nada (os eventos
ficam `pending`, não `failed` — um interruptor não é uma falha de entrega, e
voltar a ligá-lo entrega o atraso); e `default_client_factory` devolve um
`DeniedTelegramClient` que **levanta** em vez de fingir. Nem devolve falha, que
marcaria eventos reais como `failed` para sempre, nem cai no mock, que os
marcaria `sent` sem nada ter saído. Um kill switch que mente sobre o que fez não
é um kill switch.

**Sobre o valor.** `.env.v2` passou de `false` a `true`. A mudança é de *nome*,
não de comportamento: este deployment já escolhera notificações ligadas por
todas as outras vias (token montado, canal activo, políticas ligadas, cliente
HTTP aprovado explicitamente a 2026-08-25), e o `false` nunca exprimiu decisão
nenhuma porque nunca fez nada. Deixá-lo a `false` teria desligado o Telegram
como efeito secundário de corrigir o interruptor — e isso ninguém decidiu. Para
cortar mesmo: `false` e redeploy. A partir de agora corta.

## 5. Sync runs abandonados

Um `SyncRun` é aberto por um processo e fechado por esse mesmo processo:
`finish_sync_run` recusa uma corrida que não esteja `running`, e não havia
lease, nem coluna de dono, nem recuperação. Um processo que morra entre as duas
coisas deixa uma linha a dizer `running` para sempre. **Não havia crash recovery
nenhuma para `sync_runs`** — só os `jobs` a tinham, via lease.

Havia duas: a corrida 4 (produção FusionSolar, aberta a 2026-08-19, um dia lido
e depois silêncio) e a 43 (produção Sigenergy, aberta a 2026-08-25, autenticou e
depois silêncio). Nenhuma foi criada por um job — foram abertas à mão durante os
rollouts desses dias, e o terminal que as abriu já não existe.

As corridas SCADA da ligação 6 que também apareciam como `running` **não** são
disto: uma sessão de dongle dura o tempo que durar a ligação e fecha-se sozinha.
Confundi-las teria sido matar corridas vivas.

**Critério determinístico**, não um timeout às cegas — `sync/abandonment.py`:

* **Prova forte, sem espera:** uma corrida SCADA cuja sessão de dongle já está
  fechada. O dono não se limitou a calar-se; chegou ao próprio fim sem fechar a
  corrida, e essa combinação só existe quando a corrida ficou órfã.
* **Prova por silêncio:** nenhuma evidência de vida durante mais de uma hora.
  Uma corrida que trabalha escreve um `provider_request_attempt` por chamada,
  cerca de duas por segundo; uma corrida cuja evidência vive noutro sítio traz o
  seu próprio `OwnerLiveness` para o dizer. `nemsei.sync` continua neutro quanto
  a providers: o adaptador SCADA injecta o seu resolvedor, e o handler do job é
  que os compõe.

**Não se reescreve história.** A corrida mantém `started_at`, metadados e todos
os factos que escreveu. Ganha estado terminal, `error_code = "abandoned"` e
`finished_at` no último momento em que se **sabe** que esteve viva — não no
momento em que se deu por ela; uma corrida que parou no dia 19 não correu doze
dias, e dizer que sim estragava todas as durações derivadas destas linhas. E não
toca em `integration_health`: essa responde «como está esta ligação agora», e
uma corrida que se calou há nove dias não é notícia sobre agora.

Corre como `sync_runs.sweep_abandoned`, de 15 em 15 minutos, ligado por
omissão — é recuperação de falhas, não uma capacidade, faz zero chamadas a
provider e só toca em linhas que já ninguém consegue alcançar.
