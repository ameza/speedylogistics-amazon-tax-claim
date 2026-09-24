#!/usr/bin/env python3
"""Normalize Amazon's "Your Orders" Data Request export
(Retail.OrderHistory.1.csv) into the same canonical orders.csv schema as
normalize_mcp_orders.py, so build_claim.py works identically regardless of
source.

Use this as a fallback/backfill path when the MCP can't cover a date range
(e.g. orders older than what get_order_history conveniently returns, or
when you need the initial multi-year backfill and don't want to hammer
Amazon with hundreds of full_details=True fetches).

Fixes vs. the original single-purpose script this is derived from:
- Tax is `Unit Price Tax * Quantity` summed over the order's items.
  `Shipment Item Subtotal Tax` is NOT summed: it's the whole shipment's tax,
  repeated on every item row of that shipment, so summing it counts the tax
  once per item. It's only used as a per-shipment cross-check.
- Uses Decimal, not float.
- Emits the same canonical schema as the MCP path (Order ID, Order Date,
  Estimated Tax USD, Grand Total USD, Order Status, Shipping Address,
  Tracking Numbers, Source, Warnings) -- Grand Total USD is left blank here
  since the export doesn't carry it per-order in a directly comparable way,
  and Tracking Numbers uses whatever the export's own
  "Carrier Name & Tracking Number" column has verbatim (no resolution step
  needed, unlike the MCP path).
"""
import argparse
import csv
import fnmatch
import io
import os
import sys
import zipfile
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

# Amazon's Data Request export ships the order-history CSV under a name
# like "Retail.OrderHistory.1/Retail.OrderHistory.1.csv" (older export
# format) or "Your Amazon Orders/Order History.csv" (current format, with
# a literal space -- confirmed from a real export) inside the zip it
# emails a link for, but the exact numeric suffix and nesting can vary
# (e.g. multi-part exports). Match loosely rather than hardcoding a path.
ORDER_HISTORY_GLOBS = ["*OrderHistory*.csv", "*Order History*.csv"]


def _matches_order_history(filename):
    return any(fnmatch.fnmatch(filename, g) for g in ORDER_HISTORY_GLOBS)


def money(s):
    s = (s or "").replace("$", "").replace(",", "").strip()
    if s in ("", "Not Available", "N/A"):
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def find_order_history_csv_in_dir(dir_path):
    matches = []
    for root, _dirs, files in os.walk(dir_path):
        for fn in files:
            if _matches_order_history(fn):
                matches.append(os.path.join(root, fn))
    return matches


def find_order_history_csv_in_zip(zf):
    return [n for n in zf.namelist() if _matches_order_history(os.path.basename(n))]


def open_input(path):
    """Return a text-mode file-like object for the order-history CSV, given
    a path that may be: a direct .csv file, a .zip export (Amazon's raw
    download, unextracted), or a directory (an already-extracted export).
    Handles nested zips (a zip containing another zip) one level deep,
    since some Data Request exports are delivered that way.
    """
    if os.path.isdir(path):
        matches = find_order_history_csv_in_dir(path)
        if not matches:
            sys.exit(
                f"ERROR: no file matching {ORDER_HISTORY_GLOBS} found under directory {path}"
            )
        if len(matches) > 1:
            print(
                f"NOTE: multiple order-history CSVs found under {path}, using {matches[0]}: {matches}",
                file=sys.stderr,
            )
        return open(matches[0], encoding="utf-8-sig")

    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        matches = find_order_history_csv_in_zip(zf)
        if not matches:
            # One level of nesting: a zip containing another zip.
            for name in zf.namelist():
                if name.lower().endswith(".zip"):
                    inner_bytes = zf.read(name)
                    inner_zf = zipfile.ZipFile(io.BytesIO(inner_bytes))
                    inner_matches = find_order_history_csv_in_zip(inner_zf)
                    if inner_matches:
                        raw = inner_zf.read(inner_matches[0])
                        return io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig")
            sys.exit(
                f"ERROR: no file matching {ORDER_HISTORY_GLOBS} found inside zip {path} "
                f"(top-level contents: {zf.namelist()})"
            )
        if len(matches) > 1:
            print(
                f"NOTE: multiple order-history CSVs found inside {path}, using {matches[0]}: {matches}",
                file=sys.stderr,
            )
        raw = zf.read(matches[0])
        return io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig")

    # Plain CSV file.
    return open(path, encoding="utf-8-sig")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--csv",
        required=True,
        help=(
            "Path to the Amazon Data Request export -- accepts the raw .zip "
            "Amazon emails a link to (no need to extract it first), a "
            "directory it's already been extracted into, or a direct path "
            "to Retail.OrderHistory.1.csv"
        ),
    )
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    orders = OrderedDict()
    with open_input(a.csv) as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        if "Order ID" not in cols or "Order Date" not in cols:
            sys.exit(f"ERROR: expected 'Order ID' and 'Order Date' columns, got: {cols}")
        for row in reader:
            oid = (row.get("Order ID") or "").strip()
            if not oid:
                continue
            o = orders.setdefault(
                oid,
                {
                    "date": row.get("Order Date", "").strip(),
                    "tax_unit": Decimal("0"),
                    "tax_by_shipment": {},
                    "statuses": set(),
                    "addresses": set(),
                    "tracking": set(),
                },
            )
            qty = money(row.get("Quantity") or row.get("Original Quantity"))
            o["tax_unit"] += money(row.get("Unit Price Tax")) * qty
            # Same value on every item row of a shipment -- keep one per shipment.
            shipment = (row.get("Ship Date") or "", row.get("Carrier Name & Tracking Number") or "")
            o["tax_by_shipment"][shipment] = money(row.get("Shipment Item Subtotal Tax"))
            o["statuses"].add((row.get("Order Status") or "").strip().lower())
            o["addresses"].add((row.get("Shipping Address") or "").strip())
            t = (row.get("Carrier Name & Tracking Number") or "").strip()
            if t and t != "Not Available":
                o["tracking"].add(t)

    rows = []
    for oid, o in orders.items():
        tax = o["tax_unit"]
        shipment_tax = sum(o["tax_by_shipment"].values(), Decimal("0"))
        warnings = []
        if shipment_tax > 0 and shipment_tax != tax:
            warnings.append(f"item tax {tax:.2f} != shipment tax {shipment_tax:.2f} (used item tax)")
        statuses = o["statuses"]
        if statuses & {"cancelled", "canceled"} and len(statuses) == 1:
            status = "cancelled_or_unsupported"
        else:
            status = "normal"
        rows.append(
            {
                "order_id": oid,
                "order_date": o["date"],
                "estimated_tax": f"{tax:.2f}",
                "grand_total": "",
                "status": status,
                "address": " | ".join(sorted(o["addresses"])),
                "tracking": ";".join(sorted(o["tracking"])),
                "warnings": "; ".join(warnings),
            }
        )

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "Order ID",
                "Order Date",
                "Estimated Tax USD",
                "Grand Total USD",
                "Order Status",
                "Shipping Address",
                "Tracking Numbers",
                "Source",
                "Warnings",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r["order_id"],
                    r["order_date"],
                    r["estimated_tax"],
                    r["grand_total"],
                    r["status"],
                    r["address"],
                    r["tracking"],
                    "data_export",
                    r["warnings"],
                ]
            )

    print(f"Wrote {len(rows)} orders to {a.out}")


if __name__ == "__main__":
    main()
