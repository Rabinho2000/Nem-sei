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

## 3. Work items

| # | Item | Status |
|---|------|--------|
| 1 | Day-coverage-aware finality; provisional/final/blocked in the payload | done — `11fc0fc` |
| 2 | pypdf in the dev lock so the PDF golden stops skipping itself | done — `11fc0fc` |
| 3 | Backdate the 131 mapping/policy validity rows | done — see §1 |
| 4 | Durable August backfill through the existing job system | in progress |
| 5 | Simple ESCO three-rate configuration, Portuguese labels | |
| 6 | `reporting.month_close` — re-evaluate and finalise without a shell | |
| 7 | `/reports` as an operator workspace: readiness, blockers, ESCO first | |
| 8 | Expertcom July regression + August provisional, PDF and XLSX | |
| 9 | Solcorelios I and II August runs, review-ready, not approved | |

---

## 4. Test gates

`PYTHONPATH=src`, `NEMSEI_V2_TEST_DATABASE_URL` pointing at the isolated
`nemsei_v2_test` database (container `nemsei-v2-test-pg`, host port 55432).

- baseline before any change: **1115 passed, 1 skipped** — the skip was the whole
  of `test_pdf_golden.py`, now removed as a skip by item 2.
- `ruff check --no-cache src tests_v2`
- `bash scripts/run_docker_recovery_acceptance.sh`
- `bash scripts/run_postgres_operations_acceptance.sh`

---

## 5. Blockers

Kinds: **CODE** · **DATA** · **PROVIDER** · **OPERATOR** · **OUT OF SCOPE**

- **OPERATOR** — 266 of 267 assets have no billing configuration. No ESCO
  financial report is possible for them until the three rates are entered. Not
  fixable in code; fabricating a rate is the one thing reporting must not do.
- **PROVIDER** — the FusionSolar account rate-limits by frequency (~25–29
  consecutive calls, ~600 s cooldown). The August backfill is therefore
  deliberately split into staggered durable jobs rather than one long run.
- **DATA** — only the 3 Sigenergy-mapped assets have self-use, export,
  consumption and grid import. FusionSolar's verified contract exposes `PVYield`
  alone, so every FusionSolar installation can produce an *energy* report and no
  financial one, whatever its commercial configuration says.
- **DATA** — `availability_pct` and the tariff-period self-use splits have no
  trustworthy source and stay N/D. They do not block an ESCO report.
