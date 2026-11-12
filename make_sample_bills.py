"""Generate fictional sample utility bills (PDF) plus the answers we expect extracted.

    python make_sample_bills.py  ->  samples/*.pdf  and  samples/expected.json

The expected answers double as a small accuracy check: run the extractor on samples/
and compare the database with expected.json (see check_accuracy.py).
"""
import json
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = Path("samples")

BILLS = [
    {
        "file": "brightline_power_2026-03.pdf",
        "provider": "Brightline Power & Light",
        "account": "4410-22871-03",
        "mail_to": ["Oakridge Property Mgmt", "PO Box 900", "Springfield, IL 62701"],
        "service_address": "125 Maple Ave Unit 3B, Springfield, IL 62704",
        "bill_date": "2026-04-18", "due_date": "2026-05-09",
        "period": ("2026-03-15", "2026-04-14"),
        "prev_balance": 0.00, "payments": 0.00,
        "lines": [("Customer Charge", 12.50), ("Energy Charge 642 kWh @ $0.1185", 76.08),
                  ("Delivery Charge", 31.22), ("State Tax", 7.19)],
        "services": [("electric", "M-889201", 642, "kWh", 126.99)],
        "meter_rows": [("M-889201", "18,442", "19,084", "1", "642 kWh")],
        "current": 126.99, "due": 126.99,
    },
    {
        "file": "northfield_gas_2026-03.pdf",
        "provider": "Northfield Natural Gas Co.",
        "account": "NG-0098812",
        "mail_to": ["Oakridge Property Mgmt", "PO Box 900", "Springfield, IL 62701"],
        "service_address": "125 Maple Ave Unit 3B, Springfield, IL 62704",
        "bill_date": "2026-04-06", "due_date": "2026-04-27",
        "period": ("2026-03-03", "2026-04-01"),
        "prev_balance": 88.40, "payments": -88.40,
        "lines": [("Monthly Service Charge", 18.00), ("Gas Supply 54.3 therms", 32.58),
                  ("Distribution 54.3 therms", 21.45), ("Municipal Tax", 3.62)],
        "services": [("natural_gas", "G-55120", 54.3, "therms", 75.65)],
        "meter_rows": [("G-55120", "7,103", "7,155", "1.044", "54.3 therms")],
        "current": 75.65, "due": 75.65,
    },
    {
        "file": "city_of_springfield_water_2026-02.pdf",
        "provider": "City of Springfield Utilities",
        "account": "300-551-7",
        "mail_to": ["Oakridge Property Mgmt", "PO Box 900", "Springfield, IL 62701"],
        "service_address": "40 Harbor Rd, Springfield, IL 62702",
        "bill_date": "2026-03-10", "due_date": "2026-03-31",
        "period": ("2026-01-28", "2026-02-27"),
        "prev_balance": 210.15, "payments": 0.00,
        "lines": [("Water Base Charge", 22.00), ("Water Usage 9,400 gal", 58.28),
                  ("Sewer Base Charge", 18.00), ("Sewer Usage 9,400 gal", 47.94),
                  ("Trash Collection", 26.50), ("Late Fee (prior period)", 10.51)],
        "services": [("water", "W-77310", 9400, "gal", 80.28), ("sewer", None, 9400, "gal", 65.94),
                     ("trash", None, None, None, 26.50)],
        "meter_rows": [("W-77310", "1,204.6", "1,214.0", "1000 gal", "9,400 gal")],
        "current": 172.72, "due": 393.38,
    },
    {
        "file": "summit_energy_combined_2026-04.pdf",
        "provider": "Summit Energy Services",
        "account": "SE-7781-4420",
        "mail_to": ["Oakridge Property Mgmt", "PO Box 900", "Springfield, IL 62701"],
        "service_address": "88 Elm St, Springfield, IL 62703",
        "bill_date": "2026-05-12", "due_date": "2026-06-02",
        "period": ("2026-04-08", "2026-05-07"),
        "prev_balance": 0.00, "payments": 0.00,
        "lines": [("ELECTRIC - Customer Charge", 12.50), ("ELECTRIC - 1,218 kWh", 144.33),
                  ("GAS - Customer Charge", 15.00), ("GAS - 31 CCF", 27.90),
                  ("Taxes & Surcharges", 12.04)],
        "services": [("electric", "E-41007", 1218, "kWh", 156.83), ("natural_gas", "G-41007", 31, "CCF", 42.90)],
        "meter_rows": [("E-41007", "50,112", "51,330", "1", "1,218 kWh"),
                       ("G-41007", "2,890", "2,921", "1", "31 CCF")],
        "current": 211.77, "due": 211.77,
    },
]


def money(x):
    return f"${x:,.2f}" if x >= 0 else f"-${-x:,.2f}"


def draw(bill, path):
    c = canvas.Canvas(str(path), pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, h - 60, bill["provider"])
    c.setFont("Helvetica", 9)
    c.drawString(50, h - 75, "Customer Service 1-800-555-0100  |  Fictional sample bill for testing")

    c.setFont("Helvetica", 10)
    y = h - 110
    for line in bill["mail_to"]:
        c.drawString(50, y, line); y -= 13

    c.drawString(330, h - 110, f"Account Number: {bill['account']}")
    c.drawString(330, h - 123, f"Statement Date: {bill['bill_date']}")
    c.drawString(330, h - 136, f"Due Date: {bill['due_date']}")
    c.drawString(330, h - 149, f"Service Period: {bill['period'][0]} to {bill['period'][1]}")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, h - 185, "Service Address:")
    c.setFont("Helvetica", 10)
    c.drawString(150, h - 185, bill["service_address"])

    # Account summary
    y = h - 225
    c.setFont("Helvetica-Bold", 12); c.drawString(50, y, "Account Summary"); y -= 18
    c.setFont("Helvetica", 10)
    for label, val in [("Previous Balance", bill["prev_balance"]), ("Payments Received", bill["payments"]),
                       ("Current Charges", bill["current"]), ("Total Amount Due", bill["due"])]:
        c.drawString(60, y, label); c.drawRightString(300, y, money(val)); y -= 14

    # Meter table
    y -= 14
    c.setFont("Helvetica-Bold", 12); c.drawString(50, y, "Meter Information"); y -= 16
    c.setFont("Helvetica-Bold", 9)
    for x, t in zip([60, 160, 250, 340, 430], ["Meter", "Previous Read", "Current Read", "Multiplier", "Usage"]):
        c.drawString(x, y, t)
    y -= 13; c.setFont("Helvetica", 9)
    for row in bill["meter_rows"]:
        for x, t in zip([60, 160, 250, 340, 430], row):
            c.drawString(x, y, t)
        y -= 13

    # Charges detail
    y -= 14
    c.setFont("Helvetica-Bold", 12); c.drawString(50, y, "Current Charges Detail"); y -= 16
    c.setFont("Helvetica", 10)
    for label, amt in bill["lines"]:
        c.drawString(60, y, label); c.drawRightString(450, y, money(amt)); y -= 14

    # Remittance stub
    c.line(40, 120, w - 40, 120)
    c.setFont("Helvetica", 9)
    c.drawString(50, 100, f"Please return this portion with payment.  Account {bill['account']}")
    c.drawString(50, 86, f"Amount Due: {money(bill['due'])}   Due by {bill['due_date']}")
    c.save()


def split_address(addr):
    """'125 Maple Ave Unit 3B, Springfield, IL 62704' -> street, city, state, zip"""
    street, city, st_zip = [p.strip() for p in addr.split(",")]
    state, zip5 = st_zip.split()
    return {"street": street, "city": city, "state": state, "zip": zip5}


def site(addr, start, end, total, usage, charges):
    return {"address_as_printed": addr, **split_address(addr), "service_period_start": start, "service_period_end": end,
            "total_cost": total, "charges": charges, "usage": usage}


def charge_category(label, main_service):
    """Category for a printed line item, as the answer key expects it."""
    low = label.lower()
    if "tax" in low:
        return "tax"
    for word, cat in (("electric", "electric"), ("gas", "natural_gas"), ("water", "water"),
                      ("sewer", "sewer"), ("trash", "trash")):
        if low.startswith(word):
            return cat
    return main_service


def bill_key(b, sites, current, due):
    # Same shape as schema.BillExtraction, so tests can feed it straight into the pipeline.
    return {"provider_name": b["provider"], "provider_city": None, "provider_state": None,
            "account_number": b["account"], "bill_date": b["bill_date"], "due_date": b["due_date"],
            "current_charges": current, "total_amount_due": due, "currency": "USD", "sites": sites,
            "confidence": "high", "notes": None}


def main():
    OUT.mkdir(exist_ok=True)
    expected = {}
    for b in BILLS:
        draw(b, OUT / b["file"])
        # Usage = metered quantities only: no flat-fee trash, no sewer billed on water usage.
        usage = [{"utility_type": t, "meter_number": m, "usage_quantity": q, "usage_unit": u,
                  "guessed_unit": None, "guess_basis": None}
                 for t, m, q, u, _ in b["services"] if q is not None and t != "sewer"]
        # Current-period line items only: the prior-period late fee is not a charge for this period.
        charges = [{"category": charge_category(lbl, b["services"][0][0]), "description": lbl, "amount": amt}
                   for lbl, amt in b["lines"] if "prior period" not in lbl.lower()]
        sites = [site(b["service_address"], *b["period"], b["current"], usage, charges)]
        expected[b["file"]] = bill_key(b, sites, b["current"], b["due"])
    draw_multisite(MULTI, OUT / MULTI["file"])
    total = round(sum(s[5] for s in MULTI["sites"]), 2)
    sites = [site(a, ps, pe, amt, [{"utility_type": "electric", "meter_number": m,
                                    "usage_quantity": kwh, "usage_unit": "kWh",
                                    "guessed_unit": None, "guess_basis": None}],
                  [{"category": "electric", "description": "Electric charges", "amount": amt}])
             for m, a, ps, pe, kwh, amt in MULTI["sites"]]
    expected[MULTI["file"]] = bill_key(MULTI, sites, total, total)
    (OUT / "expected.json").write_text(json.dumps(expected, indent=2))
    print(f"Wrote {len(BILLS) + 1} sample bills to {OUT}/")


# A summary invoice: one account, several properties, each with its own period.
MULTI = {
    "file": "lonestar_power_summary_2026-09.pdf",
    "provider": "Lone Star Retail Power",
    "account": "LSR-55012-88",
    "bill_date": "2026-10-14", "due_date": "2026-11-04",
    "sites": [
        ("ESI-1001", "410 Cedar Ln, Plano, TX 75074", "2026-08-28", "2026-09-27", 1104, 168.42),
        ("ESI-1002", "1820 Brook Hollow Dr, Allen, TX 75002", "2026-09-02", "2026-10-01", 736, 119.87),
        ("ESI-1003", "77 Quarry Rd Unit 4, McKinney, TX 75069", "2026-09-05", "2026-10-05", 512, 88.15),
    ],
}


def draw_multisite(bill, path):
    c = canvas.Canvas(str(path), pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 18); c.drawString(50, h - 60, bill["provider"])
    c.setFont("Helvetica", 9); c.drawString(50, h - 75, "Summary Invoice  |  Fictional sample bill for testing")
    c.setFont("Helvetica", 10)
    for i, line in enumerate(["Oakridge Property Mgmt", "PO Box 900", "Springfield, IL 62701"]):
        c.drawString(50, h - 110 - 13 * i, line)
    total = sum(s[5] for s in bill["sites"])
    for i, line in enumerate([f"Account Number: {bill['account']}", f"Invoice Date: {bill['bill_date']}",
                              f"Due Date: {bill['due_date']}", f"Previous Balance: $0.00",
                              f"Current Charges: {money(total)}", f"Amount Due: {money(total)}"]):
        c.drawString(330, h - 110 - 13 * i, line)
    y = h - 210
    c.setFont("Helvetica-Bold", 12); c.drawString(50, y, "Summary of Service Locations"); y -= 16
    c.setFont("Helvetica-Bold", 8)
    for x, t in zip([50, 110, 300, 400, 470, 520], ["ESI ID", "Service Address", "Service Period", "", "kWh", "Charges"]):
        c.drawString(x, y, t)
    y -= 12; c.setFont("Helvetica", 8)
    for m, a, ps, pe, kwh, amt in bill["sites"]:
        c.drawString(50, y, m); c.drawString(110, y, a); c.drawString(300, y, f"{ps} - {pe}")
        c.drawString(470, y, f"{kwh:,}"); c.drawRightString(560, y, money(amt)); y -= 12
    c.setFont("Helvetica-Bold", 8)
    c.drawString(50, y, "Totals"); c.drawString(470, y, f"{sum(s[4] for s in bill['sites']):,}")
    c.drawRightString(560, y, money(total))
    c.save()


if __name__ == "__main__":
    main()
