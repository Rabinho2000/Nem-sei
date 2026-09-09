# ING-001 — recolhas com dono, e provadas

## 1. O problema

O V2 tinha três formas de dizer que correu bem e nenhuma de dizer que
recolheu.

`jobs` diz que um handler correu e voltou. `sync_runs` diz que se falou com o
provider e como correu. Nenhuma das duas responde à pergunta que interessa —
**os dados que devíamos ter recolhido chegaram?** — e a Fase 0 mostrou o que
isso custa: um run Sigenergy terminou `success`, avançou o cursor, e guardou o
total de um dia lido a meio desse dia. Todos os contadores diziam que o
trabalho estava feito.

Havia ainda dois buracos por baixo disso:

**Ninguém verificava o dono.** `lease_token` aparecia **zero vezes** em
`integrations/`, `monitoring/` e `sync/`. O token chegava ao `execute()` e
morria aí, por isso cada facto, cada revisão e cada avanço de cursor era
escrito sem uma única verificação. Um worker cujo lease de 30 s expirou a meio
do handler continuava a escrever, e o worker que recuperou o job escrevia as
mesmas linhas pelo outro lado.

**Nada serializava dois escritores.** O índice único parcial em
`(job_type, dedupe_key)` impede jobs duplicados na fila. Isso é dedupe, não
locking: não diz nada depois de o job sair desses estados, nada sobre um job
recuperado enquanto o handler antigo ainda está vivo, e nada sobre as linhas
que o handler escreve.

## 2. `execution_success` vs `collection_fulfilled`

A regra que este trabalho existe para impor:

> um handler que volta sem exceção teve **execution success**.
> isso não é **collection fulfilment**.

`JobOutcome(status="success")` continua a existir e continua a querer dizer o
que sempre quis: o handler não rebentou. O que deixou de fazer é decidir se a
obrigação foi cumprida.

`finalize_collection_run` **não aceita um status**. Aceita evidência e deriva
o status dela. Quem chama não consegue pedir `fulfilled`; só consegue fornecer
contadores que por acaso satisfazem o predicado. Se não satisfizerem, o run
fica `partial` ou `failed` por muito confiante que o handler estivesse.

## 3. Fencing

### `lease_token` vs `lease_generation`

`lease_token` identifica uma reclamação. Não consegue **ordenar** duas: é um
`secrets.token_urlsafe(24)` novo a cada claim, por isso quando um lease expira
e o job é reclamado outra vez, o token antigo e o novo são apenas
*diferentes*, sem forma de dizer qual veio primeiro.

`lease_generation` é essa ordem, vinda de `jobs_lease_generation_seq`.

**Um só sítio aloca.** O `UPDATE` dentro do `claim_next` que realmente toma
posse. Não é um default da coluna — isso gastaria uma generation em qualquer
INSERT e convidaria um `UPDATE` noutro sítio a avançá-la sem ninguém dar por
isso. `recover_expired` **limpa** a coluna em vez de alocar; não há segundo
bump nem segunda fonte de verdade.

**Linhas antigas ficam a `NULL`**, sem backfill. `NULL` não iguala nenhum
fence, por isso um job reclamado antes disto existir não consegue provar nada
e não pode escrever nada. Falha fechada.

### Expiração revoga sozinha

`assert_ownership` falha num lease expirado **mesmo que ninguém tenha
reclamado o job** e a generation continue a ser a mais recente da tabela.

Isto importa mais do que o roubo e é mais fácil de errar. A falha comum não é
um segundo worker roubar o job — é um worker calar-se durante mais tempo do
que o lease e depois terminar como se nada fosse. Aí não há segunda parte com
quem comparar. Só há o relógio.

### As duas verificações

```
BEGIN
  advisory xact lock no scope
  assert_ownership  #1
  factos / revisões
  cursor
  assert_ownership  #2
  finalize collection_run
COMMIT
```

A segunda não é duplicação defensiva. A primeira prova que este worker era
dono quando as escritas começaram; um lease de 30 s pode expirar enquanto
essas escritas decorrem legitimamente. Só uma verificação **depois** da última
escrita diz alguma coisa sobre o momento do commit.

Se a segunda falha, toda a transação é descartada: sem factos, sem cursor, sem
fulfilled.

## 4. Locking

`pg_advisory_xact_lock`, com chave derivada de:

```
connection_id | capability | scope_kind | scope_key | period_start | period_end
```

**Porquê advisory e não `SELECT ... FOR UPDATE`.** Um row lock precisa de uma
linha, e a primeira recolha de um scope não tem nenhuma — o run que seria
bloqueado é o run que está prestes a ser criado. Bloquear a linha da connection
serializaria capabilities que nada têm a ver umas com as outras. Uma chave
advisory bloqueia a *ideia* do scope, que é o que não pode ter dois
escritores, haja ou não linhas.

**Transacional, não de sessão.** Liberta no COMMIT ou ROLLBACK. Nada para
desbloquear à mão, nada retido por um processo que morreu.

**Granularidade: pelo menos tão grossa como o cursor.** `sync_cursors` é único
por `(provider_connection_id, capability, cursor_key)`, portanto todos os
assets de uma connection partilham um cursor de produção. Um lock por asset
deixaria dois detentores avançar esse mesmo cursor em simultâneo — que é
precisamente a corrida que o lock existe para impedir.

**BLAKE2b, não `hash()`.** O `hash()` do Python é salgado por processo, por
isso uma chave feita com ele concordaria consigo mesma durante toda a suite e
discordaria entre dois workers — um lock que nunca bloqueia e nunca falha um
teste. Um dos testes corre três subprocessos com `PYTHONHASHSEED` diferentes,
porque é a única forma de provar isso. Também não é `hashtextextended()`: em
Python a chave é igual entre versões de PostgreSQL e testável sem base de
dados.

## 5. Fronteiras transacionais

**A chamada ao provider fica fora.** O ciclo busca e faz parse para memória
primeiro, e a transação abre só depois de a rede estar despachada. Nenhuma
transação Postgres fica aberta à espera do Sigenergy. O buffer é limitado por
`max_days` × mappings-por-connection — sete dias sobre um punhado de sistemas —
o que é mais barato de segurar do que uma transação.

**O collection run é criado e commitado *antes* da transação autoritativa.**
Criado dentro dela, um run que perdesse o lease era descartado com tudo o
resto — não sobrava linha para marcar `lost_ownership`, e a perda ficava por
registar. Uma linha `running` deixada por um worker que morreu também não é
defeito: é o registo honesto de que uma tentativa começou e nunca reportou.

**A perda de ownership é registada noutra transação.** `mark_collection_run_
lost_ownership` recebe um id, não uma instância, porque a instância pertence à
transação que acabou de ser descartada. Devolve `None` em vez de rebentar
quando a linha desapareceu ou já é terminal: falhar a registar um diagnóstico
nunca pode ressuscitar um commit inválido.

## 6. Cursor

`advance_cursor` passou a aceitar um fence e a ler a linha `FOR UPDATE`. O
advisory lock já serializa os escritores que este módulo espera, mas o lock e
a linha são objetos diferentes, e um futuro chamador que se esqueça do lock
continua a não conseguir intercalar um read-modify-write ali.

O cursor nunca regride. Se A quer D-2 e B quer D-1, o lock serializa-os e o
perdedor vê o valor do vencedor; a guarda monotónica recusa recuar.

## 7. Revisões

O modelo mantém-se: "atual" é `MAX(source_revision)` por
`(provider_mapping_id, source_fact_key)`, com o unique constraint existente.
**Não** foi acrescentado `is_current`.

O advisory lock evita que dois escritores calculem `N+1` ao mesmo tempo. O
unique constraint fica como última defesa, não como mecanismo normal — que é a
diferença entre uma corrida tratada e um `IntegrityError` a rebentar a fila.

## 8. Closed-day

Para produção diária histórica, um dia D só pode ser recolhido depois de
fechar **no timezone contratual do provider**, nunca pela data do servidor.

A janela é limitada por `last_closed_day`, e `CollectionEvidence.period_closed`
volta a afirmá-lo no predicado — declarado em vez de assumido, porque o clamp
está a uma edição de distância de se perder. Uma leitura cumulativa tirada
durante D não satisfaz a obrigação de dia fechado de D.

É a forma exata do defeito que escreveu 145 factos Sigenergy com um contador
em andamento como se fosse o total do dia.

## 9. Política de histórico

**Não há retro-materialização.** Zero collection runs para factos anteriores.

> um facto existente **não é** prova de recolha cumprida

Os 145 factos Sigenergy errados não recebem selo nenhum por esta feature
existir, e não foram reparados. Continuam como estão.

## 10. Estados

```
pending → running
running → fulfilled | partial | failed | lost_ownership | cancelled | superseded
```

`failed → fulfilled` está ausente de propósito, e é a ausência mais importante
da tabela. Uma reparação é uma **linha nova** com `attempt + 1`, para que o
registo do que correu mal sobreviva em vez de ser sobrescrito pelo que acabou
por funcionar.

`superseded` a partir de `running` é o replay: um scope que outro run já
recolheu. O trabalho foi real e idempotente; o fulfilment é que não era deste
run para reclamar. Detetado sob o lock, para que o índice único continue a ser
última linha de defesa e não o que decide.

`partial` é derivado de evidência ter chegado, não da ausência de erro. Um dia
que devolveu quatro das cinco métricas escreve factos e não completa scope
nenhum; chamar-lhe `failed` esconderia a parte que chegou de tudo o que conta
cobertura.

## 11. Constraints

O predicado está escrito duas vezes — no serviço e como CHECK. A duplicação é
deliberada: o serviço é de onde vem uma mensagem de erro útil, e a constraint
é o que continua a valer quando alguém acrescentar um segundo caminho de
escrita daqui a um ano sem saber que o serviço existe.

```sql
status <> 'fulfilled' OR (
  lease_generation IS NOT NULL
  AND scopes_required IS NOT NULL
  AND scopes_written = scopes_required
  AND finished_at IS NOT NULL
)
```

`scopes_required NULL` quer dizer que ninguém contou, e nunca pode cumprir:
**um denominador desconhecido não é um denominador cheio.**

Índice único parcial sobre as linhas `fulfilled` impede dois runs a fecharem o
mesmo scope lógico — recusado pela base de dados, não apenas evitado pelo lock.

## 12. Limitações conhecidas

- **Lease de 30 s sem heartbeat.** O fence resolve a corrupção — o worker
  antigo deixa de escrever — não o desperdício: o trabalho dele perde-se na
  mesma. Heartbeat fica para depois.
- **Runs `running` órfãos.** Um worker que morre a meio deixa a linha em
  `running` para sempre. É honesto, mas acumula; falta uma varredura
  equivalente à `sync_runs.sweep_abandoned`.
- **Transação mais longa no Sigenergy.** As escritas de toda a janela passaram
  a estar numa transação em vez de uma por mapping-dia. Com `max_days=7` e
  poucos sistemas isto é pequeno; se crescer, a saída é reduzir a janela por
  run, **não** voltar a partir o commit.
- **Só produção Sigenergy está integrada.** Ver abaixo.

## 13. Ainda não integrado

| pipeline | estado |
|---|---|
| FusionSolar production | não integrado — já tem `FOR UPDATE` no cursor e factos+cursor no mesmo commit, portanto não sofre da divisão que o Sigenergy sofria; ganha o fence e o collection run numa fatia própria |
| device history | não integrado |
| current monitoring | não integrado |
| Huawei SCADA rollup | não integrado — deriva localmente, sem chamada ao provider |
| Huawei SCADA listener (push) | **fora de âmbito por desenho** — não tem job nem lease, e escreve amostras, não factos canónicos |
| importadores V1 (`v1_import`, `v1_reporting_import`) | fora de âmbito — offline, sem job |
| ING-002 obligations | não começado |
