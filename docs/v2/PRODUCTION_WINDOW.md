# Janela produtiva e coordenadas das instalações

Produção nula às três da manhã não é uma avaria. Toda a regra que vigia
ausência de produção tem de fazer essa pergunta primeiro, e até este bloco a
V2 não tinha forma nenhuma de a responder: nada no código calculava o nascer
do sol, e `assets.latitude`/`longitude` — colunas que existem desde a `0001` —
estavam a NULL nas 267 instalações.

## A resposta tem três valores, nunca dois

`monitoring/production_window.py`:

| estado | significado |
| --- | --- |
| `productive` | o sol está alto aqui; nada gerado merece explicação |
| `dark` | o sol está em baixo; nada gerado é o esperado |
| `unknown` | ninguém registou onde fica esta instalação |

`unknown` **não** é um `dark` disfarçado. Uma instalação que não se consegue
localizar tem de aparecer como uma instalação que não se consegue avaliar, e
não desaparecer silenciosamente da monitorização. É a mesma distinção que
`installation_state.py` já faz entre `no_evidence` e `unknown`, e a mesma que
`DATA_RULES.md` exige entre "0 kWh produzidos" e "produção indisponível".

## Porquê aritmética e não uma dependência

O algoritmo NOAA de posição solar são quarenta linhas de contas. Não precisa
de rede, nem de ficheiro de dados, nem de pacote, e acerta a menos de um
minuto nestas latitudes. Acrescentar uma dependência para isto falharia o
`GOAL.md` §23 — "every dependency needs a reason".

Verificado contra valores publicados:

| local | dia | calculado (UTC) | publicado (UTC) |
| --- | --- | --- | --- |
| Lisboa | 2026-06-21 | 05:12:02 / 20:04:43 | 05:12 / 20:04 |
| Lisboa | 2026-12-21 | 07:51:02 / 17:18:13 | 07:51 / 17:18 |

## A margem

A janela produtiva não começa ao nascer do sol. Junto ao horizonte a
irradiância é quase nula, os inversores ainda estão a acordar e qualquer coisa
faz sombra. Uma central que não produz quatro minutos depois do nascer do sol
é normal; quatro horas depois não é.

`DEFAULT_MARGIN` são 45 minutos para cada lado. É um juízo, não uma medição —
a V2 não guarda irradiância de onde o derivar — e por isso é um parâmetro e
não um literal enterrado dentro de uma regra.

## As coordenadas

A V1 tem 119 pares e, o que importa tanto quanto, de onde vieram:

| origem | confiança | nº |
| --- | --- | --- |
| `google_mymaps` | `ok` | 87 |
| `openrouteservice` | `suspect` | 24 |
| `manual` | `manual` | 8 |

`suspect` é a palavra da própria V1 para "geocodificado de uma morada, ninguém
verificou". Uma morada geocodificada pode cair no meio do concelho; um telhado
traçado num mapa não. Importar as 119 como se fossem igualmente boas seria a
precisão inventada que este esquema recusa em todo o lado, e por isso a
migração `0031` traz a proveniência junto com o par, com um CHECK que recusa
uma coordenada sem origem registada.

Para efeitos de nascer/pôr do sol a diferença é irrelevante — um erro de
quilómetros vale segundos de sol, contra uma margem de 45 minutos. A marca
`suspect` existe para quem um dia precise da coordenada para outra coisa
(deslocações, distâncias, mapas), não para desconfiar da janela produtiva.

## O importador é separado, de propósito

Não é uma coluna acrescentada ao `SELECT` do `v1_import.py`. Esse importador
faz o *fingerprint* de cada linha de origem com `row_hash(row, row.keys())`
para distinguir `reused` de `changed_source`; alargar-lhe a consulta mudaria
os 267 *fingerprints* de uma vez e daria o parque inteiro como alterado na
origem — destruindo exatamente o sinal que o mecanismo existe para dar.

`assets/coordinates_import.py` é estreito e idempotente:

- liga-se pelo `legacy_import_records.target_asset_id`, nunca por nome;
- nunca sobrepõe um valor já presente — a única forma de um asset V2 ter uma
  coordenada hoje é alguém a ter escrito, e um import em massa não manda mais
  do que uma pessoa;
- recusa `(0, 0)` (o que um geocoder devolve quando falha, no Golfo da Guiné),
  coordenadas fora do globo, e qualquer par sem origem conhecida — cada recusa
  fica no manifesto com o motivo.

```text
python -m nemsei.assets.coordinates_import --v1-db <frozen-v1.sqlite> [--dry-run]
```

## Análise contra as bases reais, 2026-09-01

Feita só com leituras, antes de qualquer escrita:

| | |
| --- | --- |
| pares de coordenadas na V1 | 119 |
| desses, ligados a uma instalação V2 | **119** (nenhum órfão) |
| em `(0, 0)` ou fora do globo | 0 |
| instalações V2 que ficariam sem coordenadas | **148** |

As 148 continuam a responder `unknown`, visivelmente. É a dimensão honesta da
lacuna que resta: centrais que ninguém localizou nem na V1.

## O que isto ainda não faz

Nada consome a janela produtiva ainda. As regras de incidentes por duração
(Bloco 3) são o primeiro leitor. Até lá isto é capacidade instalada e provada,
não comportamento em produção.
