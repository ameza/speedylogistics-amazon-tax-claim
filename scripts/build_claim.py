#!/usr/bin/env python3
"""Build the sales-tax refund claim table from a canonical orders.csv
(produced by normalize_mcp_orders.py or normalize_data_request_csv.py).

Deterministic: every order ID and USD amount in the email comes from this
script's output, never typed or recomputed by the model. Standard library
only.

Usage:
  python3 build_claim.py --csv orders.csv \
      [--from 2025-09-23] [--to 2026-09-23] \
      [--ledger claimed-orders.csv] [--address-filter "MIAMI"] \
      [--matched speedy-matches.csv] [--out-dir ./out]
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

EXCLUDED_STATUSES = {"cancelled_or_unsupported"}
REVIEW_STATUSES = {"possible_refund"}


def money(s):
    s = (s or "").replace("$", "").replace(",", "").strip()
    if s in ("", "Not Available", "N/A"):
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s.split("T")[0])
    except ValueError:
        try:
            return dt.datetime.strptime(s, "%m/%d/%Y").date()
        except ValueError:
            return None


def load_ids(path, col="Order ID"):
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8-sig") as f:
        return {r[col].strip() for r in csv.DictReader(f) if r.get(col)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Canonical orders.csv (see normalize_*.py)")
    today = dt.date.today()
    ap.add_argument("--from", dest="date_from", default=str(today - dt.timedelta(days=365)))
    ap.add_argument("--to", dest="date_to", default=str(today))
    ap.add_argument("--ledger", help="CSV of already-claimed orders (column 'Order ID'); these are excluded")
    ap.add_argument("--address-filter", help="case-insensitive substring the Shipping Address must contain")
    ap.add_argument("--matched", help="CSV of orders with proof of export (column 'Order ID'); if given, only these are claimed")
    ap.add_argument("--out-dir", default=".")
    a = ap.parse_args()

    d_from, d_to = dt.date.fromisoformat(a.date_from), dt.date.fromisoformat(a.date_to)
    claimed = load_ids(a.ledger)
    matched = load_ids(a.matched) if a.matched else None

    warnings = []
    rows = []
    with open(a.csv, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        required = {"Order ID", "Order Date", "Estimated Tax USD"}
        if not required.issubset(cols):
            sys.exit(f"ERROR: expected columns {required}, got: {cols}")

        seen = OrderedDict()
        for row in reader:
            oid = (row.get("Order ID") or "").strip()
            if not oid:
                continue
            if oid in seen:
                warnings.append(f"{oid}: duplicate row in input CSV -- kept first occurrence")
                continue
            seen[oid] = row

        for oid, row in seen.items():
            d = parse_date(row.get("Order Date"))
            tax = money(row.get("Estimated Tax USD"))
            status = (row.get("Order Status") or "").strip()
            tracking = (row.get("Tracking Numbers") or "").strip()
            row_warnings = (row.get("Warnings") or "").strip()
            if row_warnings:
                warnings.append(f"{oid}: {row_warnings}")

            reason = None
            if tax <= 0:
                reason = "no tax"
            elif d is None or not (d_from <= d <= d_to):
                reason = "outside window"
            elif status in EXCLUDED_STATUSES:
                reason = "cancelled/unsupported order"
            elif status in REVIEW_STATUSES:
                reason = "possible refund -- needs manual review before claiming"
            elif oid in claimed:
                reason = "already claimed"
            elif a.address_filter and a.address_filter.lower() not in (row.get("Shipping Address") or "").lower():
                reason = "not shipped to forwarder"
            elif matched is not None and oid not in matched:
                reason = "no proof of export (Speedy match)"

            rows.append(
                {
                    "order_id": oid,
                    "date": d.isoformat() if d else (row.get("Order Date") or ""),
                    "tax": tax,
                    "tracking": tracking,
                    "include": reason is None,
                    "reason": reason or "",
                }
            )

    inc = sorted([r for r in rows if r["include"]], key=lambda r: r["date"])
    total = sum((r["tax"] for r in inc), Decimal("0"))
    os.makedirs(a.out_dir, exist_ok=True)

    with open(os.path.join(a.out_dir, "claim_orders.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "Order ID", "Order Date", "Tax USD", "Tracking"])
        for i, r in enumerate(inc, 1):
            w.writerow([i, r["order_id"], r["date"], f"{r['tax']:.2f}", r["tracking"]])

    with open(os.path.join(a.out_dir, "all_orders_audit.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Order ID", "Order Date", "Tax USD", "Included", "Excluded reason", "Tracking"])
        for r in sorted(rows, key=lambda r: r["date"]):
            w.writerow([r["order_id"], r["date"], f"{r['tax']:.2f}", r["include"], r["reason"], r["tracking"]])

    lines = [f"{i}. Order ID: {r['order_id']} | Date: {r['date']} | Tax Amount: ${r['tax']:.2f}" for i, r in enumerate(inc, 1)]
    with open(os.path.join(a.out_dir, "email_order_lines.txt"), "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    summary = {
        "window": [a.date_from, a.date_to],
        "orders_in_input": len(rows),
        "orders_claimed": len(inc),
        "total_tax_usd": f"{total:.2f}",
        "warnings": warnings,
    }
    with open(os.path.join(a.out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print("\n".join(lines) if lines else "No eligible orders.")
    print(f"\nTOTAL: ${total:.2f} across {len(inc)} orders")
    for wmsg in warnings:
        print("WARNING:", wmsg, file=sys.stderr)


if __name__ == "__main__":
    main()
