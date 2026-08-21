# Diagnostics UI, Portfolio Diagnostics, Telegram: plano e progresso

Auditoria feita contra o V2 real e o V1 real (código, não memória), 2026-08-21.
O plano abaixo (secções 1-18) foi escrito **antes** de qualquer implementação,
como pedido. **D1 foi aprovado e está implementado, testado e LIVE VERIFIED**
contra a base de dados de produção real — ver §19 no fim. D2-D6 continuam por
implementar, sem aprovação ainda.

## 1. Estado actual real

**V2 `/diagnostics`** (`web/diagnostics_routes.py`, `web/diagnostics_queries.py`):
- Índice (`/diagnostics`): pesquisa por nome, lista `asset.canonical_name` +
  `device_count` + link. Sem estado, sem contagem de problemas, sem ordenação
  por gravidade — puramente uma lista de pesquisa.
- Detalhe (`/diagnostics/assets/<id>`): tabela de dispositivos worst-first
  (`_SEVERITY` por `availability_status`) + painel de findings (Fatia 4,
  `diagnostics/findings.py`) também worst-first. KPIs simples (total,
  disponíveis, precisam de atenção, sem leitura).
- `diagnostics/findings.py`: 8 regras determinísticas
  (`device_no_history`, `device_unavailable`, `device_unknown_status`,
  `stale_reading`, `zero_power_while_peers_active`,
  `power_disparity_among_peers`, `daily_energy_disparity_among_peers`,
  `partial_device_coverage`), **recomputadas em cada pedido**, nunca
  persistidas. Cada `DiagnosticFinding` já tem `rule_code`, `severity`,
  `evidence`, `observed_at`, `active_since` (via histórico real quando
  disponível), `missing_data`. `still_active` é sempre `True` por desenho —
  não há "resolvido" porque não há nada persistido a resolver.

**V2 Portfolios** (`portfolios/models.py`, `web/portfolio_queries.py`,
`web/portfolio_routes.py`):
- Domínio maduro: `Portfolio` (flat, sem parent), `PortfolioMembership`
  (temporal, `resolved`/`unresolved`/`placeholder`), `PortfolioRule` (filtros,
  nunca sub-portfolios), `PortfolioSnapshot` (composição congelada por
  período), `PortfolioDataset`/`PortfolioDatasetMember` (agregado **mensal**,
  construído explicitamente por `POST .../build`, cada membro aponta para o
  seu próprio `ReportingDataset` — nunca uma segunda lógica de cálculo),
  `PortfolioReportRun` (workflow `generated → reviewed → approved`,
  bloqueado por trigger de BD após aprovação).
- UI já em secções (`SECTIONS` em `portfolio_queries.py`):
  `overview, installations, production, availability, financial, reports,
  settings` — routing `/portfolios/<id>/<section>` já existe e é o padrão
  certo para adicionar uma secção nova.
- **Já existe um conceito chamado "attention"** (`filters["attention"]`,
  `assets_needing_attention()` em `portfolios/datasets.py`) — mas significa
  **cobertura de reporting** (`production_state != "measured"` dentro do
  `PortfolioDataset` mensal), não saúde de equipamento. Isto é uma colisão de
  nome a evitar deliberadamente (secção 2).

**V2 Telegram/notificações**: **não existe nada.** Zero referências a
`telegram` em todo o `src/nemsei/`. Ponto de partida limpo, sem dívida a
migrar.

## 2. Problemas da UI actual

- **Índice `/diagnostics` não responde "que instalações precisam de
  atenção"** — é só uma lista alfabética/pesquisável. Um operador tem de
  abrir cada instalação uma a uma para saber se há problema.
- **Sem contagem de findings por severidade no índice** — a única forma de
  saber que uma instalação tem 1 finding crítico é abrir o detalhe.
- **Colisão semântica "attention"**: Portfolios já usa "attention" para
  cobertura de reporting mensal. Uma segunda "atenção" para saúde de
  equipamento, se usar a mesma palavra sem distinção visual/textual clara,
  vai confundir — quem vê "3 instalações precisam de atenção" no Portfolio
  não sabe se é dados em falta no relatório ou um inversor avariado.
- **Painel de findings é uma lista plana** — não distingue visualmente
  "isto está a acontecer agora" de "isto foi observado uma vez". Sem
  indicação de impacto operacional (ex: "está a perder ~X kWh/h estimados").
- **Comparação entre inversores é só a tabela** — não há nada que salte à
  vista quando um inversor está muito abaixo dos pares (a regra já existe em
  `findings.py`, mas a UI não a destaca visualmente além do texto do
  finding).
- **Sem histórico recente na página** — não dá para ver "este dispositivo
  esteve indisponível 3 vezes esta semana" sem ir à BD directamente.
- **Freshness só aparece na tabela de dispositivos**, não é resumida a nível
  de instalação ("dados com X min/h de idade").

## 3. Comportamento Telegram da V1 a preservar (requisitos, não arquitectura)

Auditado directamente em `monitoring_board/app_factory.py` (linhas
~7650-8300) e `services/telegram_service.py`. **V1 é fonte de requisitos e
dados operacionais reais, não de arquitectura** — o que se segue é o que a
V1 realmente faz, não uma proposta de como o V2 deve ser construído.

### Tabelas reais (SQLite, confirmadas no schema)

- `telegram_alerts(id, asset_id, alert_type, alert_key, message, sent_at,
  status, error_message, blocked_reason)` — histórico completo, incluindo
  os **bloqueados** (não só os enviados), com a razão do bloqueio.
  Índice `(alert_key, status)`.
- `alert_settings(key, value)` — configuração key/value, editável na UI.
- `alert_blacklist(id, asset_id, asset_name, reason, active)` — opt-out
  permanente por instalação.
- `alert_baseline(id, baseline_at, created_by, notes)` — quando o corte de
  "não alertar sobre o que já estava partido" foi definido.
- `tickets`/`ticket_visits` — sistema de tickets O&M separado, relacionado
  mas não é notificações.

### Tipos de alerta reais, com identidade e cooldown próprios

| `alert_type` | Disparado por | Identidade (`alert_key`) | Cooldown/repetição |
| --- | --- | --- | --- |
| `novo_erro` | mudança de estado → Erro | `asset:tipo:batch:{batch_id}:to:{status}` | `NEW_ERROR_COOLDOWN_MINUTES` (default 0) |
| `nova_desconexao` | mudança de estado → Desconectada | idem | `OFFLINE_COOLDOWN_MINUTES` (default 120) |
| `resolvido` | mudança de estado → Operacional/Resolvido | idem | `RESOLVED_COOLDOWN_MINUTES` (default 0) |
| `erro_persistente_24h` | scan periódico, problema activo ≥24h | `asset:tipo:{problem_start}` | **nunca repete** — `problem_start` não muda enquanto o problema continua, e `alert_already_sent` bloqueia permanentemente a mesma key. Um único aviso de escalada por episódio, não um nag repetido. |
| `desconexao_persistente_2h` | idem, ≥2h, só em horário diurno por defeito | idem | idem — uma vez por episódio |
| `recorrente_7d` | ≥3 ocorrências nos últimos 7 dias | `asset:tipo:{data_de_hoje}` | pode repetir **uma vez por dia** enquanto a condição de flapping se mantiver (a key muda todos os dias) |
| `daily_summary` | agendado, 1×/dia | `daily_summary:{data}` | uma vez por dia |
| `geral_multiplas_desconexoes` | >5 desconexões no mesmo batch (e total ≤10) | `geral_desconexoes_batch_{batch_id}` | agrega em vez de enviar N mensagens |
| `batch_many_alerts` | >10 alertas prontos no mesmo batch | `batch_many_alerts:{batch_id}` | **circuit breaker**: envia 1 meta-alerta, bloqueia o resto |

**Isto responde directamente ao exemplo do pedido** ("10:32 offline → 11:02
nada → 14:32 continua há 4h, talvez escalar → 16:10 recuperado"): a V1 já
resolve isto exactamente assim — uma alerta na transição, zero repetições
até um único ponto de escalada por episódio (chave ancorada em
`problem_start`, não em tempo corrido), e uma alerta de recuperação com
duração calculada (`find_problem_start` até `happened_at`).

### Camadas de decisão (`alert_decision`, todas verificadas antes de enviar)

1. `TELEGRAM_ALERTS_ENABLED` global + variável de ambiente.
2. Telegram configurado (token + chat id).
3. `monitoring_enabled`/`alerts_enabled` por instalação.
4. Blacklist (`alert_blacklist`, por id ou nome).
5. `monitoring_status` da instalação — `maintenance`/`out_of_scope`/`disabled`
   bloqueiam sempre; `silenced` bloqueia até `silenced_until` (snooze
   temporário, distinto da blacklist permanente).
6. `ALERT_SCOPE` (`only_o&m` por defeito — **não alerta instalações sem
   contrato O&M activo**, `only_active_contracts`, `only_selected_assets`,
   `all_assets`).
7. Toggle por tipo de alerta (`SEND_NEW_ERROR_ALERTS` etc.).
8. `alert_already_sent(key)` — dedup permanente pela chave exacta.
9. Cooldown por tipo (fallback quando a key ainda não foi usada mas o
   assunto é o mesmo).

### Outras propriedades reais a preservar

- **Baseline de corte** (`ALERT_BASELINE_AT`, `IGNORE_HISTORICAL_ALERTS`
  default `true`): ao ligar o sistema, problemas já abertos antes da data de
  corte não disparam o alerta persistente — evita uma tempestade de alertas
  no arranque.
- **Resumo diário** agrega: erro/desconectadas actuais, novos desde ontem,
  persistentes, resolvidos desde ontem, recorrentes, e instalações
  silenciadas/em manutenção — um precedente real para um digest a nível de
  portfolio.
- **Contexto de alarme FusionSolar é lido ao vivo e embutido na mensagem**
  (`primary_alarm_name`, `primary_alarm_severity`, `alarm_summary`) sem
  nunca ser persistido — consistente com `ALARMS_AUDIT.md`: é usado, nunca
  guardado.
- Mensagens em HTML (`parse_mode=HTML`), emoji por severidade (🚨/⚠️/🟢/🔁),
  `html.escape` em todo o texto interpolado.

### O que a V1 tem que **não deve ser copiado cegamente**

- **`MINIMUM_ALERT_SEVERITY` é configuração morta** — está na UI de
  definições e no schema, mas nunca é lida em `alert_decision` nem em
  nenhum outro sítio. Dá uma falsa sensação de controlo. Não portar como
  está; se o V2 quiser um limiar de severidade, tem de ser real.
- **`count_problem_occurrences_since` conta linhas brutas de
  `monitoring_records`**, não episódios distintos. Isto funciona em V1
  porque a cadência é de 1-6 leituras/dia (`DIAGNOSTICS.md`). À cadência do
  V2 (30 min = ~30-48 leituras/dia quando o polling estiver ligado), a
  mesma lógica dispararia "recorrente" a partir de um único dia mau, não de
  recorrência real ao longo de vários dias. **Tem de ser recontada por
  episódios/dias distintos**, não por linhas.
- **Um único bot/chat global** — sem encaminhamento por portfolio/canal.
  Adequado à escala da V1; o V2 com Portfolios reais provavelmente quer
  pelo menos "chat por portfolio" como opção.
- **Agregação por contagem, não por severidade** — a V1 colapsa por
  volume (>5, >10), nunca deixa um crítico "furar" a agregação. O pedido
  actual quer isso como propriedade nova, não presente na V1.

## 4. IMPORTANTE — Telegram não depende de `provider_alarm_facts`

Confirmado contra `ALARMS_AUDIT.md` (auditoria anterior, ainda válida sem
alterações): FusionSolar não tem identidade de dispositivo estável nem
vocabulário de lifecycle comprovado, e nunca foi persistido um único alarme
em produção. **Nenhuma parte deste plano depende de alarmes de provider.**
Toda a fonte de evidência para incidentes/notificações é `device_status_facts`
→ `diagnostics/findings.py`, já construído e testado.

## 5. Arquitectura recomendada

```
device_status_facts (raw, append-only, já existe)
        │
        ▼
diagnostics/findings.py  (interpretação — recomputada, efémera, já existe)
        │  (o mesmo motor de regras, reutilizado, não duplicado)
        ▼
DiagnosticIncident        (incidente — NOVO, persistido, com lifecycle)
        │
        ▼
NotificationPolicy        (decide SE um incidente merece notificar — NOVO)
        │
        ▼
NotificationEvent         (registo do que foi enviado/bloqueado — NOVO)
        │
        ▼
Telegram (entrega — nunca decide)
```

Quatro camadas distintas, cada uma com uma responsabilidade só:
**factos ≠ interpretação ≠ incidente ≠ notificação** — exactamente como
pedido. O canal Telegram não lê `device_status_facts` nem corre regras; só
recebe uma mensagem já decidida.

### Porque um `DiagnosticIncident` novo, e não mexer em `findings.py`

`findings.py` foi desenhado deliberadamente para nunca acumular (recomputado
a cada pedido, sem persistência) — essa propriedade continua certa para a
UI de diagnóstico, que só precisa de "o que é verdade agora". Notificações
precisam de uma coisa que `findings.py` deliberadamente não tem: **memória
entre avaliações** — para saber que "isto já foi enviado", "está aberto há
quanto tempo", "já recuperou". Isso é um problema diferente, não uma
correcção ao que já existe.

## 6. Comparação de modelos de dados (A/B/C)

**A) `DiagnosticFinding` continua derivado (inalterado) + `Incident`
persistido**, actualizado por um avaliador periódico que chama
`evaluate_asset_findings()` e faz diff contra os incidentes actualmente
abertos (por `rule_code`+`device_id`/`asset_id`).

- ✅ Zero alterações ao módulo já enviado e testado (20 testes, já em
  produção da UI).
- ✅ Uma só implementação das regras — o avaliador de incidentes chama a
  mesma função que a UI usa, nunca duplica a lógica.
- ✅ Mapeia directamente para a separação pedida
  (facto ≠ interpretação ≠ incidente ≠ notificação, quatro camadas reais).
- ⚠️ Precisa de um processo periódico (job) a correr a avaliação e a
  comparar com o estado anterior — trabalho novo, mas do mesmo tipo do que
  já existe (`Job`/`Scheduler`, reutilizado, não uma infraestrutura nova).

**B) `DiagnosticFinding` passa a ser persistido com lifecycle** (a tabela
substitui o cálculo em memória; cada finding é escrito/actualizado a cada
avaliação).

- ❌ Muda o desenho já entregue de propósito simples para um com estado,
  sem que a UI precise disso — o `/diagnostics/assets/<id>` continua só a
  precisar do "agora".
- ❌ Confunde a camada de interpretação com a de incidente — um finding
  passaria a ter conceptualmente duas responsabilidades (mostrar o estado
  actual E manter identidade ao longo do tempo).
- ❌ Migração/risco maior num módulo que já está em produção e testado.

**C) factos → `DiagnosticIncident` directamente**, sem usar
`evaluate_asset_findings()` como camada intermédia formal.

- ❌ Ou duplica a lógica de regras (uma versão para a UI via `findings.py`,
  outra dentro do avaliador de incidentes) ou, se reutilizar
  `evaluate_asset_findings()` de qualquer forma, colapsa exactamente na
  Opção A com um nome diferente.

**Recomendação: A.** Menor risco, zero duplicação de regras, encaixa
exactamente na separação de camadas pedida, e o único componente novo
(`DiagnosticIncident` + o avaliador que o mantém) é aditivo — não toca no
que já está em produção.

## 7. Modelo de dados necessário (proposto, não migrations reais ainda)

```
DiagnosticIncident
  id
  rule_code                 -- mesmo vocabulário de findings.py
  asset_id
  device_id (nullable)      -- nullable para findings ao nível do asset (ex: partial_device_coverage)
  severity                  -- do finding no momento da abertura (pode ser recalculado)
  status                    -- open | acknowledged | resolved
  opened_at                 -- primeira vez que o finding foi observado
  last_observed_at          -- última vez que o finding continuou verdadeiro
  resolved_at               -- nullable; quando o finding deixou de ser verdadeiro
  acknowledged_at, acknowledged_by  -- nullable
  occurrence_count           -- quantas avaliações consecutivas confirmaram o finding
  evidence_json              -- snapshot do evidence do finding na abertura (+ na última observação)
  escalated_at                -- nullable; quando (se) uma escalada foi enviada

NotificationChannel
  id, kind ('telegram'), scope ('global' | 'portfolio'), portfolio_id (nullable),
  bot_token_ref, chat_id, enabled

NotificationPolicy
  id, channel_id, rule_code (nullable = aplica a todas),
  min_severity, cooldown_minutes, escalation_after_minutes (nullable),
  daytime_only (bool), quiet_hours (nullable), aggregate_threshold (nullable),
  bypass_aggregation_severity (nullable) -- NOVO vs. V1: crítico fura agregação

NotificationEvent
  id, incident_id (nullable -- daily digest não tem um incidente único),
  channel_id, event_kind ('opened'|'escalated'|'resolved'|'digest'|'aggregated'),
  status ('sent'|'blocked'|'failed'), reason (nullable), message,
  sent_at
```

Identidade estável de incidente = `(rule_code, asset_id, device_id)` —
mesma chave que já ordena os findings hoje, sem inventar uma nova noção de
identidade.

## 8. Lifecycle de incidentes

```
                 finding continua a aparecer
                 ┌────────────────────────┐
                 │                        │
   finding novo  ▼                        │
──────────────►  open ──── acknowledged ──┤
   (opened_at,                (opcional,   │
   notif. "opened")            não bloqueia │
                                a evolução)  │
                                             │
                 finding deixa de aparecer   │
                 ▼                           │
              resolved ◄─────────────────────┘
           (resolved_at, duração,
            notif. "resolved")
```

- **Abertura**: primeira avaliação em que `evaluate_asset_findings()`
  devolve um finding com aquela `(rule_code, asset_id, device_id)` e não
  existe incidente `open`/`acknowledged` com a mesma chave.
- **Continuação**: cada avaliação seguinte que ainda devolve o mesmo finding
  actualiza `last_observed_at`/`occurrence_count`/`evidence_json`, **não**
  abre um segundo incidente.
- **Escalada**: opcional, configurável por `NotificationPolicy`
  (`escalation_after_minutes`), no máximo uma vez por incidente
  (`escalated_at` não nulo bloqueia uma segunda escalada) — mesma
  propriedade que a V1 já tinha (uma escalada por episódio).
- **Resolução**: a próxima avaliação em que o finding **não** aparece mais
  para aquela chave fecha o incidente (`resolved_at = agora`), mesmo que o
  `still_active` do finding seja sempre `True` enquanto existe — a
  resolução é inferida pela ausência, não por um campo do finding.
- **Acknowledgement**: opcional, só regista quem viu — não muda cooldown
  nem impede escalada/resolução automática. Mantido simples de propósito,
  como o pedido pede.

## 9. Política anti-spam

| Evento | Regra por defeito | Configurável |
| --- | --- | --- |
| Abertura de incidente | notifica uma vez, sujeito a cooldown/quiet-hours | `cooldown_minutes` por regra |
| Incidente continua aberto | **não notifica de novo** até à escalada | — |
| Escalada | no máximo uma por incidente, ao fim de `escalation_after_minutes` | por regra/severidade, pode ser `None` = sem escalada |
| Resolução | notifica sempre, com duração calculada (`resolved_at - opened_at`) | `RESOLVED_COOLDOWN` equivalente, default 0 |
| Recorrência (mesmo `rule_code`+`asset_id` reabre N vezes numa janela) | digest diário, não mensagem imediata | limiar configurável, default 3 em 7 dias — **por episódios/dias distintos, não linhas brutas** (correcção à V1, secção 3) |
| Muitas aberturas na mesma avaliação | agrega numa mensagem, **excepto severidade crítica** que fura sempre | limiar de contagem + `bypass_aggregation_severity` |
| Corte inicial | incidentes já abertos antes de `NotificationPolicy` ficar activa não notificam | equivalente a `ALERT_BASELINE_AT` |
| Silenciar | por asset/portfolio, com validade opcional | equivalente a `silenced_until` da V1 |

## 10. Integração Portfolio

`PortfolioDiagnosticSummary` **não é um `PortfolioDataset`** — não é mensal,
não é aprovado, não congela. É uma vista **ao vivo**, recomputada como
`findings.py` já é, sobre os membros **actualmente activos** do portfolio
(mesma resolução de membership que `installations` já usa):

```
Portfolio
  → membros activos (PortfolioMembership.resolved, valid_to IS NULL)
    → para cada asset: current_device_status() (já existe)
      → evaluate_asset_findings() (já existe, mesma função, zero duplicação)
        → agregação: contagem por severidade, ranking de instalações
          piores primeiro, cobertura (quantos assets têm findings vs.
          quantos não têm nenhuma leitura)
```

**Um problema = um finding = no máximo um incidente.** O portfolio nunca
avalia regras outra vez — só conta e agrupa os findings/incidentes que já
existem ao nível do asset. Isto responde directamente à preocupação do
pedido: "1 problema num inversor ≠ finding do device + finding do asset +
finding do portfolio" — não há um terceiro finding a nível de portfolio,
há uma contagem do que já existe.

**Nova secção na UI de Portfolio** (`SECTIONS` já suporta isto de graça):
`("diagnostics", "Diagnóstico")`, entre `installations` e `production` —
exactamente a ordem que o pedido sugeriu. **Nome distinto de "attention"**
(ex.: "problemas" ou "saúde operacional") para não colidir com o "attention"
já existente de cobertura de reporting (secção 2).

Disponibilidade a nível de portfolio fica **fora desta fase** — depende do
canário ainda bloqueado (`DEVICE_TELEMETRY.md` §10); o resumo de portfolio
mostra "disponibilidade: não validada" em vez de inventar um número.

## 11. Proposta de UI (navegação)

```
Diagnostics
├── Overview          -- todas as instalações, worst-first, contagem de findings por severidade
├── Incidents          -- lista de DiagnosticIncident, abertos primeiro, com duração
├── Installations       -- o índice actual, mas com badge de severidade por linha
└── Notification history -- NotificationEvent, incluindo bloqueados e porquê

Portfolio
├── Overview
├── Installations
├── Diagnostics    ← NOVO: PortfolioDiagnosticSummary
├── Production
├── Availability
├── Financial
└── Reports
(Settings já existe, fica onde está)

Asset (dentro de /diagnostics/assets/<id>, mantém-se)
├── Overview (estado geral, última actualização, produção actual)
├── Devices (tabela actual, comparação entre inversores)
├── Diagnostics (findings, já existe, melhora visualmente)
└── (histórico recente — não uma secção nova, uma zona da página de Diagnostics)
```

Sem "Reports" novo dentro de Asset — já existe fora deste fluxo. Sem uma
secção de configuração de notificações dentro de Diagnostics — isso fica em
`Portfolio → Settings` (canal por portfolio) e uma página de administração
global para o canal `global`, reutilizando o padrão de configuração que já
existe para outras capabilities.

## 12. Migrations previstas (não escritas nesta fase)

1. `diagnostic_incidents` — tabela nova, índice em `(rule_code, asset_id,
   device_id, status)` e em `(status, last_observed_at)`.
2. `notification_channels` — tabela nova.
3. `notification_policies` — tabela nova, FK para `notification_channels`.
4. `notification_events` — tabela nova, FK opcional para
   `diagnostic_incidents`, índice em `(sent_at)` e `(incident_id)`.

Nenhuma migration mexe em `device_status_facts` ou em qualquer tabela já
viva — tudo aditivo.

## 13. Endpoints/routes/services previstos (não implementados)

- `diagnostics/incidents.py` — avaliador periódico (job): chama
  `evaluate_asset_findings()` por asset com facto recente, abre/actualiza/
  fecha `DiagnosticIncident`.
- `diagnostics/notifications.py` — decide, por incidente e por
  `NotificationPolicy`, se e o quê notificar (cooldown, agregação,
  escalada) — devolve uma decisão, não envia nada.
- `integrations/telegram/client.py` — só entrega (`sendMessage`), sem
  lógica de decisão, análoga a `telegram_service.py` da V1 mas sem as
  responsabilidades de decisão que a V1 misturava ali.
- Rotas web: `/diagnostics/incidents`, `/diagnostics/notifications`,
  `/portfolios/<id>/diagnostics`, mais um botão de acknowledge por
  incidente.
- Um novo job type (`diagnostic.evaluate` ou similar) no `Job`/`Scheduler`
  já existente — mesmo padrão do `device_status.poll` da Fatia 3, sem
  infraestrutura nova.

## 14. Testes necessários

- Unitários puros para lifecycle de incidente (abre, continua, escala uma
  vez só, resolve, reabre como novo incidente depois de resolvido).
- Testes de dedup/cooldown para cada camada da política anti-spam
  (equivalentes aos que a V1 tem implicitamente, mas explícitos e
  automatizados).
- Teste de agregação: N aberturas na mesma avaliação agregam; um crítico
  fura a agregação.
- Teste de baseline: incidentes já abertos antes da política ficar activa
  não notificam.
- Teste de idempotência do avaliador periódico (correr duas vezes seguidas
  não duplica incidentes nem eventos).
- Teste de portfolio: a soma dos findings por asset bate certo com o
  resumo do portfolio, sem um terceiro finding a aparecer do nada.
- Testes de integração web para as páginas novas.
- Nenhum teste depende de uma chamada Telegram real — o cliente é mockável,
  como a V1 já demonstra ser possível.

## 15. Riscos

- **Cadência de avaliação do avaliador de incidentes** precisa de ser
  pensada contra a cadência real de `device_status_facts` (hoje esparsa
  para a maioria dos assets, densa só para o canário) — um avaliador a
  correr de 5 em 5 minutos sobre dados que só mudam 1×/dia não ganha nada
  e gasta ciclos à toa.
- **Volume de Telegram se o canário escalar** — a política de agregação
  tem de estar madura antes de ligar notificações para mais do que o
  canário, ou o mesmo "alert storm" que a V1 já resolveu (circuit breaker)
  reaparece no V2 sem essa protecção construída ainda.
- **Confundir "attention" de reporting com "attention" de diagnóstico** —
  já nomeado na secção 2; risco real de UX, não hipotético.
- **`count_problem_occurrences_since` copiado literalmente** seria um bug
  reintroduzido à cadência do V2 — já identificado, não copiar sem
  recontagem por episódios.
- **Secrets do bot Telegram** — token/chat id têm de seguir o mesmo padrão
  de secrets já usado para credenciais FusionSolar (nunca em logs, nunca
  commitado), e o cliente de entrega precisa do mesmo tipo de kill switch
  (`enabled=false` estrutural) que `device_status_poll` já tem.

## 16. Dependências/bloqueios

- **Nenhuma parte deste plano depende do canário de disponibilidade
  bloqueado** (`DEVICE_TELEMETRY.md` §10) nem de `provider_alarm_facts`
  (`ALARMS_AUDIT.md`) — ambos ficam fora do caminho crítico, por desenho.
- Depende de `diagnostics/findings.py` (já existe) e do padrão
  `Job`/`Scheduler` (já existe) — nada de infraestrutura nova por baixo.
- Depende de uma decisão humana sobre credenciais Telegram reais (bot
  token, chat id) antes de qualquer fatia poder enviar uma mensagem real —
  até lá, tudo é testável com um cliente mock.

## 17. Alarmes de provider — mantido como gate (sem alterações)

Para FusionSolar, o que falta verificar antes de sequer propor um canário:
identidade estável de dispositivo no payload de alarme (hoje só por nome),
e um valor real de `status`/`alarmStatus`/`state` alguma vez observado e
guardado (hoje: zero linhas, nunca). Um canário mínimo futuro, se algum dia
fizer sentido, seria: uma chamada isolada a `alarms()` contra a conta
canário já validada, guardando o payload bruto para inspecção — nada mais,
sem desenhar schema antes disso. Sigenergy continua **BLOCKED**, sem
alteração face à auditoria anterior. Nenhuma fatia abaixo depende disto.

## 18. Ordem de implementação em fatias pequenas

| Fatia | Objectivo | Valor operacional | Risco | Dependências |
| --- | --- | --- | --- | --- |
| **D1** | `DiagnosticIncident` + avaliador periódico (sem Telegram, sem UI nova) | Histórico de incidentes fica visível na BD; base para tudo o resto | Baixo — aditivo, reutiliza `findings.py` | `Job`/`Scheduler` existente |
| **D2** | UI: Overview de Diagnostics com contagem por severidade + página de Incidents | Responde "que instalações precisam de atenção" sem abrir cada uma | Baixo — só leitura | D1 |
| **D3** | `NotificationChannel`/`NotificationPolicy`/`NotificationEvent` + cliente Telegram (mock-first, sem enviar de propósito até aprovado) | Infra de notificação pronta e testável sem risco de spam real | Baixo-médio — sem envio real ainda | D1 |
| **D4** | Política anti-spam completa (cooldown, escalada única, agregação, bypass crítico, baseline) + activação real do canal Telegram para o canário apenas | Primeiro alerta real, escopado ao mesmo canário já validado | Médio — primeira mensagem real | D3 |
| **D5** | Portfolio Diagnostics (secção nova, agregação de findings/incidentes por portfolio) | Visão de carteira sem abrir instalação a instalação | Baixo — só agregação, sem lógica nova | D1, D2 |
| **D6** | Digest diário + agregação entre instalações (equivalente ao resumo da V1) | Reduz ruído, dá visão de conjunto diária | Baixo | D4 |

### Recomendação das próximas 3–5 fatias, por impacto

1. **D1** — sem incidentes persistidos, nada do resto (Telegram, portfolio
   diagnostics, histórico) tem uma base sólida. Maior alavancagem, menor
   risco.
2. **D2** — o ganho de UX mais imediato e visível, e não depende de nada
   além de D1.
3. **D3** — desbloqueia Telegram tecnicamente sem risco de enviar nada
   ainda (cliente mock-first), permite validar toda a política offline.
4. **D5** — Portfolio Diagnostics, porque reaproveita tudo o que D1/D2
   já construíram sem trabalho novo de regras.
5. **D4** — só depois de D3 estar testada e revista: é o primeiro momento
   em que uma mensagem real sai para o Telegram, e deve ficar escopada ao
   canário, tal como todo o resto desta sessão.

## 19. D1 — implementado, testado, LIVE VERIFIED (2026-08-21)

`findings.py` está inalterado, exactamente como pedido: continua recomputado
por pedido, sem persistência, a única fonte da lógica de regras.

### O que foi construído

- `diagnostics/models.py`: `DiagnosticIncident` — identidade
  `(rule_code, asset_id, device_id)`, `status` (`open`/`resolved`),
  `opened_at`/`last_observed_at`/`resolved_at`, `occurrence_count`,
  `detector_version` (= `findings.RULES_VERSION`, provenance da regra),
  `evidence_json` (snapshot do `evidence` do finding). Um único incidente
  `open` por identidade garantido por um índice único parcial funcional
  (`COALESCE(device_id, -1)`, porque Postgres trata cada `NULL` como
  distinto — um `UniqueConstraint` simples deixaria coexistir dois
  incidentes `open` para o mesmo finding ao nível de asset).
- `diagnostics/incidents.py`: `evaluate_and_persist_incidents` — uma
  passagem por avaliação, por asset, que chama `evaluate_asset_findings`
  (nunca reimplementa uma regra) e reconcilia com os incidentes `open`
  já persistidos: abre o que é novo, confirma o que continua, resolve o
  que deixou de aparecer.
- Migration `0017_diagnostic_incidents` (aditiva, nenhuma tabela existente
  tocada; `downgrade()` recusa-se se houver linhas, tal como `0015`/`0016`).
- `Job`/`ScheduleState`/`Scheduler` reutilizados sem tabela nova:
  `JobRepository.enqueue_due_incident_evaluation` (sem `connection_id` nem
  cap — zero chamadas a provider, nunca precisou de nenhum dos dois) e
  `jobs.handlers._execute_incident_evaluation`, o job type
  `diagnostics.evaluate_incidents`.
- `Settings.diagnostic_incident_evaluation_enabled`/`_interval_minutes`
  (desligado por omissão, 15 min por omissão).

### Um bug real encontrado ao tentar persistir, não hipotético

`DiagnosticFinding.evidence` inclui valores `Decimal` (ex.:
`active_power_kw`) — a primeira tentativa de gravar um incidente falhou com
`TypeError: Object of type Decimal is not JSON serializable`. A mesma classe
de bug que `reporting/datasets.py` já tinha resolvido para outro payload.
Corrigido correctamente, não com um workaround local: `json_safe` foi
extraído para `nemsei/shared/json_safe.py` (utilizado agora por
`reporting/datasets.py` e por `diagnostics/incidents.py`), em vez de
duplicar a função.

### Testado

54 testes novos: `test_diagnostics_incidents.py` (10 — identidade,
provenance, dedup por episódio, idempotência, restart, resolução com
duração, novo episódio após resolução não reutiliza a linha, finding ao
nível de asset com `device_id` nulo, o índice único a recusar um duplicado
mesmo contornando o serviço, dispositivos independentes não se
contaminam), `test_jobs.py`/`test_scheduler.py` (agendamento idempotente,
restart-safe, ticks concorrentes só criam um job, o `Scheduler.run_once()`
real liga/desliga com a config), `test_worker.py` (o pipeline completo
Job→Scheduler→Worker→handler, não a função chamada directamente). Suite
completa da V2: **535 passed, 1 skipped**, sem regressões.

### LIVE VERIFIED contra produção real

Antes de tocar na BD viva: `pg_dump -Fc` verificado (cabeçalho `PGDMP`
confirmado). Migration `0017` aplicada via o serviço `migrate` canónico
(`docker compose ... run --rm migrate`), não SQL manual. Imagens
`web`/`worker`/`scheduler` reconstruídas a partir do código committed
(`docker compose ... up -d --build`) — não `docker cp` avulso desta vez,
dado o número de ficheiros tocados.

Ligado via `docker-compose.v2.diagnostic-incidents.yml` (override,
`scheduler`/`worker` apenas, `.env.v2` nunca tocado — mesmo padrão do
override do canário). Dois ciclos reais observados, 5 min de intervalo:

| Ciclo | Hora (UTC) | Incidentes abertos | Incidentes confirmados | Total de incidentes `open` |
| --- | --- | --- | --- | --- |
| 1 (job 96) | 10:38:59 | 642 | 0 | 642 |
| 2 (job 97) | 10:44:00 | 0 | 642 | 642 |

**Prova directa, com dados reais de produção, do requisito central: o mesmo
problema persistente continua a ser um único incidente.** Nenhuma linha
duplicada entre os dois ciclos — os mesmos 642 incidentes, `occurrence_count`
1→2, `opened_at` inalterado (datas reais de Maio-Julho 2026, herdadas do
histórico importado da V1 via `active_since`), `last_observed_at` avançado
para a hora real de cada ciclo.

Composição real dos 642: 323 `stale_reading` + 319 `device_unknown_status`
— nenhum `device_unavailable`/disparidade entre pares hoje. Isto não é uma
falha do motor: reflecte honestamente o que `DIAGNOSTICS.md` já tinha
documentado — a maioria dos 325 dispositivos importados da V1 só tem uma
leitura histórica esporádica, sem cadência viva, e uma parte relevante dos
códigos de estado brutos da V1 nunca caiu em `available`/`unavailable`
reconhecidos pelo classificador (correctamente devolvendo `unknown`, não
inventando um valor). Um resultado honesto, não um resultado bonito.

**A prova de "a recuperação fecha o incidente" não foi feita contra produção
real, deliberadamente** — os 325 dispositivos importados da V1 não têm
nenhuma leitura nova a chegar (sem canário activo mais além do que já existia),
pelo que não há nenhum episódio real a recuperar-se dentro desta janela.
Forjar uma leitura de recuperação sintética na BD de produção violaria a
honestidade dos dados que esta sessão inteira defende. Em vez disso, essa
propriedade está provada pela suite de testes automatizada, contra um
Postgres real (não SQLite, não mocks) —
`test_recovery_resolves_the_incident_and_preserves_its_duration` e
`test_a_new_episode_after_resolution_is_a_new_incident_not_a_reused_row`,
ambos verdes. Quando o canário de disponibilidade (§10 de
`DEVICE_TELEMETRY.md`, ainda bloqueado) alguma vez recuperar um dispositivo
real, essa mesma propriedade fica automaticamente observável em produção
também, sem alterar nada aqui.

### Estado, config actual em produção

`NEMSEI_V2_DIAGNOSTIC_INCIDENT_EVALUATION_ENABLED=true` para
`scheduler`/`worker`, intervalo por omissão (15 min, código), `web`/`migrate`
inalterados. Kill switch: remover
`docker-compose.v2.diagnostic-incidents.yml` do próximo `up -d`, ou pôr
`ENABLED=false` nesse ficheiro e redeployar — nenhum dos dois apaga
histórico de incidentes.

### Milestone

**D1: IMPLEMENTED, TESTED, LIVE VERIFIED.** Nenhum Telegram, nenhuma
chamada a provider nova, nenhuma migration destrutiva. D2 (UI de
Diagnostics Overview/Incidents) é o próximo passo natural — já tem dados
reais prontos para mostrar.

## 20. D2 — Overview + Incidents, implementado e testado (2026-08-21)

Sem migration nova, sem tabela nova — só leitura sobre `diagnostic_incidents`
(D1). `diagnostics/findings.py` continua intocado.

### Decisão de navegação: duas páginas, não quatro

O esboço original de §11 sugeria `Overview`/`Incidents`/`Installations`/
`Notification history` como quatro entradas separadas. Implementado como
**duas**: a página `Installations`/`Overview` original (`/diagnostics`) foi
enriquecida no próprio lugar em vez de duplicada — teria sido exactamente o
tipo de "navegação excessiva" que o pedido original pediu para evitar.
`Notification history` fica fora até D3/D4 existirem (não há nada para
mostrar sem notificações). `Incidents` (`/diagnostics/incidents`) é a única
página nova.

### `/diagnostics` (Overview, enriquecido)

- Tira de KPIs no topo: total de instalações, quantas têm um crítico activo,
  quantas só têm avisos, quantas não têm nenhum finding activo.
- Cada linha mostra badges de severidade (contagem real de incidentes
  `open` por severidade, não um número inventado) em vez de nada.
- **Ordenado por gravidade primeiro, não alfabeticamente** — e a ordenação
  acontece **antes** de qualquer limite de exibição, nunca depois: um limite
  aplicado antes da ordenação esconderia uma instalação genuinamente crítica
  só por não começar por uma letra cedo no alfabeto. Provado directamente
  por teste (`test_overview_sorts_a_critical_installation_before_a_healthy_one_regardless_of_name`).
- Pesquisa existente mantida sem alteração de comportamento.

### `/diagnostics/incidents` (nova)

Todos os incidentes `open`, em todas as instalações, mais graves primeiro e
depois mais antigos primeiro (`opened_at` ascendente) — um problema há mais
tempo sem ninguém ver é pelo menos tão relevante como um mais recente da
mesma severidade. Mostra "aberto há" e "última confirmação há" (duração
humana, `diagnostics_queries.duration_label`), a regra que originou o
incidente, e liga de volta ao diagnóstico da instalação. Sem botão de
"resolver" — o incidente fecha-se sozinho quando o finding deixar de
aparecer, consistente com o lifecycle já definido em D1.

### Um bug real encontrado pelo próprio teste, não hipotético

A pesquisa em `/diagnostics/incidents` devolvia **zero resultados sempre**,
mesmo para uma pesquisa que devia bater certo. Causa: `asset_search_clause`
referencia `Organization.display_name`, mas a query de incidentes nunca
juntava `Organization` — o SQLAlchemy acrescentava-a como uma tabela não
associada no `FROM` (um produto cartesiano real, avisado pelo próprio
SQLAlchemy como `SAWarning`), e com zero organizações na base de dados (o
caso normal para a maioria das instalações), um cross join com uma tabela
vazia devolve sempre zero linhas — para todas as pesquisas, correctas ou
não. Corrigido com o mesmo `outerjoin(Organization, ...)` que
`searchable_assets_with_devices` já tinha, por a mesma razão. Encontrado
porque o teste de pesquisa (`test_incidents_page_search_filters_by_installation`)
falhou de propósito, não por inspecção manual.

### Testado

8 testes novos em `test_diagnostics_web.py`: ordenação worst-first
independente do nome, badge "sem findings activos", contagem do resumo,
listagem de incidentes worst-first, página vazia quando nada está aberto, e
o teste de pesquisa que apanhou o bug acima. Suite completa da V2: sem
regressões (ver relatório da sessão).

### Milestone

**D2: IMPLEMENTED, TESTED, deployed to production.** `web` reconstruído a
partir do código committed e redeployado (`docker compose ... up -d --build
web`), sem alteração de dados. Confirmado a responder correctamente sem
erro de servidor (`/diagnostics` e `/diagnostics/incidents`, ambos `302`
para login, não `500`). **Não confirmado visualmente num browser
autenticado** — esta sessão não tem a credencial de administrador real, e
não tentou contorná-la; os 642 incidentes reais de D1 estão prontos na BD
para esta UI os mostrar assim que alguém entrar.

## 21. D3 — Notification infrastructure, mock-only, implementado e testado (2026-08-21)

Aprovado explicitamente com um limite muito claro: **nenhuma mensagem
Telegram real nesta fatia**. Cumprido estruturalmente, não por convenção —
`notifications/telegram_client.py` só tem uma interface (`TelegramClient`,
um `Protocol`) e um mock (`MockTelegramClient`, que nunca abre um socket).
Não existe nenhum caminho de código nesta fatia capaz de uma chamada HTTP
real; um cliente real é trabalho de D4, atrás da mesma interface.

### Modelo: três tabelas novas, `diagnostic_incidents` inalterada

```
DiagnosticIncident (D1, inalterado)
        │
        ▼
NotificationPolicy   -- decide SE um episódio merece notificar, e quando
        │
        ▼
NotificationEvent    -- registo persistido e auditável de UMA decisão
        │              (e, separadamente, da sua entrega)
        ▼
NotificationChannel  -- para onde a decisão seria entregue
        │
        ▼
TelegramClient (interface + mock; D4 adiciona o real)
```

Migration `0018_notifications` (aditiva, `diagnostic_incidents` intocada).
**Um bug real apanhado pelo próprio teste de migração**, não hipotético:
`CHANNEL_KINDS = ("telegram",)` é um tuplo Python de um elemento — a sua
representação `!r` mantém a vírgula final (`('telegram',)`), que o Postgres
rejeita dentro de `IN (...)` como erro de sintaxe mesmo antes de qualquer
dado real existir. Corrigido com um pequeno helper (`_sql_in_list`) em vez
de reescrever a constraint à mão; aplicado tanto ao modelo como à migration.

### Identidade estrutural, não texto de mensagem

`NotificationEvent` é único em `(incident_id, kind, channel_id)` —
`kind ∈ {opened, escalated, resolved}`. Isto, sozinho, responde à maior
parte dos nove fluxos pedidos:

- Um incidente novo elegível cria exactamente uma linha `opened`.
- Reavaliar o mesmo incidente não cria nada novo — a identidade já existe.
- Um incidente que continua aberto **não** volta a notificar por si só; só
  uma regra explícita (`escalation_after_minutes`) pode criar uma segunda
  linha, `escalated`, e mesmo essa só uma vez por incidente (a mesma
  identidade estrutural aplica-se a cada `kind`).
- Um incidente resolvido pode produzir uma linha `resolved`, se
  `notify_on_resolve=True`.
- Um episódio que resolve e reaparece é, por desenho da própria D1, uma
  **nova linha `DiagnosticIncident`** — logo automaticamente uma nova
  identidade de notificação também, sem nada a repor aqui.
- Incidentes diferentes no mesmo asset/device (rule_codes diferentes) nunca
  colidem — a identidade inclui `incident_id`, não `(asset_id, device_id)`.

### Um bug real de concorrência, apanhado pelo próprio teste de concorrência

A primeira versão de `_decide_for_policy` verificava `_has_event` e depois
inseria — uma janela real entre leitura e escrita. Um teste com 4 threads a
decidir o mesmo incidente em paralelo (proof #7) fez exactamente o que
devia: a constraint única do Postgres rejeitou a segunda inserção, mas o
código **deixava o `IntegrityError` propagar e abortar a transacção
inteira**, em vez de tratar a corrida como um não-evento gracioso. Corrigido
com um `SAVEPOINT` (`session.begin_nested()`) à volta de cada inserção
individual — quem perde a corrida devolve `None` e simplesmente não conta
nada, sem abortar o resto do lote. Sem isto, um restart/concorrência real
teria podido falhar o job inteiro por causa de uma única colisão, não só
"duplicar" — um bug mais grave do que o pedido original antecipava.

### Estados de `NotificationEvent`: quatro, não mais

`pending` (decidido, ainda por entregar) → `sent` **ou** `failed` (só a
partir de `pending`/`failed`, nunca de `skipped`) — mais `skipped`, uma
decisão real e final tomada no momento da criação (canal desligado), não um
não-evento silencioso. Uma falha nunca marca `sent` — `sent_at` só é escrito
quando `TelegramClient.send_message` devolve `delivered=True`, e a
constraint `(status = 'sent') = (sent_at IS NOT NULL)` torna isto impossível
de violar mesmo por um bug futuro no código. Um `failed` é reavaliado em
cada passagem seguinte (retry ilimitado — D3 não tem nenhum modo de falha
real contra o qual desenhar um limite/backoff; isso é trabalho de D4, com
um cliente real).

### Anti-recorrência bruta

`occurrence_count`/dedup nunca contam linhas brutas — a identidade é
`(incident_id, kind, channel_id)`, e `DiagnosticIncident.occurrence_count`
(D1) já era um contador por ciclo de avaliação, não por linha de facto.
`MINIMUM_ALERT_SEVERITY` da V1 (config morta, nunca lida) não foi portado —
`min_severity` aqui é um campo real, verificado em `_in_scope` a cada
decisão.

### Baseline, não mascarado na policy

Ver §22 (dry-run) — a decisão de excluir incidentes pré-existentes da
notificação `opened` é feita de forma honesta e documentada
(`NotificationPolicy.baseline_at`), o mesmo mecanismo que a V1 já precisou
de construir (`ALERT_BASELINE_AT`), não uma forma de esconder o volume real.

### Testado

19 testes em `test_notifications.py`, um por cada um dos nove fluxos
pedidos mais os limites de âmbito (severidade, rule_codes, baseline,
baseline não se aplica a `resolved`) e a constraint da BD directamente. Mais
6 testes de agendamento (`test_jobs.py`/`test_scheduler.py`, mesmo padrão de
D1: idempotente, restart-safe, ticks concorrentes) e 1 teste
Job→Scheduler→Worker→handler de ponta a ponta
(`test_worker_executes_a_real_notification_processing_cycle_end_to_end`).
Suite completa da V2: ver resultado no relatório da sessão.

## 22. Dry-run contra os 644 incidentes reais e vivos (2026-08-21)

**Só leitura.** Nenhuma escrita à base de dados de produção para produzir
isto — um `SELECT` directo à BD viva (`diagnostic_incidents`, `status='open'`),
depois simulado localmente com a mesma lógica de âmbito de
`notifications/service.py` (severidade + `rule_codes` + `baseline_at`),
sem nenhuma `NotificationPolicy` real criada em produção.

(O número subiu de 642 para **644** entre D2 e agora — os dois dispositivos
canário do asset 153 ficaram `stale_reading` desde a última leitura viva de
ontem, 2026-08-20 10:59:18Z, exactamente como esperado: o polling do
dispositivo nunca foi reactivado depois da janela da Fatia 3. Sinal real,
não ruído do dry-run.)

### Composição real, hoje

| rule_code | severidade | contagem |
| --- | --- | --- |
| `device_unknown_status` | warning | 319 |
| `stale_reading` | warning | 325 |

**Zero `critical`. Zero `device_unavailable`. Zero disparidade entre pares.**
Os únicos dois rule_codes activos hoje são os dois que `DIAGNOSTICS.md` e o
audit da V1 já tinham identificado como "sinal de fundo estrutural", não
"evento operacional novo" — `device_unknown_status` porque a maioria dos
códigos de estado brutos importados da V1 nunca caiu no vocabulário
reconhecido, `stale_reading` porque a esmagadora maioria dos 325
dispositivos não tem nenhum polling ao vivo activo.

### Simulação de quatro propostas de policy

| Policy | Notificáveis | Excluídos por âmbito | Excluídos por baseline |
| --- | --- | --- | --- |
| A — severidade≥aviso, sem âmbito, sem baseline (ingénua) | **644 / 644** | 0 | 0 |
| B — só crítico, sem baseline | 0 / 644 | 644 | 0 |
| C — **recomendada**: rule_codes accionáveis + severidade≥aviso + baseline=agora | 0 / 644 | 644 | 0 |
| D — mesmo âmbito de C, sem baseline (isola o âmbito) | 0 / 644 | 644 | 0 |
| E — severidade≥aviso, sem âmbito, com baseline=agora (isola a baseline) | 0 / 644 | 0 | 644 |

**Policy A confirma exactamente o risco que o pedido antecipava**: uma
policy ingénua por severidade sozinha dispararia as 644 de uma vez — uma
tempestade real, não hipotética.

**Policy C (a recomendada) chega a zero por uma razão honesta, não por
mascarar o volume**: nenhum dos 644 incidentes de hoje é
`device_unavailable`/disparidade entre pares — os únicos rule_codes
genuinamente accionáveis. Não é a baseline a fazer esse trabalho (Policy D,
mesmo âmbito sem baseline, chega ao mesmo zero) — é a composição real dos
dados. **Isto responde directamente ao pedido de "identifica primeiro se o
problema está nos findings/incidents"**: está. `device_unknown_status` e
`stale_reading`, à densidade de amostragem actual (quase todos os 325
dispositivos sem polling ao vivo), não são hoje sinais accionáveis
individualmente — são visíveis no dashboard (D2), correctamente, mas não
deviam ainda disparar uma notificação Telegram por dispositivo.

**Policy E prova que a baseline, sozinha, também chegaria a zero** — todos
os 644 têm `opened_at` de Maio-Agosto, antes de qualquer momento de
activação de uma policy hoje. As duas defesas (âmbito de regra + baseline)
são complementares, não redundantes: o âmbito protege contra
`device_unknown_status`/`stale_reading` continuarem ruidosos mesmo depois de
zerar a baseline; a baseline protege contra qualquer futuro backlog de
importação, de qualquer rule_code, criar uma tempestade semelhante outra
vez.

### Recomendação sobre a policy proposta

**Não portar `device_unknown_status`/`stale_reading` para notificação
individual por agora.** Ficam visíveis no dashboard (`/diagnostics`,
`/diagnostics/incidents`, D2) — que é precisamente onde um sinal de fundo,
não-urgente, pertence. Reconsiderar `stale_reading` especificamente quando
o polling ao vivo cobrir uma fracção maior da carteira (hoje: 2 de 325
dispositivos) e deixar de ser uma característica estrutural quase universal.

**Não foi necessário mascarar nada na `NotificationPolicy`** — o âmbito
recomendado (`rule_codes` restrito a `device_unavailable`,
`zero_power_while_peers_active`, `power_disparity_among_peers`,
`daily_energy_disparity_among_peers`) é uma decisão honesta sobre que
sinais são accionáveis, documentada, não um ajuste para esconder um número
feio.

### Estado em produção

**Schema aplicado (migration `0018`), código deployado, processamento
ainda DESLIGADO.** `notification_processing_enabled=False` por omissão, sem
override de compose criado para o ligar — ao contrário de D1, esta sessão
optou por não activar a avaliação ao vivo de notificações mesmo sabendo
(pelo dry-run) que produziria zero eventos reais hoje. Razão: o conteúdo da
policy proposta ainda não teve uma aprovação explícita separada do "D3 está
implementado" — este dry-run é precisamente o que serve essa revisão. Nem
um único `NotificationChannel`/`NotificationPolicy` real foi criado na BD
de produção.

### Milestone

**D3: IMPLEMENTED, TESTED. Schema em produção, processamento ao vivo
DESLIGADO por decisão deliberada**, não por limitação técnica — ver
recomendação em §23.

## 23. Recomendação sobre D4 (Telegram real)

**Ainda não avançar para D4 sem mais uma decisão intermédia primeiro**, e
por uma razão específica, não genérica: a infraestrutura (D3) está pronta e
testada, mas **a policy real ainda não foi activada nem revista por um
humano com os números do dry-run à frente** — activar D4 (entrega real)
sem antes confirmar a policy resultaria em zero mensagens reais hoje de
qualquer forma (dado o dry-run), o que é seguro, mas significa que D4
ficaria "aprovado" sem nunca ter sido genuinamente exercitado contra uma
decisão real.

Ordem recomendada:

1. Rever e aprovar explicitamente o conteúdo da Policy C (ou uma variante)
   como a policy real a activar — não implícito em "D3 está aprovado".
2. Activar `notification_processing_enabled` em produção (mesmo padrão de
   override de compose que D1 usou), com essa policy real mas
   **`NotificationChannel.enabled=False`** — prova que o pipeline decide
   correctamente em produção (cria `NotificationEvent` `skipped`, nunca
   `pending`) sem nenhum risco de entrega, nem mock nem real.
3. Só depois disso, D4: um `TelegramClient` real atrás da mesma interface,
   com um bot/chat de teste, `enabled=True` só para esse canal de teste,
   antes de qualquer canal de produção real.

Isto não é burocracia extra — é a mesma disciplina que M7 já seguiu em
Fatia 2/3 (canário antes de escala) e em D1 (schema e mecanismo antes de
dados reais visíveis), aplicada ao único componente desta fatia que ainda
não foi genuinamente exercitado: uma policy real, decidindo sobre
incidentes reais, à frente de um humano.
