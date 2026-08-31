# Huawei SCADA/NMS — receção direta do SDongle na V2

Este documento descreve a integração **`huawei_scada`**: o SDongle Huawei liga-se
ao servidor V2 e o servidor lê-lhe registos Modbus por essa mesma ligação. Não há
edge collector, não há túnel (Pinggy, ngrok, frp) e não há `socat`. O único
caminho é o NAT do router do cliente até à porta que o serviço publica.

Última revisão: 2026-08-25.

## 1. O que é diferente neste provider

Todos os outros providers da V2 são **de saída**: a V2 autentica-se, chama um
endpoint e recebe uma resposta. Este é **de entrada**. Isso muda três coisas de
forma estrutural, e é por isso que o código não se parece com o do FusionSolar
nem com o do Sigenergy:

| | FusionSolar / Sigenergy | Huawei SCADA |
| --- | --- | --- |
| Quem inicia | a V2 | o dongle |
| Credencial | conta + segredo montado | nenhuma — a identidade é o número de série anunciado |
| Orçamento de chamadas | limite da conta, com `ProviderRequestState` | não aplicável; a cadência é nossa |
| Processo | job no `worker` | processo próprio com uma porta TCP |
| Erro típico | HTTP 401/429 | ligação cai e o dongle volta a ligar |

Consequência prática: **não existe capacidade de "validar ligação"**. Uma ligação
Huawei SCADA está a funcionar quando um logger está ligado, o que é uma pergunta
de estado (`IntegrationHealth`, `huawei_scada_sessions`), não um pedido que se
possa fazer a pedido.

## 2. A evidência do protocolo

Tudo o que se segue foi observado nas duas instalações do piloto. O que não foi
observado está na secção 7 e **não está implementado**.

### 2.1 Mensagem de abertura

```
ADV(J1=SDongle...;2=V...;3=P...;4=<numero_de_serie>;5=100;6=1.3
```

| Campo | Significado | Uso na V2 |
| --- | --- | --- |
| `1` | modelo | metadata da sessão |
| `2` | versão de software | metadata da sessão |
| `3` | produto | metadata da sessão |
| `4` | **número de série do dongle** | **a identidade — é só isto que decide a que central pertencem os dados** |
| `5` | unidade Modbus agregada (`100`) | é a unidade que se interroga; lida do anúncio, não fixada em código |
| `6` | versão de protocolo | metadata da sessão |

> **`ADV(J1=...` não existe na linha.** Descoberto quando o primeiro dongle real
> se ligou, a 2026-08-27, e **todas as sessões morreram em `idle_timeout`**. Os
> bytes verdadeiros do SDongleA-05 são:
>
> ```
> 00 00 00 00 00 5b 00 41 44 00 56 01 05 2c 00 00 06 00 01 01 00 12 4a 31 3d ...
>                      A  D  \0 V                                   J  1  =
> ```
>
> É uma trama binária proprietária cujos bytes imprimíveis se leem como
> "AD V ... J1=". Não há `(` nenhum, e o "A", "D" e "V" nem são contíguos — a
> forma no cabeçalho desta secção é uma transcrição humana, não o protocolo. O
> parser localiza agora a **corrida de campos `N=valor;`**, a única parte
> genuinamente textual, que sobrevive a qualquer invólucro.

O anúncio **não tem terminador**, por isso `extract_advertisement` só o dá por
completo quando aparece `)`, CR, LF ou NUL — **ou** quando o par fica em
silêncio. Duas subtilezas que custaram testes vermelhos:

- **Aceitar assim que o número de série fecha perde os campos seguintes**, e o
  campo 5 é a *unidade Modbus a interrogar*. Um dongle que respondesse noutra
  unidade nunca seria lido.
- **O `)` é imprimível**, por isso fica *dentro* do match; CR, LF e NUL ficam a
  seguir. As duas posições têm de ser verificadas.

### 2.2 Registos agregados (`unit=100`)

| Registo | Hex | Sinal | Tipo | Escala | Coluna |
| --- | --- | --- | --- | --- | --- |
| 37498 | `0x927A` | entrada total / PV | U32 | 1000 | `pv_input_power_kw` |
| 37500 | `0x927C` | potência da carga | U32 | 1000 | `load_power_kw` |
| 37502 | `0x927E` | potência da rede | I32 | 1000 | `grid_power_kw` |
| 37504 | `0x9280` | potência da bateria | I32 | 1000 | `battery_power_kw` |
| 37516 | `0x928C` | potência activa total | U32 | 1000 | `total_active_power_kw` |

Uma leitura cobre 37498–37517 num só pedido de 20 registos. Firmware que recuse a
leitura larga cai automaticamente para cinco leituras de dois registos, e a V2
lembra-se dessa decisão em vez de a repetir em cada ciclo.

**Sentinelas.** `0xFFFFFFFF` (U32) e `0x7FFFFFFF`/`0x80000000` (I32) significam
"indisponível" e descodificam para `None`. Descodificá-las como número daria
4 294 967,295 kW — um valor fabricado com aspecto de leitura.

Também são legíveis: software do dongle em `30050` (`0x7562`, ASCII) e
identificação de dispositivo pela sequência `2B 0E 03 87`. Ambos são
informativos; uma recusa de qualquer um deles não afecta a sessão.

### 2.3 O inversor recusa-se a responder

Nas duas instalações testadas, ler `unit=1` devolveu `function=0x83`,
`exception=0x04` (*slave device failure*). **Isto é tratado como limitação
conhecida do downstream**, não como avaria:

- é descodificado com nome (`slave_device_failure`), não como erro de parsing;
- é registado em `huawei_scada_sessions.metadata_json.downstream_probe`, para que
  o dia em que mudar seja visível;
- **a sessão continua a recolher os dados agregados do `unit=100`** — provado por
  teste, tanto ao nível da sessão como ponta-a-ponta no listener.

Nenhuma leitura ao `unit=1` é obrigatória, e o registry **não declara**
`DEVICE_MONITORING` para este provider: não há contrato de dispositivo para
implementar enquanto nenhuma unidade a jusante responder.

**Mas o inversor é identificável.** A lista de dispositivos (`2B 0E 03 87`) no
`unit=100` devolve *dois* descritores: objecto `0x88` com o dongle e `0x89` com
o inversor — no piloto, `SUN2000-20KTL-M5`, série `TA2310347396`. Descobre-se o
inversor sem o conseguir ler. Não está explorado.

## 3. Estrutura do código

```
src/nemsei/integrations/huawei_scada/
  protocol.py    MBAP, frames fragmentados/agrupados, ADV, device list,
                 U32/I32, escalas, sinais, sentinelas. Sem I/O nenhum.
  session.py     A conversa: handshake, polling, timeouts, keep-alive,
                 sondagem do unit=1. Transporte injectado — sem socket.
  listener.py    O ÚNICO ficheiro com socket. Aceita ligações, uma thread
                 por dongle, binding único.
  ingestion.py   Identidade, quarentena, amostras append-only, observação
                 canónica, estado da sessão, saúde.
  rollup.py      Potência → energia, por integração declarada.
  retention.py   Limpeza de amostras cujo dia já é definitivo.
  service.py     O contrato verificado da ligação, e o fingerprint do par.
  models.py      As três tabelas.
```

A separação não é estética: é o que permite testar frames partidos, um reconnect
a meio de um polling e a recusa `0x83`/`0x04` **sem rede nenhuma**. O teste
`test_only_the_huawei_listener_touches_a_socket` fecha essa porta à chave.

**Só leitura, estruturalmente.** `protocol.py` não tem codificador para a função
`0x06` nem `0x10`. Os únicos frames que sabe construir são `0x03` (ler registos) e
`0x2B` (identificação). Isto é verificado por dois testes, um deles nas fronteiras
de arquitectura — não é uma promessa em prosa.

## 4. Persistência

Três tabelas (migração `0026_huawei_scada`), mais o alargamento da constraint
`ck_provider_connections_provider_code`.

### `huawei_scada_power_samples`

Append-only, com o mesmo modelo de revisão/supersessão do `production_facts`.
Guarda **potência em kW**, nunca energia — as colunas trazem a unidade no nome
precisamente porque confundir kW com kWh é o erro mais caro disponível aqui.

A identidade de uma amostra é `(provider_mapping_id, source_sample_key,
source_revision)`, onde a chave é o número de série mais o instante **quantizado
para a grelha de amostragem** (`sample_bucket_seconds` = intervalo de polling).
É isto que faz com que **um reconnect não duplique dados**: uma releitura do mesmo
intervalo com o mesmo valor não escreve nada, e com valor diferente escreve uma
revisão que supersede a anterior.

O `observed_at` é **a hora de receção do listener**, porque o protocolo não traz
timestamp nenhum. Cada linha di-lo explicitamente
(`metadata_json.observed_at_source = "listener_receive_time"`).

### `huawei_scada_sessions`

Uma linha por ligação TCP: quem ligou, quando, por quanto tempo, quantas leituras,
quantos erros, e como terminou. É isto que faz um reconnect aparecer como
reconnect e não como uma lacuna nas amostras.

O endereço do par **não é guardado**. Guarda-se um `peer_fingerprint` — um digest
HMAC-SHA256 truncado, com sal do segredo da instalação. Chega para ver que "a
mesma origem voltou a ligar" e é estruturalmente inútil para decidir a que central
pertence um dongle, que é o objectivo.

### `huawei_scada_pending_dongles`

Quarentena. Um número de série sem mapping aprovado fica aqui, com o anúncio que
apresentou, e a sessão é fechada com `close_reason='unmapped_dongle'`. **Nada aqui
se torna um `AssetProviderMapping` sozinho.** Um número de série que um operador
rejeitou permanece rejeitado por muito que volte a bater à porta.

> **Porque é que nunca se mapeia pelo IP.** Uma regra de NAT editada no router do
> cliente, um lease de DHCP que muda, um cliente que muda de ISP, ou duas centrais
> atrás do mesmo endereço público — qualquer um destes atribuiria silenciosamente
> a produção de um cliente a outro. O número de série é a única entrada da
> decisão.

## 5. Energia e relatórios

`rollup.py` converte amostras de potência em `ProductionFact` diários. É um **job
durável no worker**, sobre linhas que a base já tem: zero chamadas a provider, e
não cria `SyncRun` nenhum.

Regras, todas gravadas na metadata de cada facto:

- **integração trapezoidal** entre amostras consecutivas, no **dia local do
  asset** (por isso um dia de mudança de hora integra sobre 23 h ou 25 h reais);
- **lacunas maiores que `MAX_SAMPLE_GAP_SECONDS` são excluídas, nunca
  interpoladas** — assumir que a potência a meio de uma falha de quatro horas foi
  a média dos extremos seria inventar energia;
- um registo ausente **quebra o segmento**, não vale zero;
- tempo a andar para trás conta como `clock_anomalies` e não acrescenta energia;
- `quality='partial'`, `metadata.measurement_method='power_integral'`,
  `metadata.estimated=true`, **sempre** — uma estimativa integrada não é um
  contador, por muito densa que seja a amostragem;
- `completeness='complete'` só quando o dia está coberto **de extremo a extremo**
  (as duas pontas a menos de uma lacuna da meia-noite local). Cobertura alta das
  horas que se viu não diz nada sobre as que não se viu.

Métricas escritas:

| Métrica | Origem | Condição |
| --- | --- | --- |
| `production_energy` | registo escolhido pelo contrato | sempre |
| `consumption_energy` | 37500 | sempre |
| `export_energy` | 37502, recortado | **só** com convenção de sinal verificada |
| `grid_import_energy` | 37502, recortado | **só** com convenção de sinal verificada |
| `self_use_energy` | consumo − importação | só se pedido, com convenção verificada **e** sem fluxo de bateria nesse dia |

O autoconsumo é a regra mais restritiva de propósito: `consumo − importação` só
iguala autoconsumo quando nada mais absorve a diferença, e é exactamente essa a
razão pela qual `monitoring/models.py` se recusa a derivar entre métricas em
geral. Um dia com **qualquer** fluxo de bateria é saltado, com aviso, não estimado.

### Fonte primária ou fallback

O rollup só escreve factos para o mapping que a `asset_source_policies` do asset
seleciona como fonte de produção nesse dia. Isto não é zelo: `build_dataset` soma
os factos de **todos** os mappings de um asset, por isso escrever à revelia da
política duplicaria a produção de qualquer central que tenha também FusionSolar
ou Sigenergy. Pôr o Huawei como fallback é uma linha em `asset_source_policies`,
sem alteração de código.

### O que o relatório mostra

Os relatórios continuam **database-only** — nenhuma chamada a provider durante a
geração, garantido por teste. E a origem fica visível:

- a linha do dataset fica em `actual_state='partial'` (nunca `measured`);
- `provenance_json.estimated_fact_keys` nomeia os factos estimados — a chave só
  aparece quando existe algum, porque acrescentá-la sempre mudaria o
  `input_digest` de todos os datasets já congelados sem um único número ter
  mudado;
- `quality_json.actual_sources` diz de onde veio a energia;
- o payload traz `energy_estimated`, `energy_estimated_months`, `energy_sources` e
  a nota `energy_integrated_from_power_samples_not_metered`.

## 6. O contrato verificado

Como no FusionSolar e no Sigenergy, o que não foi verificado **recusa-se a
correr** em vez de adivinhar. Por `credential_reference` da ligação:

| Variável | Obrigatória | Porquê |
| --- | --- | --- |
| `..._POWER_UNIT` | sim, `kW` | os registos são inteiros com ganho 1000; se isto estiver errado, todos os kWh ficam errados por um factor de mil e continuam plausíveis |
| `..._PRODUCTION_SIGNAL` | sim | 37498 (entrada PV) e 37516 (activa total) são medições diferentes, separadas pelas perdas de conversão. **Não há valor por omissão.** |
| `..._GRID_SIGN_CONVENTION` | não | sem ela, **não** se escreve exportação nem importação. Metade certo não é opção. No piloto a evidência diz `positive_import` — ver §11. |
| `..._SELF_USE_DERIVATION` | não | exige a convenção acima; ver as regras em 5 |
| `..._MAX_SAMPLE_GAP_SECONDS` | não (900) | o corte de lacunas da integração |

## 7. O piloto: rede, TLS e riscos

### Montagem

```
SDongle (LAN do cliente) ──► router do cliente ──► Internet
                                                     │
                        NAT: porta pública ──────────┘
                                                     ▼
                                  servidor Linux 192.168.1.237:1502
                                                     │
                                            serviço `scada-listener`
```

O endereço e a porta publicados vêm do ambiente
(`NEMSEI_V2_SCADA_BIND_ADDRESS`, `NEMSEI_V2_SCADA_BIND_PORT`) e o valor por
omissão é `127.0.0.1` — só loopback. **Nenhum endereço público, porta pública ou
segredo está escrito em código nem no Compose**, e há um teste que o verifica.

### Arranque

O serviço está atrás de um profile e **não** arranca com o `up -d` canónico:

```bash
docker compose -f docker-compose.v2.yml --env-file .env.v2 --profile huawei-scada up -d --build scada-listener
```

### Binding único

Duas garantias, porque a porta sozinha não chega:

1. **A porta.** Só um serviço a publica; um segundo processo falha o bind e morre
   de forma ruidosa.
2. **Um advisory lock do PostgreSQL sobre o id da ligação.** Apanha o caso que a
   porta não apanha — um segundo listener noutra máquina ou noutra porta apontado
   à *mesma* ligação, que duplicaria todas as amostras sem colisão nenhuma para
   dar por isso. O lock é tomado numa ligação dedicada, porque um advisory lock
   dura o que durar a sessão que o tomou.

O listener **nunca** corre dentro do web worker. O gunicorn pode reiniciar ou
multiplicar workers a qualquer momento, e cada um desses eventos ou disputa a
porta ou abre um segundo listener que responde a metade dos dongles.

### TLS: desligado no piloto

**O piloto corre em claro.** É uma decisão consciente e limitada ao piloto. O que
isso significa, dito sem rodeios:

- quem estiver no caminho **vê** as leituras de potência da instalação;
- quem estiver no caminho **pode falsificar respostas Modbus**, e a V2 não tem
  como distinguir uma resposta forjada de uma verdadeira — o protocolo não tem
  autenticação nenhuma, nem sequer um MAC;
- **qualquer pessoa na Internet pode ligar-se à porta** e anunciar-se com um
  número de série. A quarentena limita o estrago (um série desconhecido nunca é
  associado a uma central), mas **um série conhecido é suficiente para injectar
  amostras falsas**, e um número de série de dongle não é segredo;
- a porta publicada é uma superfície de ataque exposta: o `max_sessions` e os
  timeouts limitam a exaustão de recursos, não o acesso.

**Antes de produção**, pelo menos um destes tem de existir, e nenhum está
implementado:

1. TLS com certificado de cliente, se o firmware do dongle o suportar;
2. uma VPN ou túnel IPsec/WireGuard por instalação, com a porta a escutar apenas
   na interface da VPN;
3. lista de origens permitidas na firewall do router/servidor, como mitigação
   parcial — não resolve a escuta nem a falsificação, só reduz quem pode tentar.

Enquanto isso não existir, esta integração deve tratar-se como **piloto numa rede
controlada**, e os factos que produz continuam marcados como estimativa.

## 8. Pôr uma central a receber

1. Criar a ligação de provider `huawei_scada` (`configured` + `enabled`), com
   `credential_reference` (por exemplo `primary`) — não há credencial nenhuma para
   guardar; a referência só nomeia o conjunto de variáveis do contrato.
2. Preencher o contrato verificado (secção 6) no `.env.v2`.
3. Criar o `AssetProviderMapping` com `external_id` = **número de série do
   dongle**, `resource_kind='plant'`, `mapping_status='active'`.
4. Confirmar que o asset tem **fuso horário** — sem ele não há política de origem
   nem dia local, e o rollup salta a central em voz alta.
5. Criar as `asset_source_policies` que forem precisas: `monitoring` para que o
   estado da instalação venha do dongle, `production` para que o rollup escreva
   factos.
6. Configurar o NAT do router do cliente e arrancar o `scada-listener`.
7. Ligar o rollup (`..._ROLLUP_ENABLED`, `..._ROLLUP_CONNECTION_ID`).

Se um dongle ligar antes do passo 3, aparece em `huawei_scada_pending_dongles` com
o número de série que anunciou — que é a forma mais fácil de o descobrir.

Os passos 1, 3 e 5 fazem-se com `scripts/huawei_scada_onboard.py`, para não
andarem em SQL à mão, onde uma gralha atribui silenciosamente a produção de um
cliente a outro:

```bash
docker exec nemsei-v2-worker-1 python /app/scripts/huawei_scada_onboard.py create-connection
docker exec nemsei-v2-worker-1 python /app/scripts/huawei_scada_onboard.py status
docker exec nemsei-v2-worker-1 python /app/scripts/huawei_scada_onboard.py bind \
    --serial HV2340123456 --asset-id 153 --monitoring --production
```

`status` mostra as três coisas que interessam ao operador: quem está em
quarentena, que dongles estão ligados a que centrais (com fuso e políticas), e
que sessões estão abertas neste momento. `bind` recusa-se a correr contra um
asset inexistente, contra um asset sem fuso, ou contra um série já reclamado por
outra central.

## 9. Jobs e saúde

O listener é **guiado por eventos**. Não tem fila, não tem cadência de saída e
nenhum job o consegue arrancar ou parar. O que é durável são os dois jobs:

| Job | Cadência | O que faz |
| --- | --- | --- |
| `huawei_scada.rollup` | 60 min | reintegra os últimos `lookback_days` dias locais; correr outra vez **é** a reconciliação, e a regra append-only transforma-a em correcção, não em duplicado |
| `huawei_scada.retention` | diária | apaga amostras de dias que já têm um facto de produção `complete` e mais antigos que a janela; nunca apaga evidência que ainda não foi usada |

Ambos são partilham o `_enqueue_due_cycle` do `JobRepository`: `ScheduleState`
persistido (resiste a reinícios), `_catch_up_slot` (um agendador parado cinco dias
enfileira um ciclo, não o backlog inteiro) e chave de dedupe idempotente.

Saúde e `last_seen`:

- `IntegrationHealth` da ligação, escrita no fecho de cada sessão
  (`sync_state`, `last_success_at`, `last_failure_at`);
- um `SyncRun` por sessão identificada (`capability='current_monitoring'`,
  `metadata.transport='inbound_tcp_session'`), com contagens de leituras e erros;
- `huawei_scada_sessions.last_seen_at` por sessão, e
  `ingestion.stale_open_sessions` para encontrar sessões que um listener em
  crash deixou abertas;
- `MonitoringCurrentState` por mapping, quando a política de monitorização
  selecciona este mapping.

**Estado corrente derivado da potência**, pela mesma razão que no Sigenergy: o
bloco agregado não tem registo de estado nenhum. Geração positiva →
`operational`; leitura completa sem geração → `unknown` e **não** `offline`,
senão todas as centrais do país ficavam em baixo todas as noites.

## 10. O que não está implementado, e porquê

| Não existe | Razão |
| --- | --- |
| `CONNECTION_VALIDATION` | não há endpoint para chamar; "está a funcionar" é uma pergunta de estado |
| `DISCOVERY` | não há conta para enumerar; tratar um série que chega como descoberta fica a um passo de o associar automaticamente |
| `DEVICE_MONITORING` / `DEVICE_DISCOVERY` | `unit=1` responde `0x83`/`0x04` nas duas instalações |
| `PRODUCTION_HISTORY` | o dongle serve registos instantâneos e nenhuma série histórica. A energia diária existe, mas por integração local com zero chamadas — declarar a capacidade diria ao preflight de activação que existe uma leitura de provider que não existe |
| `PROVIDER_MUTATIONS` | nunca. Não há codificador de escrita em `protocol.py` |
| Exportação/importação sem convenção de sinal | 37502 é com sinal e nada observado diz o que significa positivo |
| TLS | secção 7 |
| Repartição horária/tarifária | as amostras existem, mas a V2 não tem factos sub-diários (ver `KNOWN_GAPS.md`) |

## 11. Resolver as três variáveis a partir dos dados reais

O preço de o contrato não ter valores por omissão é que ficam três campos em
branco e nada óbvio com que os preencher. `scripts/huawei_scada_verify_contract.py`
fecha essa lacuna: lê as amostras que o listener já gravou e diz o que elas
implicam, sem decidir nada e sem escrever nada.

```bash
docker exec nemsei-v2-worker-1 python /app/scripts/huawei_scada_verify_contract.py --days 3
```

| Pergunta | Como é respondida pelos dados |
| --- | --- |
| `POWER_UNIT` | pico de potência contra a potência instalada do asset. Um ganho errado por mil não parece subtilmente errado: parece um telhado de 30 kW a produzir 30 MW |
| `PRODUCTION_SIGNAL` | rácio entre 37516 e 37498 enquanto a central produz. As perdas de conversão põem o AC abaixo do DC, o que **identifica** qual é qual — a escolha de qual deles o relatório chama produção continua a ser comercial |
| `GRID_SIGN_CONVENTION` | **o balanço energético.** Com a bateria parada, `load = pv + grid` se positivo for importar e `load = pv − grid` se for exportar. Só uma hipótese fecha. A contagem nocturna fica como recurso |

**Resultado real no piloto, 2026-08-27**, dez minutos depois de ligar e em pleno
dia: `positive_import`, com `load = pv + grid` a fechar em **0,000000 kW em 10
amostras de 10** e a hipótese oposta a falhar em todas. A heurística nocturna
que este documento descrevia antes era restritiva de mais — o balanço é uma
identidade, não uma estatística, e distingue as hipóteses a qualquer hora.

Uma amostra real (08:03): pv 7,007 / carga 10,805 / rede +3,798 / bateria 0 →
10,805 − 7,007 = 3,798, exacto.

O veredicto continua a recusar-se quando os dados são finos ou contraditórios:
"inconclusivo" é uma resposta normal e quer dizer continuar a recolher.

**Nota sobre o `PRODUCTION_SIGNAL` nesta central:** 37498 e 37516 devolvem o
valor **idêntico** em todas as amostras, porque não há bateria. A escolha é
indiferente aqui e não o será numa central com armazenamento — o script diz
`registers_are_identical` em vez de inventar uma perda de conversão.

Mesmo com veredicto, **confirmar contra o contador real antes de fixar a
variável**. A leitura é consistente com a física; o contador é a fonte de verdade.

## 12. O que falta para isto funcionar a sério

Nada disto é código. É a lista do que só existe fora deste repositório.

**Bloqueante, no lado da instalação:**

1. **Configurar cada SDongle para ligar a este servidor.** Na app FusionSolar /
   SUN2000, na configuração do sistema de gestão, apontar ao endereço público e à
   porta do NAT. Sem isto não aparece ligação nenhuma — a V2 não tem como ir
   buscar o dongle, é o dongle que tem de vir.
2. **Regra de NAT no router do cliente** para `192.168.1.237:1502`, e a firewall
   do servidor a deixar passar.
3. **`NEMSEI_V2_SCADA_BIND_ADDRESS`** com a interface certa. Por omissão é
   `127.0.0.1`, o que não recebe nada de fora — de propósito.

**Bloqueante, no lado dos dados:**

4. **O número de série de cada dongle** e a que central pertence. Se não se
   souber de antemão, deixar ligar uma vez e ler de `status`.
5. **Fuso horário em cada asset.** Sem ele não há dia local nem política de
   origem, e o rollup salta a central em voz alta.
6. **`PRODUCTION_SIGNAL` e `POWER_UNIT`**, com a evidência da §11.
7. **`GRID_SIGN_CONVENTION`**, se se quiser exportação e importação. Sem ela o
   resto funciona — só essas duas métricas é que não são escritas.

**Do lado do servidor, por fazer:**

8. **Aplicar a migração `0026` à base viva**, com backup verificado antes
   (`POSTGRESQL_RUNBOOK.md`).
9. **Arrancar o serviço** com o profile `huawei-scada`.
10. **Ligar o rollup** depois de o passo 6 estar resolvido, não antes: um rollup
    com o sinal de produção errado escreve factos plausíveis e errados, e
    corrigi-los depois é uma revisão por dia e por métrica.

**Ainda em aberto, e não é pouco:** a §7 (TLS). Enquanto a porta estiver em claro,
isto é um piloto numa rede controlada.

## 13. Mapa de testes

| Ficheiro | Cobre |
| --- | --- |
| `test_huawei_scada_protocol.py` | ADV (real, parcial, sem série, unidade anunciada), frames fragmentados e agrupados, MBAP inválido, `0x83`/`0x04`, U32/I32, escalas, sinais, sentinelas, só-leitura |
| `test_huawei_scada_session.py` | handshake (incluindo byte a byte), polling, fallback de leitura larga, timeout, id de transacção, resposta obsoleta, sonda do `unit=1`, keep-alive, reconnect |
| `test_huawei_scada_ingestion.py` | identidade, quarentena, rejeição persistente, fingerprint, amostras, dedupe e revisões, falha de leitura sem amostra, observação canónica, noite ≠ offline, política de origem, `SyncRun` e saúde |
| `test_huawei_scada_rollup.py` | integração, lacunas, registos ausentes, anomalias de relógio, recorte com sinal, contrato que recusa, dia local, completude, supersessão, política de origem, retenção |
| `test_huawei_scada_listener.py` | ponta-a-ponta com logger falso, quarentena, timeout, erros repetidos, limite de sessões, encerramento, **lock exclusivo**, validação de configuração, contrato de deployment (binding único, processo próprio, sem IP fixo, sem túnel) |
| `test_huawei_scada_contract_evidence.py` | veredictos do sinal da rede, da escala e do registo de produção — e, sobretudo, todas as maneiras de eles se recusarem: poucos dados, evidência contraditória, bateria a mexer, sem capacidade instalada declarada |
| `test_huawei_scada_reporting.py` | dataset e assembler com energia estimada, digest inalterado sem estimativas, sem chamadas de provider na geração, despacho dos jobs, agendamento e dedupe |
| `test_migrations.py` | `upgrade`/`downgrade` da `0026` no grafo completo |
| `test_architecture_boundaries.py` | só o `listener.py` toca em socket; sem escrita Modbus; adapter sem dependências de web/domínio |
