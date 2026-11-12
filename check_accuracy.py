"""Compare what the pipeline stored in the DB with a hand-verified answer key.

    python check_accuracy.py samples/expected.json [--db output/bills.db]

The answer key uses the same shape as schema.BillExtraction. Use it on the samples
now, and on a hand-keyed set of ~20 real bills per new provider before trusting the
pipeline unattended.

Checked per bill: current charges, and that the site totals add up to them.
Checked per service address: printed address, city/state/ZIP (service_addresses), service period, total cost,
charge items (totals per category), and each usage quantity + unit.
"""
import argparse
import json
import sqlite3
import sys

from pathlib import Path

from extract_bills import connect, normalize_unit


def norm(s):
    return " ".join(str(s).lower().replace(",", " ").replace(".", " ").split()) if s is not None else None


def compose(s):
    return f"{s['street']}, {s['city']}, {s['state']} {s['zip']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("expected")
    ap.add_argument("--db", default="output/bills.db")
    args = ap.parse_args()
    expected = json.load(open(args.expected))
    conn = connect(Path(args.db))   # upgrades an older database layout first
    conn.row_factory = sqlite3.Row

    wrong = 0
    for fname, exp in expected.items():
        bill = conn.execute("SELECT * FROM bills WHERE source_file=? ORDER BY bill_id DESC LIMIT 1", (fname,)).fetchone()
        if bill is None:
            print(f"MISSING  {fname}"); wrong += 1; continue
        sites = {norm(r["service_address"]): r for r in
                 conn.execute("""SELECT s.*, a.city, a.state, a.zip FROM bill_sites s
                                 LEFT JOIN service_addresses a USING (address_id) WHERE s.bill_id=?""",
                              (bill["bill_id"],))}
        problems = []
        if bill["current_charges"] is None or abs(bill["current_charges"] - exp["current_charges"]) > 0.005:
            problems.append(f"current_charges {bill['current_charges']} != {exp['current_charges']}")
        if len(sites) != len(exp["sites"]):
            problems.append(f"{len(sites)} addresses found, expected {len(exp['sites'])}")

        for es in exp["sites"]:
            addr = es["address_as_printed"]          # bill tables keep the address as printed
            got = sites.get(norm(addr))
            if got is None:
                problems.append(f"address not found: {addr!r} (have {[r['service_address'] for r in sites.values()]})")
                continue
            for f in ("city", "state", "zip"):        # completed values live in service_addresses
                if norm(got[f]) != norm(es[f]):
                    problems.append(f"{addr}: {f} {got[f]} != {es[f]}")
            for f in ("service_period_start", "service_period_end"):
                if got[f] != es[f]:
                    problems.append(f"{addr}: {f} {got[f]} != {es[f]}")
            if got["total_cost"] is None or abs(got["total_cost"] - es["total_cost"]) > 0.005:
                problems.append(f"{addr}: total_cost {got['total_cost']} != {es['total_cost']}")
            # Charge items: compare totals per category (labels may be worded differently).
            got_cat = {}
            for c in conn.execute("SELECT category, amount FROM site_charges WHERE bill_id=? AND address_id=?",
                                  (got["bill_id"], got["address_id"])):
                got_cat[c["category"]] = round(got_cat.get(c["category"], 0) + c["amount"], 2)
            want_cat = {}
            for c in es["charges"]:
                want_cat[c["category"]] = round(want_cat.get(c["category"], 0) + c["amount"], 2)
            if got_cat != want_cat:
                problems.append(f"{addr}: charges by category {got_cat} != {want_cat}")
            usage = {(u["utility_type"], u["usage_unit"]): u["usage_quantity"] for u in
                     conn.execute("SELECT * FROM site_usage WHERE bill_id=? AND address_id=?",
                                  (got["bill_id"], got["address_id"]))}
            for eu in es["usage"]:
                k = (eu["utility_type"], normalize_unit(eu["usage_unit"]))
                if usage.get(k) != eu["usage_quantity"]:
                    problems.append(f"{addr}: {k[0]} usage {usage.get(k)} != {eu['usage_quantity']} {k[1]} (have {usage})")
        if problems:
            wrong += 1
            print(f"WRONG    {fname}: " + "; ".join(problems))
        else:
            print(f"OK       {fname}")
    print(f"\n{len(expected) - wrong}/{len(expected)} bills fully correct")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
