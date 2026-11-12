"""Utility bill extraction pipeline.

    python extract_bills.py inbox/                 # extract new PDFs, update DB + Excel
    python extract_bills.py inbox/ --reprocess     # re-run bills already in the DB
    python extract_bills.py --export-only          # rebuild Excel from the DB

Steps for each PDF:
  1. Fingerprint (SHA-256) -> skip if already loaded (safe to re-run on the same folder).
  2. Send the PDF to Claude with a strict output schema (schema.py): one total cost
     per service address, plus metered usage with units.
  3. Complete addresses: if a service address has no ZIP, look it up with the US Census
     geocoder, using the provider's city/state when the address has none.
  4. Normalize (units, dates, service month) and validate (math, ranges, missing fields).
  5. Write to SQLite (bills.db). Bills failing a check are flagged needs_review.
  6. Export Excel workbook (bills.xlsx) for review, pivots and dashboards.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from schema import BillExtraction, Confidence, Site

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You extract data from utility bills for a property manager's accounting and energy dashboard.

Read the whole bill, including summary boxes, usage tables, per-site detail pages and the remittance stub, then fill in the schema.

Rules:
- A site is a service address: where the utility is delivered (labelled "Service Address", "Premise", "Service Location", or listed per ESI ID). Never use the customer's mailing/billing address as a service address.
- Most bills have one site. Multi-site / summary invoices list several properties under one account: create one site per property with its own period, total cost and usage.
- For each site record ONE total_cost: all new charges for that address this period (every service, tax and fee), excluding previous balance, payments and prior-period late fees. Do not break charges down by service. On a single-site bill total_cost equals current_charges.
- Also list each site's charge line items separately (e.g. water base, water usage, sewer, refuse, sales tax), exactly as printed, each with its category. They must add up to that site's total_cost. On summary invoices use the per-site breakdown if shown (e.g. energy, delivery, taxes); otherwise one item with the site total.
- Usage is consumption for the period (often "Total Usage", or current minus previous read times any multiplier), not a meter reading. Keep the unit exactly as printed. If no unit is printed for a usage, leave usage_unit null and give guessed_unit with guess_basis: look for clues anywhere on the bill (chart labels, rate tables, multipliers, messages), otherwise use the usual unit for that service and provider.
- Copy only what is printed for the service address: if the address shows just a street, leave city/state/zip null. Do not guess them.
- provider_city/provider_state come from the utility's own address on the bill (remit-to or office address).
- Dates in YYYY-MM-DD. If a year is missing, infer it from the bill date.
- If a value is not on the bill, return null. Never guess; lower confidence and explain in notes instead.
"""

UNIT_ALIASES = {
    "kwh": "kWh", "kw-h": "kWh", "kilowatt hours": "kWh", "kilowatt-hours": "kWh",
    "mwh": "MWh",
    "therm": "therms", "therms": "therms", "thm": "therms",
    "ccf": "CCF", "hcf": "CCF", "100 cubic feet": "CCF", "cubic feet x 100": "CCF",
    "mcf": "MCF",
    "cf": "cubic feet", "cu ft": "cubic feet", "cubic feet": "cubic feet",
    "gal": "gallons", "gals": "gallons", "gallon": "gallons", "gallons": "gallons",
    "kgal": "kgal", "1000 gallons": "kgal", "thousand gallons": "kgal",
    "m3": "m3", "cubic meters": "m3",
    "dth": "Dth", "dekatherms": "Dth",
}

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    bill_id            INTEGER PRIMARY KEY,
    file_sha256        TEXT UNIQUE NOT NULL,
    source_file        TEXT NOT NULL,
    provider_name      TEXT,
    provider_city      TEXT,
    provider_state     TEXT,
    account_number     TEXT,
    site_count         INTEGER,
    bill_date          TEXT,
    due_date           TEXT,
    current_charges    REAL,
    total_amount_due   REAL,
    currency           TEXT,
    confidence         TEXT,
    status             TEXT,          -- ok | needs_review | error
    issues             TEXT,          -- JSON list of validation messages
    notes              TEXT,
    model              TEXT,
    raw_json           TEXT,
    extracted_at       TEXT
);
-- One row per distinct service address (as printed). The only place city/state/ZIP are kept,
-- whether printed on the bill or looked up (Census geocoder / web search).
CREATE TABLE IF NOT EXISTS service_addresses (
    address_id      INTEGER PRIMARY KEY,
    service_address TEXT UNIQUE NOT NULL COLLATE NOCASE, -- as printed (first seen); matching ignores case
    city            TEXT,
    state           TEXT,
    zip             TEXT,                 -- 5-digit
    city_source     TEXT,                 -- bill | provider (provider's address) | web (web search)
    zip_source      TEXT,                 -- bill | lookup (Census geocoder) | web | web+census | NULL (not found)
    address_note    TEXT,                 -- how a web-searched address was found (source URL, conflicts)
    updated_at      TEXT
);
-- One row per service address per bill: the unit for cost analysis.
-- Identified by bill + address (a bill lists each address once).
CREATE TABLE IF NOT EXISTS bill_sites (
    bill_id              INTEGER NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    address_id           INTEGER NOT NULL REFERENCES service_addresses(address_id),
    service_address      TEXT,        -- exactly as printed on the bill
    service_period_start TEXT,
    service_period_end   TEXT,
    service_month        TEXT,        -- YYYY-MM, month holding most of the service days
    total_cost           REAL,
    PRIMARY KEY (bill_id, address_id)
);
CREATE TABLE IF NOT EXISTS site_usage (
    usage_id       INTEGER PRIMARY KEY,
    bill_id        INTEGER NOT NULL,
    address_id     INTEGER NOT NULL,
    utility_type   TEXT,
    meter_number   TEXT,
    usage_quantity REAL,
    usage_unit     TEXT,              -- normalized unit (always filled when one can be determined)
    usage_unit_raw TEXT,              -- as printed (NULL if the bill prints none)
    unit_source    TEXT,              -- bill | guessed (clues on the bill) | default (service type)
    unit_basis     TEXT,              -- why a guessed/default unit was chosen
    FOREIGN KEY (bill_id, address_id) REFERENCES bill_sites(bill_id, address_id) ON DELETE CASCADE
);
-- Individual charge line items (water, sewer, trash, tax...), linked to their bill and address.
CREATE TABLE IF NOT EXISTS site_charges (
    charge_id      INTEGER PRIMARY KEY,
    bill_id        INTEGER NOT NULL,
    address_id     INTEGER NOT NULL,
    category       TEXT,              -- electric | natural_gas | water | sewer | trash | stormwater | irrigation | tax | other
    description    TEXT,              -- as printed
    amount         REAL,
    FOREIGN KEY (bill_id, address_id) REFERENCES bill_sites(bill_id, address_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS web_address_cache (
    lookup_key   TEXT PRIMARY KEY,    -- street + printed city/state + provider
    result_json  TEXT,                -- AddressLookup JSON, NULL if nothing usable was found
    looked_up_at TEXT
);
CREATE TABLE IF NOT EXISTS zip_cache (
    lookup_key      TEXT PRIMARY KEY, -- "STREET|CITY|ST"
    zip             TEXT,
    matched_address TEXT,
    looked_up_at    TEXT
);
DROP VIEW IF EXISTS v_sites;
CREATE VIEW v_sites AS
SELECT s.bill_id, s.address_id, s.service_address, s.service_month, s.total_cost, b.provider_name,
       b.account_number, s.service_period_start, s.service_period_end, b.status, b.source_file
FROM bills b JOIN bill_sites s USING (bill_id);
DROP VIEW IF EXISTS v_charges;
CREATE VIEW v_charges AS
SELECT s.address_id, s.service_address, s.service_month, b.provider_name, c.category, c.description, c.amount,
       s.total_cost AS address_total_cost, b.account_number, b.status, b.bill_id, b.source_file
FROM bills b JOIN bill_sites s USING (bill_id) JOIN site_charges c USING (bill_id, address_id);
DROP VIEW IF EXISTS v_usage;
CREATE VIEW v_usage AS
SELECT s.address_id, s.service_address, s.service_month, b.provider_name, u.utility_type, u.meter_number,
       u.usage_quantity, u.usage_unit, u.unit_source, u.unit_basis, u.usage_unit_raw,
       s.service_period_start, s.service_period_end,
       b.status, b.bill_id, b.source_file
FROM bills b JOIN bill_sites s USING (bill_id) JOIN site_usage u USING (bill_id, address_id);
"""


# ---------------------------------------------------------------- extraction

def extract_with_claude(client, pdf_path: Path, model: str, effort: str) -> BillExtraction:
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode()
    # beta endpoint only for server-side refusal fallback: if the model declines, the API
    # re-runs the same request on a fallback model inside the same call.
    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        output_config={"effort": effort},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": f"Extract the bill data. File name: {pdf_path.name}"},
            ],
        }],
        output_format=BillExtraction,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {getattr(response.stop_details, 'explanation', '')}")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"no structured output (stop_reason={response.stop_reason})")
    return response.parsed_output


# ---------------------------------------------------------------- ZIP lookup

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/address"


def census_zip(street: str, city: str, state: str) -> tuple[str | None, str | None]:
    """Ask the US Census geocoder (free, no key) for the ZIP of a street address."""
    query = urllib.parse.urlencode({"street": street, "city": city, "state": state,
                                    "benchmark": "Public_AR_Current", "format": "json"})
    with urllib.request.urlopen(f"{CENSUS_URL}?{query}", timeout=30) as resp:
        matches = json.load(resp)["result"]["addressMatches"]
    zips = {m["addressComponents"]["zip"] for m in matches}
    if len(zips) == 1:            # one unambiguous answer
        return zips.pop(), matches[0]["matchedAddress"]
    return None, None


def lookup_zip(conn: sqlite3.Connection, street: str, city: str, state: str) -> str | None:
    key = f"{street.upper()}|{city.upper()}|{state.upper()}"
    row = conn.execute("SELECT zip FROM zip_cache WHERE lookup_key=?", (key,)).fetchone()
    if row:
        return row[0]
    try:
        zip5, matched = census_zip(street, city, state)
    except Exception as exc:     # network problem: don't cache, try again next run
        print(f"    ZIP lookup failed for {street}, {city} {state}: {exc}")
        return None
    conn.execute("INSERT OR REPLACE INTO zip_cache VALUES (?,?,?,?)",
                 (key, zip5, matched, datetime.now().isoformat(timespec="seconds")))
    return zip5


class AddressLookup(BaseModel):
    city: str | None
    state: str | None
    zip: str | None
    confidence: Confidence
    source: str | None = None


WEB_LOOKUP_PROMPT = """Find the city, 2-letter state and 5-digit ZIP code of this US service address.

Street: {street}
Clues from the utility bill (may be incomplete):
- City/state printed with the address: {printed}
- Utility provider: {provider}, located in {provider_loc}

Use web search (county appraisal district / property records, real-estate listings, USPS, maps). The street
must match exactly; prefer a city in the provider's service area. If several places have this street, pick
the one consistent with the provider and lower your confidence.

Finish with ONLY a JSON object on the last line, no code fence:
{{"city": "...", "state": "TX", "zip": "12345", "confidence": "high|medium|low", "source": "<URL you relied on>"}}
Use null for anything you could not find."""


def web_lookup_address(client, street: str, site: Site, bill: BillExtraction,
                       model: str = DEFAULT_MODEL) -> AddressLookup | None:
    """Ask Claude to find city/state/ZIP with web search. Returns None if nothing usable."""
    prompt = WEB_LOOKUP_PROMPT.format(
        street=street,
        printed=", ".join(p for p in [site.city, site.state] if p) or "none",
        provider=bill.provider_name,
        provider_loc=", ".join(p for p in [bill.provider_city, bill.provider_state] if p) or "unknown",
    )
    messages = [{"role": "user", "content": prompt}]
    for _ in range(3):   # server-side search loop may pause; resend to let it continue
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
            messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": response.content}]
    if response.stop_reason in ("refusal", "pause_turn"):
        return None
    text = "".join(b.text for b in response.content if b.type == "text")
    candidates = re.findall(r"\{[^{}]*\}", text)
    if not candidates:
        return None
    try:
        found = AddressLookup.model_validate_json(candidates[-1])
    except ValueError:
        return None
    m = re.match(r"\s*(\d{5})", found.zip or "")
    found.zip = m.group(1) if m else None
    found.state = found.state.strip().upper()[:2] if found.state else None
    return found


def cached_web_lookup(conn, client, street, site, bill) -> AddressLookup | None:
    key = "|".join(str(x).upper() for x in [street, site.city, site.state, bill.provider_name,
                                             bill.provider_city, bill.provider_state])
    row = conn.execute("SELECT result_json FROM web_address_cache WHERE lookup_key=?", (key,)).fetchone()
    if row:
        return AddressLookup.model_validate_json(row[0]) if row[0] else None
    if client is None:           # offline / migration: only cached answers
        return None
    try:
        found = web_lookup_address(client, street, site, bill)
    except Exception as exc:     # API problem: don't cache, try again next run
        print(f"    web address lookup failed for {street}: {exc}")
        return None
    conn.execute("INSERT OR REPLACE INTO web_address_cache VALUES (?,?,?)",
                 (key, found.model_dump_json() if found else None, datetime.now().isoformat(timespec="seconds")))
    return found


def resolve_site(conn: sqlite3.Connection, site: Site, bill: BillExtraction, client=None) -> dict:
    """Complete the service address. Order, stopping at the first ZIP found:

    1. ZIP printed on the bill.
    2. US Census geocoder, with the printed city/state or else the provider's city/state.
    3. Web search by Claude (city, state and ZIP), then re-checked with the Census geocoder.
    """
    street = " ".join(site.street.split())
    city, state, city_source = site.city, site.state, "bill"
    if not (city and state) and bill.provider_city and bill.provider_state:
        city, state, city_source = bill.provider_city, bill.provider_state, "provider"
    zip5, zip_source, note = None, None, None
    m = re.match(r"\s*(\d{5})", site.zip or "")
    if m:
        zip5, zip_source = m.group(1), "bill"
    elif city and state:
        zip5 = lookup_zip(conn, street, city, state)
        zip_source = "lookup" if zip5 else None

    if not zip5:
        found = cached_web_lookup(conn, client, street, site, bill)
        if found and found.zip and found.confidence.value != "low":
            city, state, zip5 = found.city or city, found.state or state, found.zip
            city_source = "web" if found.city else city_source
            # Cross-check: if the Census knows this street in the city the web search found, trust its ZIP.
            census = lookup_zip(conn, street, city, state) if city and state else None
            if census and census != zip5:
                note = f"web search said {zip5}, Census geocoder says {census}; used Census"
                zip5, zip_source = census, "web+census"
            else:
                zip_source = "web+census" if census else "web"
                note = f"web search ({found.confidence.value} confidence): {found.source or 'no source given'}"
        elif found:
            note = f"web search found no confident match (best guess: {found.city}, {found.state} {found.zip})"

    city = city.title() if city and city.isupper() else city
    state = state.upper() if state else None
    tail = " ".join(p for p in [state, zip5] if p)
    address = ", ".join(p for p in [street, city, tail] if p)
    return {"street": street, "city": city, "state": state, "zip": zip5, "city_source": city_source if city else None,
            "zip_source": zip_source, "address_note": note, "full_address": address}


def printed_address(site: Site) -> str:
    return " ".join((site.address_as_printed or site.street).split())


def merge_same_address(sites: list[Site]) -> list[Site]:
    """Rows are keyed by bill + address, so combine any sites on one bill that share an address
    (e.g. two meters listed separately): costs add up, charges and usage are kept, period widened."""
    merged: dict[str, Site] = {}
    for s in sites:
        key = printed_address(s).casefold()
        if key not in merged:
            merged[key] = s.model_copy(deep=True)
            continue
        m = merged[key]
        m.total_cost = (None if m.total_cost is None and s.total_cost is None
                        else (m.total_cost or 0) + (s.total_cost or 0))
        m.charges += s.charges
        m.usage += s.usage
        starts = [d for d in (m.service_period_start, s.service_period_start) if d]
        ends = [d for d in (m.service_period_end, s.service_period_end) if d]
        m.service_period_start = min(starts) if starts else None
        m.service_period_end = max(ends) if ends else None
    return list(merged.values())


def upsert_address(conn: sqlite3.Connection, site: Site, bill: BillExtraction, client=None) -> dict:
    """Find or create the service_addresses row for this printed address.

    City/state/ZIP are looked up once per printed address; later bills for the same
    address reuse the row. A row still missing its ZIP is retried.
    """
    printed = printed_address(site)
    row = conn.execute("SELECT * FROM service_addresses WHERE service_address=?", (printed,)).fetchone()
    cols = [d[0] for d in conn.execute("SELECT * FROM service_addresses LIMIT 0").description]
    if row and row[cols.index("zip")]:
        return dict(zip(cols, row))
    r = resolve_site(conn, site, bill, client)
    now = datetime.now().isoformat(timespec="seconds")
    if row:
        conn.execute("""UPDATE service_addresses SET city=?, state=?, zip=?, city_source=?, zip_source=?,
                            address_note=?, updated_at=? WHERE service_address=?""",
                     (r["city"], r["state"], r["zip"], r["city_source"], r["zip_source"], r["address_note"], now, printed))
    else:
        conn.execute("""INSERT INTO service_addresses (service_address, city, state, zip, city_source, zip_source,
                            address_note, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                     (printed, r["city"], r["state"], r["zip"], r["city_source"], r["zip_source"], r["address_note"], now))
    row = conn.execute("SELECT * FROM service_addresses WHERE service_address=?", (printed,)).fetchone()
    return dict(zip(cols, row))


# ---------------------------------------------------------------- normalize + validate

def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def service_month(start: date | None, end: date | None, bill_date: date | None) -> str | None:
    """Month that contains the most days of the service period (YYYY-MM).

    A 15 Mar - 14 Apr bill counts as March (17 days) rather than April (14 days).
    Ties go to the later month. Falls back to the month before the bill date.
    """
    if start and end and end >= start:
        counts: dict[str, int] = {}
        d = start
        while d <= end:
            key = d.strftime("%Y-%m")
            counts[key] = counts.get(key, 0) + 1
            d += timedelta(days=1)
        return max(sorted(counts), key=lambda k: counts[k])
    if end:
        return end.strftime("%Y-%m")
    if bill_date:
        prev = bill_date.replace(day=1) - timedelta(days=1)
        return prev.strftime("%Y-%m")
    return None


def normalize_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    key = unit.strip().lower().rstrip(".")
    return UNIT_ALIASES.get(key, unit.strip())


# Last-resort unit when the bill prints none and Claude found no clue (US conventions).
DEFAULT_UNITS = {
    "electric": "kWh", "natural_gas": "therms", "water": "gallons", "sewer": "gallons",
    "irrigation": "gallons", "propane": "gallons", "fuel_oil": "gallons", "steam": "lbs",
}


def resolve_unit(u) -> tuple[str | None, str | None, str | None]:
    """Return (unit, source, basis): printed unit, else Claude's guess from the bill, else service default."""
    if u.usage_unit:
        return normalize_unit(u.usage_unit), "bill", None
    if u.guessed_unit:
        return normalize_unit(u.guessed_unit), "guessed", u.guess_basis
    default = DEFAULT_UNITS.get(u.utility_type.value)
    if default:
        return default, "default", f"usual unit for {u.utility_type.value.replace('_', ' ')}"
    return None, None, None


def validate(bill: BillExtraction, resolved: list[dict]) -> list[str]:
    issues: list[str] = []
    if bill.current_charges is None:
        issues.append("current charges missing")
    elif bill.current_charges < 0:
        issues.append("current charges negative (credit bill)")
    if not bill.sites:
        issues.append("no service address found")

    for site, r in zip(bill.sites, resolved):
        where = printed_address(site) or "(no address)"   # as printed
        if len(site.street) < 5:
            issues.append(f"{where}: street address incomplete")
        if not r["zip"]:
            issues.append(f"{where}: ZIP not printed and not found by Census lookup or web search")
        if site.total_cost is None:
            issues.append(f"{where}: total cost missing")
        start, end = parse_date(site.service_period_start), parse_date(site.service_period_end)
        if not (start and end):
            issues.append(f"{where}: service period missing")
        elif not 15 <= (end - start).days + 1 <= 45:
            issues.append(f"{where}: unusual service period length {(end - start).days + 1} days")
        if not site.charges:
            issues.append(f"{where}: no charge items found")
        elif site.total_cost is not None and abs(sum(c.amount for c in site.charges) - site.total_cost) > 0.02:
            issues.append(f"{where}: charge items ({sum(c.amount for c in site.charges):,.2f}) "
                          f"don't add up to total cost ({site.total_cost:,.2f})")
        if not site.usage:
            issues.append(f"{where}: no usage found")
        for u in site.usage:
            if u.usage_quantity < 0:
                issues.append(f"{where}: negative {u.utility_type.value} usage")
            if resolve_unit(u)[0] is None:   # guessed/default units are filled in, not flagged
                issues.append(f"{where}: {u.utility_type.value} usage has no unit and none could be inferred")

    totals = [s.total_cost for s in bill.sites]
    if bill.current_charges is not None and totals and None not in totals:
        if abs(sum(totals) - bill.current_charges) > 0.02:
            issues.append(f"site totals ({sum(totals):,.2f}) don't add up to current charges ({bill.current_charges:,.2f})")
    if bill.confidence.value == "low":
        issues.append("model reported low confidence")
    return issues


# ---------------------------------------------------------------- storage

def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bill_services'").fetchone():
        sys.exit(f"{db_path} uses the old per-service layout. Delete it (or use --db for a new file) "
                 "and re-run; bills are re-extracted from the PDFs.")
    # Add columns introduced after a database was created (bills keep their data).
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='site_usage'").fetchone():
        have = {r[1] for r in conn.execute("PRAGMA table_info(site_usage)")}
        for col in ("unit_source", "unit_basis"):
            if col not in have:
                conn.execute(f"ALTER TABLE site_usage ADD COLUMN {col} TEXT")
    migrate = False
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bill_sites'").fetchone():
        have = {r[1] for r in conn.execute("PRAGMA table_info(bill_sites)")}
        # Older layouts: city/state/ZIP on bill_sites, or rows keyed by a separate site_id.
        migrate = "zip" in have or "site_id" in have
    if migrate:
        rebuild_from_raw(conn, db_path)
    else:
        conn.executescript(DB_SCHEMA)
    return conn


def _upgrade_raw(raw: dict) -> dict:
    """Fill fields that older extractions didn't have, so they validate against today's schema."""
    for s in raw.get("sites", []):
        if not s.get("address_as_printed"):
            tail = " ".join(p for p in [s.get("state"), s.get("zip")] if p)
            s["address_as_printed"] = ", ".join(p for p in [s.get("street"), s.get("city"), tail] if p)
        s.setdefault("charges", [])
        for u in s.get("usage", []):
            u.setdefault("guessed_unit", None)
            u.setdefault("guess_basis", None)
    return raw


def rebuild_from_raw(conn: sqlite3.Connection, db_path: Path) -> None:
    """Upgrade an older database to the current layout (service_addresses table; bill rows
    keyed by bill_id + address_id).

    Bills are rebuilt from the model output already stored in bills.raw_json, so no PDF is
    sent to Claude again. The service_addresses table and the Census/web caches are kept,
    so address_id values and looked-up ZIPs stay the same.
    """
    n = 1
    backup = db_path.with_name(f"{db_path.stem}.before-upgrade{db_path.suffix}")
    while backup.exists():          # keep earlier backups
        n += 1
        backup = db_path.with_name(f"{db_path.stem}.before-upgrade-{n}{db_path.suffix}")
    conn.commit()
    with sqlite3.connect(backup) as b:
        conn.backup(b)
    print(f"Upgrading {db_path} to the current layout (backup: {backup})")
    old = conn.execute("SELECT file_sha256, source_file, raw_json, model, issues FROM bills").fetchall()
    conn.execute("PRAGMA foreign_keys = OFF")
    for t in ("v_sites", "v_charges", "v_usage"):
        conn.execute(f"DROP VIEW IF EXISTS {t}")
    for t in ("site_usage", "site_charges", "bill_sites", "bills"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(DB_SCHEMA)
    for digest, source_file, raw_json, model, issues in old:
        if raw_json:
            bill = BillExtraction.model_validate(_upgrade_raw(json.loads(raw_json)))
            save(conn, Path(source_file), digest, bill, model)
        else:
            save(conn, Path(source_file), digest, None, model, error=(json.loads(issues or "[]") or ["error"])[0])
    print(f"  {len(old)} bills moved; {conn.execute('SELECT COUNT(*) FROM service_addresses').fetchone()[0]} addresses")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(conn: sqlite3.Connection, pdf: Path, digest: str, bill: BillExtraction | None,
         model: str, error: str | None = None, client=None) -> str:
    """Store one bill. `client` enables the web-search address lookup (None = Census only)."""
    conn.execute("DELETE FROM bills WHERE file_sha256 = ?", (digest,))
    now = datetime.now().isoformat(timespec="seconds")
    if bill is None:
        conn.execute(
            "INSERT INTO bills (file_sha256, source_file, status, issues, model, extracted_at) VALUES (?,?,?,?,?,?)",
            (digest, pdf.name, "error", json.dumps([error]), model, now),
        )
        conn.commit()
        return "error"

    # A single-site bill's site total is the bill's current charges.
    if len(bill.sites) == 1 and bill.sites[0].total_cost is None:
        bill.sites[0].total_cost = bill.current_charges

    bill.sites = merge_same_address(bill.sites)
    resolved = [upsert_address(conn, s, bill, client) for s in bill.sites]
    issues = validate(bill, resolved)
    status = "needs_review" if issues else "ok"
    cur = conn.execute(
        """INSERT INTO bills (file_sha256, source_file, provider_name, provider_city, provider_state,
               account_number, site_count, bill_date, due_date, current_charges, total_amount_due,
               currency, confidence, status, issues, notes, model, raw_json, extracted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (digest, pdf.name, bill.provider_name, bill.provider_city, bill.provider_state,
         bill.account_number, len(bill.sites), bill.bill_date, bill.due_date,
         bill.current_charges, bill.total_amount_due, bill.currency, bill.confidence.value,
         status, json.dumps(issues), bill.notes, model, bill.model_dump_json(), now),
    )
    bill_id = cur.lastrowid
    for site, r in zip(bill.sites, resolved):
        address_id = r["address_id"]
        conn.execute(
            """INSERT INTO bill_sites (bill_id, address_id, service_address, service_period_start,
                   service_period_end, service_month, total_cost)
               VALUES (?,?,?,?,?,?,?)""",
            (bill_id, address_id, printed_address(site), site.service_period_start, site.service_period_end,
             service_month(parse_date(site.service_period_start), parse_date(site.service_period_end),
                           parse_date(bill.bill_date)),
             site.total_cost),
        )
        for ch in site.charges:
            conn.execute(
                "INSERT INTO site_charges (bill_id, address_id, category, description, amount) VALUES (?,?,?,?,?)",
                (bill_id, address_id, ch.category.value, ch.description, ch.amount),
            )
        for u in site.usage:
            unit, unit_source, unit_basis = resolve_unit(u)
            conn.execute(
                """INSERT INTO site_usage (bill_id, address_id, utility_type, meter_number, usage_quantity,
                       usage_unit, usage_unit_raw, unit_source, unit_basis) VALUES (?,?,?,?,?,?,?,?,?)""",
                (bill_id, address_id, u.utility_type.value, u.meter_number, u.usage_quantity,
                 unit, u.usage_unit, unit_source, unit_basis),
            )
    conn.commit()
    return status


# ---------------------------------------------------------------- Excel export

def export_excel(conn: sqlite3.Connection, xlsx_path: Path) -> None:
    import pandas as pd
    from openpyxl.styles import Font

    sites = pd.read_sql_query(
        """SELECT service_address, service_month, total_cost, provider_name, account_number,
                  service_period_start, service_period_end, status, address_id, bill_id, source_file
           FROM v_sites ORDER BY service_address, service_month, provider_name""", conn)
    addresses = pd.read_sql_query(
        """SELECT address_id, service_address, city, state, zip, city_source, zip_source, address_note
           FROM service_addresses ORDER BY service_address""", conn)
    usage = pd.read_sql_query(
        """SELECT service_address, service_month, provider_name, utility_type, usage_quantity,
                  usage_unit, unit_source, unit_basis, usage_unit_raw, meter_number,
                  service_period_start, service_period_end,
                  status, address_id, bill_id, source_file
           FROM v_usage ORDER BY service_address, service_month, utility_type""", conn)
    charges = pd.read_sql_query(
        """SELECT service_address, service_month, provider_name, category, description, amount,
                  address_total_cost, account_number, status, address_id, bill_id, source_file
           FROM v_charges ORDER BY service_address, service_month, bill_id, category""", conn)
    bills = pd.read_sql_query(
        """SELECT b.bill_id,
                  -- service address(es) as printed on the bill; a summary invoice lists each, separated by "; "
                  (SELECT group_concat(service_address, '; ')
                     FROM (SELECT DISTINCT s.service_address FROM bill_sites s
                           WHERE s.bill_id = b.bill_id ORDER BY s.rowid)) AS service_address,
                  b.provider_name, b.account_number, b.site_count, b.bill_date, b.due_date,
                  b.current_charges, b.total_amount_due, b.currency, b.status, b.confidence, b.issues,
                  b.notes, b.source_file
           FROM bills b ORDER BY b.provider_name, b.bill_date""", conn)
    review = bills[bills["status"] != "ok"]

    # Summaries group by address_id, so one property printed differently on different bills
    # (e.g. "4152 EMERSON AVE" vs "4152 Emerson Ave") is still one row. Label = service_addresses entry.
    label = dict(zip(addresses["address_id"], addresses["service_address"]))
    def by_property(df):
        out = df.drop(columns=["service_address"]).copy()
        out.insert(0, "service_address", out["address_id"].map(label))
        return out
    cost_pivot = (by_property(sites).pivot_table(index=["address_id", "service_address"], columns="service_month",
                                                 values="total_cost", aggfunc="sum", fill_value=0)
                  if not sites.empty else pd.DataFrame())
    category_pivot = (by_property(charges).pivot_table(index=["address_id", "service_address", "service_month"],
                                                       columns="category", values="amount", aggfunc="sum",
                                                       fill_value=0).reset_index()
                      if not charges.empty else pd.DataFrame())
    usage_summary = (by_property(usage).groupby(["address_id", "service_address", "utility_type", "usage_unit",
                                                 "service_month"], dropna=False)["usage_quantity"].sum().reset_index()
                     if not usage.empty else usage)

    # Keep ID columns as integers even if a join leaves gaps (pandas would otherwise make them float).
    for df in (sites, addresses, usage, charges, bills, review, category_pivot, usage_summary):
        for col in ("address_id", "bill_id"):
            if col in getattr(df, "columns", []):
                df[col] = df[col].astype("Int64")

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as xl:
        sites.to_excel(xl, sheet_name="Cost by Address", index=False)
        addresses.to_excel(xl, sheet_name="Service Addresses", index=False)
        charges.to_excel(xl, sheet_name="Charge Items", index=False)
        category_pivot.to_excel(xl, sheet_name="Cost by Category", index=False)
        usage.to_excel(xl, sheet_name="Usage", index=False)
        cost_pivot.to_excel(xl, sheet_name="Cost by Property-Month")
        usage_summary.to_excel(xl, sheet_name="Usage Summary", index=False)
        bills.to_excel(xl, sheet_name="Bills", index=False)
        review.to_excel(xl, sheet_name="Needs Review", index=False)

        money_cols = {"total_cost", "current_charges", "total_amount_due", "amount", "address_total_cost"}
        money_sheets = {"Cost by Property-Month", "Cost by Category"}
        id_cols = {"address_id", "bill_id"}
        for ws in xl.book.worksheets:
            ws.freeze_panes = "B2" if ws.title in money_sheets else "A2"
            if ws.max_row > 1:
                ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                header = str(col[0].value or "")
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
                if header in id_cols:             # IDs are whole numbers, never "1.00"
                    for c in col[1:]:
                        if isinstance(c.value, float) and c.value.is_integer():
                            c.value = int(c.value)
                        c.number_format = "0"
                elif header in money_cols or ws.title in money_sheets:
                    for c in col[1:]:
                        if isinstance(c.value, (int, float)):
                            c.number_format = "#,##0.00"
            for c in ws[1]:
                c.font = Font(bold=True)


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract utility bill PDFs into SQLite + Excel")
    ap.add_argument("inputs", nargs="*", type=Path, help="PDF files or folders")
    ap.add_argument("--db", type=Path, default=Path("output/bills.db"))
    ap.add_argument("--xlsx", type=Path, default=Path("output/bills.xlsx"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--workers", type=int, default=4, help="parallel API requests")
    ap.add_argument("--reprocess", action="store_true", help="re-extract PDFs already in the DB")
    ap.add_argument("--export-only", action="store_true")
    args = ap.parse_args(argv)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.xlsx.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)

    if not args.export_only:
        pdfs: list[Path] = []
        for p in args.inputs:
            pdfs += sorted(p.rglob("*.pdf")) if p.is_dir() else [p]
        known = {r[0] for r in conn.execute("SELECT file_sha256 FROM bills WHERE status != 'error'")}
        todo = [(p, sha256(p)) for p in pdfs]
        todo = [(p, h) for p, h in todo if args.reprocess or h not in known]
        print(f"{len(pdfs)} PDFs found, {len(todo)} to extract")

        if todo:
            import anthropic
            client = anthropic.Anthropic(max_retries=5)
            tally: dict[str, int] = {}
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(extract_with_claude, client, p, args.model, args.effort): (p, h)
                           for p, h in todo}
                for fut in as_completed(futures):     # results are saved one at a time, on this thread
                    pdf, digest = futures[fut]
                    try:
                        status = save(conn, pdf, digest, fut.result(), args.model, client=client)
                    except Exception as exc:  # keep going; failed files land in Needs Review
                        status = save(conn, pdf, digest, None, args.model, error=f"{type(exc).__name__}: {exc}")
                    tally[status] = tally.get(status, 0) + 1
                    print(f"  [{status:>12}] {pdf.name}")
            print("Done:", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    print(f"Database: {args.db}")
    try:
        export_excel(conn, args.xlsx)
    except PermissionError:
        print(f"Could not write {args.xlsx}: it is probably open in Excel. The database is up to date; "
              f"close the workbook and run:  python extract_bills.py --export-only")
        return 2
    print(f"Workbook: {args.xlsx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
