"""Exercise everything except the Claude call: dedupe, ZIP completion, validation, SQLite, Excel.

Claude's answer is replaced with the answer key from samples/expected.json, plus one
deliberately broken file to prove the review queue catches it. The Census ZIP lookup
is replaced with a stub so the test runs offline.
"""
import json
import shutil
import sqlite3
from pathlib import Path

import extract_bills as eb
from schema import BillExtraction

KEY = json.loads(Path("samples/expected.json").read_text())
FAKE_ZIPS = {("12 Oak St", "Fate", "TX"): "75189"}


def fake_extract(client, pdf, model, effort):
    if pdf.name.startswith("broken"):
        raise RuntimeError("simulated unreadable scan")
    return BillExtraction.model_validate(KEY[pdf.name])


eb.extract_with_claude = fake_extract
eb.census_zip = lambda street, city, state: (FAKE_ZIPS.get((street, city, state)), None)
import anthropic
anthropic.Anthropic = lambda **kw: None

# 1. Full pipeline on the samples
out = Path("output_test"); shutil.rmtree(out, ignore_errors=True)
inbox = out / "inbox"; inbox.mkdir(parents=True)
for p in Path("samples").glob("*.pdf"):
    shutil.copy(p, inbox / p.name)
(inbox / "broken_scan.pdf").write_bytes(b"%PDF-1.4 not really")
args = ["output_test/inbox", "--db", "output_test/bills.db", "--xlsx", "output_test/bills.xlsx"]
eb.main(args)
print("--- second run (should skip everything except the failed file) ---")
eb.main(args)

# 2. ZIP completion rules
conn = eb.connect(out / "zip_test.db")
base = {"provider_name": "City of Fate", "provider_city": "Fate", "provider_state": "TX", "account_number": None,
        "bill_date": "2026-10-20", "due_date": None, "current_charges": 10.0, "total_amount_due": 10.0,
        "currency": "USD", "confidence": "high", "notes": None}
site = {"address_as_printed": "x", "service_period_start": "2026-09-15", "service_period_end": "2026-10-15", "total_cost": 10.0,
        "charges": [{"category": "water", "description": "Water", "amount": 10.0}],
        "usage": [{"utility_type": "water", "meter_number": None, "usage_quantity": 1, "usage_unit": "gal",
                   "guessed_unit": None, "guess_basis": None}]}
cases = [
    ({"street": "12 Oak St", "city": None, "state": None, "zip": None}, "12 Oak St, Fate, TX 75189", "provider", "lookup"),
    ({"street": "9 Elm St", "city": "Plano", "state": "TX", "zip": "75075-1948"}, "9 Elm St, Plano, TX 75075", "bill", "bill"),
    ({"street": "1 Nowhere Rd", "city": None, "state": None, "zip": None}, "1 Nowhere Rd, Fate, TX", "provider", None),
]
for s, want_addr, want_city_src, want_zip_src in cases:
    bill = BillExtraction.model_validate({**base, "sites": [{**s, **site}]})
    r = eb.resolve_site(conn, bill.sites[0], bill)
    assert (r["full_address"], r["city_source"], r["zip_source"]) == (want_addr, want_city_src, want_zip_src), r
    print(f"ZIP rule ok: {s['street']!r:16} -> {r['full_address']!r} (city from {r['city_source']}, zip from {r['zip_source']})")
issues = eb.validate(bill, [r])
assert any("ZIP not printed" in i for i in issues), issues
print("Unmatched ZIP is flagged for review")

# 3. Unit fill-in rules: printed > Claude's guess from the bill > service-type default
from schema import Usage
unit_cases = [
    ({"utility_type": "water", "usage_unit": "Gal.", "guessed_unit": None, "guess_basis": None}, ("gallons", "bill")),
    ({"utility_type": "water", "usage_unit": None, "guessed_unit": "kgal", "guess_basis": "rate per 1,000 gal"}, ("kgal", "guessed")),
    ({"utility_type": "natural_gas", "usage_unit": None, "guessed_unit": None, "guess_basis": None}, ("therms", "default")),
    ({"utility_type": "other", "usage_unit": None, "guessed_unit": None, "guess_basis": None}, (None, None)),
]
for u, want in unit_cases:
    got = eb.resolve_unit(Usage.model_validate({"meter_number": None, "usage_quantity": 5, **u}))
    assert got[:2] == want, (u, got)
    print(f"Unit rule ok: {u['utility_type']:12} printed={u['usage_unit']!s:5} guess={u['guessed_unit']!s:5} -> {got[0]} ({got[1]})")

# 4. Charge items are stored per bill + address and must add up to the address total
db = sqlite3.connect(out / "bills.db")
rows = db.execute("""SELECT c.description, c.amount, s.total_cost FROM site_charges c
                     JOIN bill_sites s USING (bill_id, address_id) JOIN bills b ON b.bill_id = c.bill_id
                     WHERE b.source_file LIKE 'city_of_springfield%'""").fetchall()
assert len(rows) == 5 and abs(sum(r[1] for r in rows) - rows[0][2]) < 0.005, rows
print(f"Water bill stored as {len(rows)} linked charge items: " + ", ".join(f"{d} {a:.2f}" for d, a, _ in rows))
bad = BillExtraction.model_validate({**base, "sites": [{**cases[0][0], **site,
        "charges": [{"category": "water", "description": "Water", "amount": 7.0}]}]})
assert any("don't add up to total cost" in i for i in eb.validate(bad, [eb.resolve_site(conn, bad.sites[0], bad)]))
print("Charge items that don't add up are flagged for review")

# 5. Web-search fallback: used only when the Census lookup finds nothing; re-checked by Census
from extract_bills import AddressLookup
FAKE_WEB = {
    "77 Hidden Ln": AddressLookup(city="Heath", state="TX", zip="75032", confidence="high", source="https://example.test/a"),
    "5 Conflict Ct": AddressLookup(city="Rockwall", state="TX", zip="75087", confidence="medium", source="https://example.test/b"),
    "9 Vague Way": AddressLookup(city="Fate", state="TX", zip="75189", confidence="low", source=None),
}
FAKE_ZIPS.update({("77 Hidden Ln", "Heath", "TX"): "75032", ("5 Conflict Ct", "Rockwall", "TX"): "75032"})
calls = []
def fake_web(client, street, site, bill, model=None):
    calls.append(street)
    return FAKE_WEB.get(street)
eb.web_lookup_address = fake_web
web_cases = [  # street, expected address, city_source, zip_source
    ("77 Hidden Ln", "77 Hidden Ln, Heath, TX 75032", "web", "web+census"),     # web found, Census agrees
    ("5 Conflict Ct", "5 Conflict Ct, Rockwall, TX 75032", "web", "web+census"),  # Census overrides web ZIP
    ("9 Vague Way", "9 Vague Way, Fate, TX", "provider", None),                 # low confidence -> not used
    ("12 Oak St", "12 Oak St, Fate, TX 75189", "provider", "lookup"),          # Census hit first -> no web call
]
for street, want_addr, want_city_src, want_zip_src in web_cases:
    bill = BillExtraction.model_validate({**base, "sites": [{"street": street, "city": None, "state": None, "zip": None, **site}]})
    r = eb.resolve_site(conn, bill.sites[0], bill, client=object())
    assert (r["full_address"], r["city_source"], r["zip_source"]) == (want_addr, want_city_src, want_zip_src), r
    print(f"Web rule ok: {street!r:16} -> {r['full_address']!r} (city {r['city_source']}, zip {r['zip_source']}) {r['address_note'] or ''}")
assert "12 Oak St" not in calls, "web search should not run when Census already found the ZIP"
eb.resolve_site(conn, bill.sites[0], bill, client=object())
r = eb.resolve_site(conn, BillExtraction.model_validate({**base, "sites": [{"street": "77 Hidden Ln", "city": None, "state": None, "zip": None, **site}]}).sites[0],
                    BillExtraction.model_validate({**base, "sites": [{"street": "77 Hidden Ln", "city": None, "state": None, "zip": None, **site}]}), client=object())
assert calls.count("77 Hidden Ln") == 1, calls
print("Web lookups are cached (one search per address)")

# 6. Address table: city/state/ZIP live only in service_addresses; bill tables keep the printed address
db = sqlite3.connect(out / "bills.db")
bill_cols = {r[1] for r in db.execute("PRAGMA table_info(bill_sites)")}
assert not bill_cols & {"city", "state", "zip"}, bill_cols
rows = db.execute("""SELECT s.service_address, a.city, a.state, a.zip FROM bill_sites s
                     JOIN service_addresses a USING (address_id)""").fetchall()
assert rows and all(r[3] for r in rows), rows
print(f"{len(rows)} bill addresses linked to {db.execute('SELECT COUNT(*) FROM service_addresses').fetchone()[0]} service_addresses rows, e.g. {rows[0]}")

# 7. ID columns in Excel are whole numbers with an integer format on every sheet
import openpyxl
eb.export_excel(eb.connect(out / "bills.db"), out / "bills.xlsx")
wb = openpyxl.load_workbook(out / "bills.xlsx")
checked = 0
for ws in wb.worksheets:
    headers = [c.value for c in ws[1]]
    for name in ("address_id", "bill_id"):
        if name in headers:
            i = headers.index(name) + 1
            for r in range(2, ws.max_row + 1):
                cell = ws.cell(r, i)
                if cell.value is None:
                    continue
                assert isinstance(cell.value, int) and cell.number_format == "0", (ws.title, name, cell.value, cell.number_format)
                checked += 1
print(f"{checked} ID cells checked: all integers shown without decimals")

# 8. Bills sheet shows each bill's service address(es) as printed; summary invoices list all of them
import pandas as pd
bills_sheet = pd.read_excel(out / "bills.xlsx", sheet_name="Bills")
assert list(bills_sheet.columns[:2]) == ["bill_id", "service_address"], list(bills_sheet.columns)
multi = bills_sheet.loc[bills_sheet.source_file.str.startswith("lonestar"), "service_address"].iloc[0]
assert multi.count("; ") == 2, multi
broken = bills_sheet.loc[bills_sheet.source_file.str.startswith("broken"), "service_address"]
assert broken.isna().all(), broken
print(f"Bills sheet has service_address; summary invoice shows: {multi}")

# 9. No site_id: bill rows are keyed by bill_id + address_id; a summary invoice has one row per address
db = sqlite3.connect(out / "bills.db")
for t in ("bill_sites", "site_charges", "site_usage"):
    cols = {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
    assert "site_id" not in cols and {"bill_id", "address_id"} <= cols, (t, cols)
pk = [r[1] for r in db.execute("PRAGMA table_info(bill_sites)") if r[5]]
assert pk == ["bill_id", "address_id"], pk
multi = db.execute("""SELECT b.bill_id, COUNT(*) FROM bills b JOIN bill_sites s USING (bill_id)
                      WHERE b.source_file LIKE 'lonestar%' GROUP BY 1""").fetchone()
assert multi[1] == 3, multi
orphans = db.execute("""SELECT COUNT(*) FROM site_charges c LEFT JOIN bill_sites s USING (bill_id, address_id)
                        WHERE s.bill_id IS NULL""").fetchone()[0]
assert orphans == 0
print(f"Keys: bill_sites primary key is (bill_id, address_id); summary invoice bill {multi[0]} has {multi[1]} address rows")

# 10. Two sites on one bill with the same address are merged into one row
from schema import Site
a = Site.model_validate({**site, "address_as_printed": "10 Same St", "street": "10 Same St", "city": "Fate",
                         "state": "TX", "zip": "75189", "total_cost": 4.0,
                         "charges": [{"category": "water", "description": "Meter A", "amount": 4.0}]})
b = a.model_copy(update={"address_as_printed": "10 SAME ST", "total_cost": 6.0,
                         "charges": [a.charges[0].model_copy(update={"description": "Meter B", "amount": 6.0})]})
m = eb.merge_same_address([a, b])
assert len(m) == 1 and m[0].total_cost == 10.0 and len(m[0].charges) == 2, m
print("Same address twice on one bill is merged: total 10.00 from 4.00 + 6.00")
