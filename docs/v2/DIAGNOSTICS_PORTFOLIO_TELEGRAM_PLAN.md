# Diagnostics UI, Portfolio Diagnostics, Telegram: plano (não implementado)

Documento de planeamento apenas — sem código, sem migrations, sem alterações à
BD. Auditoria feita contra o V2 real e o V1 real (código, não memória), 2026-08-21.

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
