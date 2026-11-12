"""Export the bill database as CSV files for the Property Utilities dashboard.

    python export_dashboard_data.py [--db output/bills.db] [--out output/dashboard]

Writes properties.csv, bills.csv, charges.csv and usage.csv. Re-run it after new
bills are loaded, then upload the files again to the dashboard (Sources > replace file).
"""
import argparse
import csv
import sqlite3
from pathlib import Path

QUERIES = {
    "properties": """
        SELECT address_id,
               service_address || COALESCE(', ' || city, '') || COALESCE(', ' || state, '') || COALESCE(' ' || zip, '') AS address,
               service_address AS street, city, state, zip
        FROM service_addresses ORDER BY address_id""",
    "bills": """
        SELECT s.bill_id, s.address_id, b.provider_name AS provider, b.account_number AS account,
               s.service_month, s.service_period_start AS period_start, s.service_period_end AS period_end,
               b.bill_date, b.due_date, s.total_cost, b.current_charges, b.total_amount_due AS amount_due,
               b.status, b.source_file
        FROM bills b JOIN bill_sites s USING (bill_id) ORDER BY s.address_id, s.service_month""",
    "charges": """
        SELECT c.charge_id, c.bill_id, c.address_id, s.service_month, c.category, c.description, c.amount
        FROM site_charges c JOIN bill_sites s USING (bill_id, address_id) ORDER BY c.bill_id, c.charge_id""",
    "usage": """
        SELECT u.usage_id, u.bill_id, u.address_id, s.service_month, u.utility_type, u.meter_number,
               u.usage_quantity, u.usage_unit, u.unit_source, u.unit_basis
        FROM site_usage u JOIN bill_sites s USING (bill_id, address_id) ORDER BY u.bill_id, u.usage_id""",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="output/bills.db")
    ap.add_argument("--out", default="output/dashboard")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    for name, sql in QUERIES.items():
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        print(f"{name}.csv: {len(rows)} rows")


if __name__ == "__main__":
    main()
