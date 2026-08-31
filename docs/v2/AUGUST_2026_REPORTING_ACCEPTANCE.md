# August 2026 reporting — acceptance ledger

What August reporting needs, what it already has, and what is still in the way.
Evidence and outstanding work only; this is not a diary.

Opened 2026-08-31. Baseline `acde328` (remote and local agreed, tree clean, V2
CI green for that SHA).

---

## 1. What the audit found

### Commercial model — the three ESCO rates already exist

`AssetBillingConfig` carries exactly the three values an ESCO contract needs,
and `reporting/rules/billing.py` already applies them the intended way:

| Operator term         | Column                      | Rule in `calculate_billing`             |
|-----------------------|-----------------------------|-----------------------------------------|
| Taxa de venda         | `solcor_price_per_kwh`      | `solcor_payment = billable × taxa`      |
| Taxa de poupança      | `default_electricity_price` | `savings = self_use × taxa`             |
| Venda de excedente    | `default_export_price`      | `export_revenue = export × venda`       |

Verified in code, not assumed. **No migration, no new columns.** What was
missing was an operator-facing way to set them and labels that name them the
way the business does.

`fixed_monthly_fee_eur` and the Cheias/Ponta/Vazio/Super-Vazio tariff stay
supported and stay optional — neither blocks an ESCO report.

### Finality was wrong, and it gated every euro

`aggregate_rows` called a period final when every month it covered produced a
total. `prepare_customer_report` withholds all monetary fields on a non-final
period — that guard was already correct. Feeding it "every month has a number"
made Expertcom's August (25 of 31 days, none for 19–23, month not over) look
final, so it would have stated a month's savings and a month's ESCO invoice.

Fixed in `reporting/finality.py` (commit `11fc0fc`): a period is final only when
it **has ended** and **every day it covers has a current, non-missing fact**.

### August data, at the start of the session

| Metric              | Rows | Assets | Note                                  |
|---------------------|-----:|-------:|---------------------------------------|
| `production_energy` |  351 |    132 | 95 assets had 2 days, 32 had 1        |
| the other four      |   33 |      2 | Sigenergy only                        |

Root cause found: **131 FusionSolar plant mappings and their production source
policies carried `valid_from = 2026-08-25`**, because `create_mapping` defaults
`valid_from` to the day it runs (`providers/service.py:191`) and that is the day
they were bulk-created. `_selected_mappings` filters by that date, so the sync
structurally could not fetch 2026-08-01..08-24 for the fleet.

V1 is not an alternative source for August: `production_records` holds 23 daily
rows for 1 asset in August (max `period_date` 2026-08-23). V1 read-only,
unmodified.

**Operator decision taken 2026-08-31:** backdate those 131 mappings and 131
policies to 2026-08-01. Non-destructive — no deletes, `production_facts` is
append-only and the sync is idempotent, no superseded mappings and no `valid_to`
anywhere in the set, so attribution is unambiguous. Prior values recorded before
the change.

### Commercial configuration is the fleet's real blocker

**1 of 267 assets has an `AssetBillingConfig`** (asset 319, Expertcom, ESCO,
`v1_import`). Contract types: 85 ESCO + 5 ESCO BUYOUT, 154 EPC + 9 EPC (O&M),
14 unstated. Every other ESCO installation needs its three rates entered before
it can produce a financial report. That is operator input, not code.

---

## 2. The golden cases

### Expertcom — asset 319, alias `Expertcom`, canonical `TZXRS1780315946`

Provider: **Sigenergy** connection 5 (mapping 729, active). ESCO.

July 2026 reconciles exactly against the historical oracle:

| Metric           | V2 persisted facts | Oracle        |
|------------------|-------------------:|--------------:|
| production       |          22 965.45 |     22 965.45 |
| self-consumption |          17 500.13 |     17 500.13 |
| export           |           5 461.22 |      5 461.22 |
| consumption      |          31 465.16 |     31 465.16 |
| grid import      |          13 965.03 |     13 965.03 |
| daily rows       |                 31 |            31 |

August 2026: 25 distinct days present, **missing 2026-08-19 … 2026-08-23**.
Therefore provisional, and correctly so.

### Solcorelios

Portfolio 1 "Solcorelios I" — 47 memberships. Portfolio 2 "Solcorelios II" — 33.
No `portfolio_report_runs` rows existed at the start of the session.

---

## 3. Where August stands

Every installation that can honestly produce an August report has one. Read off
the live deployment after generation:

| | 2026-08 |
|---|---:|
| installations | 267 |
| ESCO | 90 |
| with August energy | 114 |
| **August snapshots produced** | **114** (+1 predating the fields) |
| of those, stating euros | **0** — the month has not closed |
| no active plant mapping / no energy | 153 |
| ESCO with no rates entered | 89 |
| where a euro figure is possible at all | 1 |

Generation failures: **0**. Every one came out `provisional`, which on the 31st
of August is the only honest answer.

### Work items

| # | Item | Status |
|---|------|--------|
| 1 | Day-coverage-aware finality; provisional/final/blocked in the payload | done — `11fc0fc` |
| 2 | pypdf in the dev lock so the PDF golden stops skipping itself | done — `11fc0fc` |
| 3 | Backdate the 131 mapping/policy validity rows | done — §1 |
| 4 | Simple ESCO three-rate configuration, Portuguese labels, ESCO validation | done — `d37ae04` |
| 5 | `reporting.month_close` — re-evaluate and finalise without a shell | done — `d37ae04` |
| 6 | `/reports` as an operator workspace: readiness, blockers, ESCO first | done — `aa1ded4` |
| 7 | Month close declared as a required deployment component | done — `60ebb88` |
| 8 | August generated for all 114 eligible installations | done — §3 |
| 9 | Expertcom July regression + August provisional, PDF and XLSX | done — §4 |
| 10 | Solcorelios I and II August runs, review-ready, not approved | done — §4 |
| 11 | Fleet August backfill 2026-08-03 … 08-30 | durable, queued — §5 |

### Live deployment

Deployed through `scripts/v2_compose_up.sh`. All five containers up, restart
count 0 each, `4 declared component settings are live in the running
containers`. `NEMSEI_V2_REPORT_MONTH_CLOSE_ENABLED=true` verified with
`printenv` inside both scheduler and worker.

`reporting.month_close` ran as job 3441 and is scheduled hourly
(`schedule_state.reporting.month_close`). It currently tracks **40 provisional
periods** and finalises none, twice in a row with identical output — August has
not ended.

Every reporting URL was rendered against the live database: `/reports` for two
months, `/reports/assets` with each filter, the per-installation page, and both
`.pdf` and `.xlsx` snapshot downloads. All 200, PDFs and workbooks real bytes.

---

## 4. Golden acceptance, run against the live database

### A — Expertcom (asset 319), July 2026 as the regression oracle

Snapshot 66, dataset 213, digest `f5cc55b2fc4b`. `reporting_state = final`,
31/31 source days, no reasons outstanding.

| Metric           | V2 assembled | Oracle        |     |
|------------------|-------------:|--------------:|-----|
| production       |    22 965.45 |     22 965.45 | OK  |
| self-consumption |    17 500.13 |     17 500.13 | OK  |
| export           |     5 461.22 |      5 461.22 | OK  |
| consumption      |    31 465.16 |     31 465.16 | OK  |
| grid import      |    13 965.03 |     13 965.03 | OK  |
| daily rows       |           31 |            31 | OK  |

The month is closed and complete, so the euros are stated: poupança 3 120,07 €,
receita de excedente 245,75 €, faturação ESCO 1 505,01 €, benefício líquido
1 860,82 €. `availability_pct` and the four tariff-period splits stay N/D and
are named in `unavailable_fields`. PDF 9 405 B, XLSX 8 259 B, both rendered from
the frozen snapshot.

### A — Expertcom, August 2026

Snapshot 67, dataset 214, digest `aee8a9e9d6e5`. `reporting_state = provisional`,
25/31 source days, reasons `period_still_open` and `missing_source_days:6`
(2026-08-19 … 08-23, plus 08-31 which had not happened yet).

Energy is reported: production 11 720,90 kWh, self-use 9 131,31, export
2 589,64, consumption 51 156,94. **Every euro is N/D**, as is grid import,
because the period is not closed. PDF 8 877 B, XLSX 8 168 B.

11 720,90 against a raw sum of 11 804,93: one day carries a corrected revision,
and only the current one counts.

### B — Solcorelios I and II, August 2026

| | Solcorelios I | Solcorelios II |
|---|---|---|
| run | 4 | 5 |
| status | `generated` | `generated` |
| approved | **no** | **no** |
| members in period | 35 | 22 |
| ready | 26 | 14 |
| blocked | 9 (`sem_dados_de_producao_no_periodo`) | 8 (same) |
| unresolved members | 12 | 11 |
| coverage label | 26/35 | 14/22 |
| member snapshot states | 26 × provisional | 14 × provisional |
| Σ production of members | 56 894,17 kWh | 32 077,59 kWh |

The aggregate declares its blocked and unresolved members rather than quietly
totalling a smaller number. Neither run was approved.

### C — immutability and provider isolation

1. PDF and XLSX rendered with `socket.socket` and `socket.create_connection`
   replaced by raisers. Both produced bytes: **the renderers open no
   connection** — proven, not argued from imports.
2. Reopening snapshot 67 returns the frozen numbers (11 720,90, provisional,
   25 days), never a recomputation.
3. Regenerating August over unchanged facts returned snapshot **67 again**:
   identical input reuses the snapshot, by design.
4. `UPDATE report_snapshots` is refused by the `report_snapshots_immutable`
   trigger, from psql and through the ORM alike.

### D — skips

The only skip in the baseline suite was the whole of `test_pdf_golden.py`,
which `importorskip`ed itself away for want of pypdf. With pypdf locked it runs:
6 tests, 0 skips, including the page-by-page comparison against the frozen V1
checkout.

---

## 5. August ingestion

The fleet's August production is fetched by the existing durable job system,
never by anything this session has to stay open for.

### The blocker, and how it was cleared

131 FusionSolar plant mappings and their production source policies carried
`valid_from = 2026-08-25` — `create_mapping` defaults it to the day it runs
(`providers/service.py:191`) and that is the day they were bulk-created.
`_selected_mappings` filters by that date, so the sync structurally could not
ask for 2026-08-01..08-24. Backdated to 2026-08-01 with operator approval; prior
values recorded first. Coverage went from 351 facts across 132 assets (most with
one or two days) to full fleet coverage of 2026-08-01 and 08-02 within minutes.

### The provider limit, measured rather than assumed

| sync run | window | provider calls | items accepted | outcome |
|---|---|---:|---:|---|
| 2135 | 08-01 | 3 | 98 | partial, then refused |
| 2129 | 08-01 | 1 | 0 | refused |
| 2136 | 08-07 | **0** | 0 | refused *locally*, attempt not charged |
| 2141 | 08-03 | 2 | 0 | refused |
| 2146 | 08-03 | 1 | 0 | refused |
| 2147 | 08-04 | 0 | 0 | refused locally |

Run 2136 is the rate-limit deferral working exactly as designed: the persisted
cooldown turned the job away before any HTTP, and it was not charged an attempt
for a call it never made. That mechanism needed no change.

By 21:30 UTC the account was refusing on the **first** call, repeatedly, over
forty minutes, with `production_history_daily.actual_call_count` at 265. That is
an exhausted allowance, not a cadence problem — frequency limits back off in ten
minutes and this did not.

### What is scheduled

A six-day window cannot finish inside one attempt at this throughput, and a
bounded backfill restarts at its window start unless it succeeded outright, so
each retry re-fetched the same first days and ran out of attempts without
advancing. Jobs 3399–3402 were cancelled for that reason.

Replaced by **26 single-day jobs, 3415–3440** — one source day each (two calls:
134 mappings in batches of 100), `max_attempts` raised to 10 so ~600 s cooldowns
do not exhaust them overnight. Two run now as a probe; the other 24 are spaced
11 minutes apart from **2026-09-01 00:20 UTC to 04:33 UTC**, on the assumption a
daily allowance rolls over at midnight. If that assumption is wrong they simply
wait longer, which is what they are built to do.

They cover every day the fleet still lacks: 2026-08-03 … 08-24 and 08-27 …
08-30. They need no session, no shell and no supervision.

### What happens when they land

Nothing has to be remembered. `reporting.month_close` runs hourly, sees the day
coverage change, and once September has begun and a month's days are all
present it produces a **new final snapshot** beside the provisional one. The
provisional report is not touched.

---

## 6. Blockers

Kinds: **CODE** · **DATA** · **PROVIDER** · **OPERATOR** · **OUT OF SCOPE**

- **OPERATOR** — 266 of 267 assets have no billing configuration. No ESCO
  financial report is possible for them until the three rates are entered. Not
  fixable in code; fabricating a rate is the one thing reporting must not do.
  The form to enter them now exists and validates.
- **PROVIDER** — the FusionSolar account had exhausted its
  `production_history_daily` allowance by 21:30 UTC on 2026-08-31: refused on
  the first call, repeatedly, for forty minutes, with the family's lifetime
  counter at 265. 22 of August's 31 source days are still short of the fleet.
  Scheduled durably across 00:20–04:33 UTC with ten attempts each; see §5.
  Nothing else in the system is waiting on it.
- **DATA** — only the 3 Sigenergy-mapped assets carry self-use, export,
  consumption and grid import. FusionSolar's verified contract exposes `PVYield`
  alone, so every FusionSolar installation can produce an *energy* report and no
  financial one, whatever its commercial configuration says. `money_possible`
  on `/reports` reads 1 for exactly this reason.
- **DATA** — 133 of 267 assets have no active plant mapping at all and cannot
  report any month.
- **DATA** — `availability_pct` and the tariff-period self-use splits have no
  trustworthy source and stay N/D. Neither blocks an ESCO report.
- **OUT OF SCOPE** — no code path builds the portfolio Excel payload from a
  run; `build_portfolio_report_workbook` is a golden-tested pure function with
  no caller. Individual PDF and XLSX both work end to end.

---

## 7. Test gates

`PYTHONPATH=src`, `NEMSEI_V2_TEST_DATABASE_URL` pointing at the isolated
`nemsei_v2_test` database (container `nemsei-v2-test-pg`, host port 55432).

- `pytest -q tests_v2` — baseline before any change: 1115 passed, 1 skipped
  (that skip was the whole PDF golden module). After: **1159 passed, 0 skipped**.
- `ruff check --no-cache src tests_v2`
- `bash scripts/run_docker_recovery_acceptance.sh`
- `bash scripts/run_postgres_operations_acceptance.sh`
- canonical deploy: `bash scripts/v2_compose_up.sh`
