# Utility Bill Extraction: Process & Runbook

Turns utility bill PDFs from any provider into clean rows in a SQLite database and an Excel workbook, ready for analysis or a dashboard (Power BI, Tableau, Excel pivots).

**Fields captured.** Required: service address (as printed, plus city, state and ZIP in a separate address table), month of service, total cost for that address, and usage with its unit. Also captured: provider, account number, meter number, bill and due dates, service period, and total amount due. Each address gets one total cost, and the charge line items behind it (water, sewer, trash, taxes, fees…) are also recorded separately, linked to the same bill and address.

---

## 1. How it works

```
 PDF bills ──► 1. Intake ──► 2. AI extraction ──► 3. Complete ──► 4. Validate ──► 5. Store ──► 6. Export
 (inbox/)      fingerprint   Claude reads the      addresses:      math, ranges,    SQLite      Excel workbook
               skip dupes    PDF; output is        ZIP lookup,     missing data     bills.db    bills.xlsx
                             forced into a schema  units, month    │
                                                                    └─► "Needs Review" sheet ◄── human checks
```

| Step | What happens | Why |
|---|---|---|
| **1. Intake** | Every PDF in the inbox gets a SHA-256 fingerprint. Files already loaded are skipped. | You can re-run on the same folder safely; a bill emailed twice is loaded once. |
| **2. Extraction** | The PDF goes to Claude (`claude-opus-5-5`) as a document. Claude reads the text and the page images, so scanned bills work too. The reply must match a fixed schema ([schema.py](schema.py)). | Bill layouts differ by provider. One model handles them all, so there are no per-provider templates to maintain. |
| **3. Complete & normalize** | A service address with no ZIP is looked up with the free US Census geocoder, using the provider's city and state if the address has none. If that finds nothing, Claude looks up the city, state and ZIP with web search (see the definitions below). The result goes into the `service_addresses` table, one row per address; the bill tables keep the address exactly as printed. Units are mapped to standard names (`HCF`→`CCF`, `Gal.`→`gallons`, `KWH`→`kWh`). The service month is calculated. | The same property then matches across providers, and different providers can be compared and summed. |
| **4. Validate** | Rule checks run on every bill (see §3). Any failure marks the bill `needs_review` with a reason. | You trust the system because it flags its own weak spots. |
| **5. Store** | Bills go in `bills`, each service address on a bill in `bill_sites` (one total cost), charge items in `site_charges`, usage in `site_usage`, and each distinct address with its city, state and ZIP in `service_addresses`. | A summary invoice can cover many properties; each property's cost and usage stay separate. |
| **6. Export** | The workbook has these sheets: Cost by Address, Service Addresses, Charge Items, Cost by Category, Usage, Cost by Property-Month, Usage Summary, Bills, Needs Review. | Ready for review and pivots, and to connect to a dashboard. |

### Key definitions (agreed business rules)

- **Service address** is where the utility is delivered, not the mailing address. Bills often show both. The bill tables store it **exactly as printed** (same words, abbreviations and capitals). The city, state and ZIP, whether printed or looked up, are stored only in the `service_addresses` table, linked by `address_id`.
- **Month of service** is the calendar month that holds the most days of the service period. Example: 15 Mar–14 Apr has 17 days in March, so it is March. Billing periods rarely match calendar months, so this rule keeps monthly comparisons consistent. If no period is printed, the month before the bill date is used.
- **Total cost** (`total_cost`, one per service address) is all new charges for that address for the period: every service, tax and fee. It excludes previous balance, payments and prior late fees. On a one-address bill it equals the bill's current charges. On a summary invoice it is the per-property subtotal, and the subtotals must add up to the bill's current charges. The `total_amount_due` printed on the bill is kept on the bill row for AP reconciliation.
- **Charge items** (`site_charges`) are the individual current-period lines behind each address's total cost, as printed. Examples: WATER, SEWER, SWR SC, GARBAGE, TAXES, CAREFLITE. Each item keeps its printed label and gets a `category`: electric, natural_gas, water, sewer, trash, stormwater, irrigation, tax or other. Base, customer and usage charges go to their service; taxes go to `tax`; program fees such as CareFlite or EMS go to `other`. Every item carries the `bill_id` and the `address_id`, so it links back to its bill and property. The items for an address must add up to its total cost, or the bill goes to Needs Review. On a summary invoice that prints only one total per property, that property gets a single item.
- **ZIP, city and state completion.** If the bill prints no ZIP for a service address, these steps run in order and stop at the first ZIP found:
  1. **Census geocoder.** The free US Census geocoder searches the street, using the city and state printed with the address, or else the city and state of the provider's own address on the bill. Only an unambiguous match is accepted. Example: "203 Tiffany Dr" on a City of Fate bill returns 75189. That is not 75087, the ZIP in the City of Fate's own address, so the lookup is by street, never just the city's ZIP.
  2. **Web search.** If the Census finds nothing, for example because the provider's address is missing or the street isn't in the Census data, Claude searches the web for the street's city, state and ZIP. It uses the provider and any printed city as clues, and checks sources such as appraisal districts, real-estate listings, USPS and maps. A low-confidence answer is not used.
  3. **Census re-check.** The web answer is checked with the Census geocoder. If the Census returns a different ZIP for that city, the Census ZIP wins, and the conflict is noted.

  `city_source` (`bill`/`provider`/`web`) and `zip_source` (`bill`/`lookup`/`web`/`web+census`) (in `service_addresses`) record where each value came from. `address_note` keeps the web source URL and any conflict. If no step finds a ZIP, the bill goes to Needs Review.

  Lookups are cached in the `zip_cache` and `web_address_cache` tables, so each address is searched only once. A web lookup takes about 15 seconds and costs about the same as extracting a bill. What leaves your machine: the street, city and state go to the Census service. The street, the printed city and the provider's name and city go to Claude for the web search.
- **Usage** is consumption for the period, not a meter reading. It is recorded for metered services only. Flat-fee services such as trash have no usage row, and sewer billed on water consumption doesn't repeat the water quantity. The printed unit is kept in `usage_unit_raw`, and the standardized unit is in `usage_unit`.
- **Missing units are filled in.** If the bill prints no unit next to the usage, the unit is chosen in this order, and `unit_source` records which rule was used:
  1. `bill`: the unit printed next to the usage.
  2. `guessed`: Claude's best guess from other clues on the bill, such as a usage chart labelled "in gallons", a rate table "per 1,000 gal", or meter multipliers. The reason is saved in `unit_basis`.
  3. `default`: the usual US unit for the service type. Electric is kWh, natural gas is therms, steam is lbs, and water, sewer, irrigation, propane and fuel oil are gallons.

  Filled-in units don't send a bill to review. To audit them, filter the Usage sheet on `unit_source` ≠ `bill`. Only usage of type `other` with no unit at all is flagged.
- **Multi-site (summary) invoices** list many properties on one account, such as a TXU invoice with 14 ESI IDs. These get one row per property, each with its own address, service period, month of service and total cost.

---

## 2. Running it

**One-time setup**

```bash
pip install -r requirements.txt
```

Then set your Anthropic API key in an environment variable. In PowerShell:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

**Each month**

1. Drop new bill PDFs into `inbox/`. Subfolders are fine, for example `inbox/2026-04/`.
2. Run:
   ```bash
   python extract_bills.py inbox/
   ```
3. Open `output/bills.xlsx`. Go to the **Needs Review** sheet and check each flagged bill against its PDF (the `source_file` column). Fix the values in the DB or in Excel, or re-run that bill with `--reprocess --effort high`.
4. Refresh the dashboard. It reads `output/bills.db` or `output/bills.xlsx`.

**Useful options**

| Option | Use |
|---|---|
| `--reprocess` | Re-extract bills already in the DB, for example after a prompt change |
| `--effort low/medium/high` | Trade cost for care. `medium` is the default; use `high` for hard scans |
| `--workers 8` | More parallel requests for big backlogs |
| `--export-only` | Rebuild the Excel file from the DB without calling the API |
| `--db`, `--xlsx` | Choose other output paths |

**Try it with the sample bills**

These five fictional bills cover electric, gas, a combined water/sewer/trash bill with a prior balance and a late fee, a combined electric + gas bill, and a 3-property summary invoice. Keep them in their own database so they don't mix with real data.

```bash
python make_sample_bills.py
```

```bash
python extract_bills.py samples/ --db output_samples/bills.db --xlsx output_samples/bills.xlsx
```

```bash
python check_accuracy.py samples/expected.json --db output_samples/bills.db
```

`python test_offline.py` tests every step except the API call. It needs no key.

---

## 3. Quality control

Automatic checks. A bill that fails any of them goes to **Needs Review**:

- A service address is incomplete, or has no ZIP after the Census lookup and web search
- Current charges are missing, or negative (a credit bill)
- An address has no total cost, or the address totals don't add up to the bill's current charges (within 2¢)
- An address has no charge items, or its charge items don't add up to its total cost (within 2¢)
- An address's service period is missing, or shorter than 15 or longer than 45 days (move-in or move-out partial periods, estimated, catch-up or final bills)
- An address has no usage, usage is negative, or no unit could be printed, guessed or defaulted
- The model says it has low confidence. The model also writes notes about estimated reads, credits and similar items.
- The file could not be read, or the API failed. These show with status `error` and are retried on the next run.

**Suggested human controls**

- **Onboarding a new provider:** hand-key about 10–20 of their bills into an answer key (same format as `samples/expected.json`). Run `check_accuracy.py` against it. Go live once the results match.
- **Monthly:** review every Needs Review row. Also spot-check about 5% of `ok` bills.
- **Anomaly check in the dashboard:** usage or cost more than ±40% month-over-month for the same address and utility. This often means a bad read, a leak, or an extraction error.

---

## 4. Data model (for the dashboard)

**`service_addresses`**: one row per distinct service address. This is the only table with city, state and ZIP.
Columns: `address_id`, `service_address` (as printed the first time it was seen; matching ignores upper/lower case), `city`, `state`, `zip`, `city_source`, `zip_source`, `address_note`, `updated_at`. Each address is looked up once; a row still missing its ZIP is retried on the next bill for that address.

**`bill_sites`**: one row per service address per bill. This is the unit for cost analysis. Each row is identified by **`bill_id` + `address_id`**: a one-address bill has one row; a summary invoice has one row per property. If a bill lists the same address twice (e.g. two meters), the two are merged into one row: costs are added and all charge items and usage are kept.
Columns: `address_id` (links to `service_addresses`), `service_address` (exactly as printed on this bill), `service_period_start` / `service_period_end`, `service_month` (YYYY-MM), `total_cost`.

**`site_charges`**: charge line items. Columns: `bill_id`, `address_id`, `category`, `description` (as printed), `amount`.

**`site_usage`**: metered usage for each address. Columns: `bill_id`, `address_id`, `utility_type`, `meter_number`, `usage_quantity`, `usage_unit` (always filled where possible), `usage_unit_raw` (as printed, empty if none), `unit_source` (`bill`/`guessed`/`default`), `unit_basis`.

**`bills`**: one row per PDF. Columns: `provider_name`, `provider_city` / `provider_state`, `account_number`, `site_count`, `bill_date`, `due_date`, `current_charges`, `total_amount_due`, `status`, `issues`, `confidence`, `notes`, `source_file`, `file_sha256`, `raw_json` (full model output for audit).

**Views for dashboards:** `v_sites` (cost per address per bill, with provider and status), `v_charges` (each charge item with its address, month, provider and bill), and `v_usage` (usage per address). The **Cost by Category** sheet sums `v_charges` by address, month and category, for example to see how sewer costs change over the year. For cost, group `v_sites` by `service_address` and `service_month` and sum `total_cost`. The **Cost by Property-Month** sheet is built this way.

Bills loaded before charge items were added have no items. Run `python extract_bills.py inbox/ --reprocess` once to fill them in.

**Upgrading.** A database in an older layout (city/state/ZIP on `bill_sites`, or rows keyed by a separate `site_id`) is upgraded automatically on the next run. A backup copy is saved first as `bills.before-upgrade.db` (or `-2`, `-3`… if one exists). `address_id` values are kept. The bills are rebuilt from the Claude output already stored in `raw_json`, so no PDF is sent again, and the addresses come from the lookup caches. Older bills keep the address as Claude wrote it then; re-run with `--reprocess` to get it exactly as printed.

The summary sheets (Cost by Property-Month, Cost by Category, Usage Summary) group by `address_id`, so a property printed slightly differently on different bills stays one row.

A database created before this layout (with a `bill_services` table) is refused with a message. Delete it and re-run; the bills are re-extracted from the PDFs.

Common dashboard visuals:

- Cost by property × month, as a stacked bar by provider
- Usage trend per property and utility. Never sum different units; filter by `usage_unit`.
- Cost per unit, for example $/kWh, by provider
- Year-over-year change for the same month
- Open review items count

When the portfolio grows, add a `properties` table that maps `service_address` and `account_number` to a property ID, unit count and square footage. This enables per-unit and per-square-foot benchmarks.

---

## 5. Scaling & operations

- **Cost:** one bill is one API call. Typical 1–3 page bills cost a few cents each at `medium` effort.
- **Large backlogs (thousands of bills):** move the call to the Message Batches API. It costs 50% less and results arrive asynchronously.
- **Automation:** run `extract_bills.py` on a schedule, for example with Windows Task Scheduler or cron. Point it at the folder where the AP inbox or a bill-pay portal saves PDFs.
- **Database:** SQLite is fine up to millions of rows. To share data across a team, point the same schema at Postgres or SQL Server. The SQL is standard.
- **Audit trail:** every row keeps the source file name, file hash, model name, extraction time, and the raw model output.
- **Refusals:** the request turns on Anthropic's server-side fallback (`fallbacks="default"`). If the primary model declines a request, another model completes it inside the same call. Utility bills are very unlikely to trigger this.

## Files

| File | Purpose |
|---|---|
| [extract_bills.py](extract_bills.py) | The pipeline (CLI) |
| [schema.py](schema.py) | Fields the AI must return |
| [make_sample_bills.py](make_sample_bills.py) | Generates fictional test bills and an answer key |
| [check_accuracy.py](check_accuracy.py) | Compares DB results with an answer key |
| [test_offline.py](test_offline.py) | End-to-end test with the API call stubbed out |
