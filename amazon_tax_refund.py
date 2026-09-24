#!/usr/bin/env python3
"""Interactive batch runner for the Amazon forwarder sales-tax refund claim.

Usage:
    1. Request "Your Orders" at
       https://www.amazon.com/hz/privacy-central/data-requests/preview.html
       and wait for Amazon's download email (this step can't be scripted).
    2. Save the .zip it links to in this same folder (next to this script) --
       no need to extract it.
    3. Run:  python3 amazon_tax_refund.py

It then asks for exactly two things:
    - Confirmation of which .zip in this folder is the Amazon export
      (skipped automatically if there's only one).
    - Your Speedy Logistics CR username/password, typed directly into
      Speedy's real login form by speedy_client.py -- never stored or
      printed. Only asked when there's actually a new order
      to check, and only when a fresh login is needed.

Every order Amazon has ever shown you is tracked in a local SQLite database
(orders.db, next to this script) -- this is what replaces the old
"how many days back should I look" question. Each run just asks: "of the
orders I know about, which ones still need a Speedy check, and which
Speedy-confirmed ones haven't been claimed yet?" There's no time window to
reason about and nothing to configure -- an order is eligible exactly once:
from the moment Speedy confirms it has proof of export, until you mark it
claimed.

Fully interactive, no command-line arguments or flags -- run it with just
`python3 amazon_tax_refund.py` and it opens a menu (run the pipeline / check status /
mark orders claimed / record Amazon's reply / purge), picked by number.

Each run leaves exactly two files in its dated folder: email.txt (the
ready-to-send claim email, order list included) and invoices.zip (the
proof-of-export invoices to attach). It never sends anything itself.

Requires `uv` (https://astral.sh/uv) on PATH -- used to run speedy_client.py
with its Python dependencies (requests, playwright) without polluting the
system Python. Everything else uses the system python3's standard library.
"""
import csv
import datetime as dt
import getpass
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR / "scripts"
CONFIG_PATH = THIS_DIR / "config" / "config.json"
STAGING_DIR = THIS_DIR / ".cache"
COOKIE_JAR = SCRIPTS_DIR / ".speedy-session" / "cookies.json"  # kept in sync with speedy_client.py's own COOKIE_JAR_PATH
FRESH_SESSION_HOURS = 20  # Speedy sessions last ~24h; re-login a bit early.

DB_PATH = THIS_DIR / "orders.db"
PDF_STORE_DIR = STAGING_DIR / "invoices"  # downloaded Speedy PDFs, reused across runs
EARLIEST_DATE = "2000-01-01"  # stand-in for "no date filtering" -- build_claim.py needs a --from

CONFIG_DEFAULTS = {
    "to_email": "tax-exempt@amazon.com",
    "from_email": "you@example.com",
    "refund_method": "my original payment method or as Amazon Gift Card credit",
    "attachment_size_limit_mb": 25,
    "forwarder_name": "Speedy Logistics CR",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    order_date TEXT,
    tax_usd TEXT,
    tracking TEXT,
    shipping_address TEXT,
    order_status TEXT,
    speedy_status TEXT NOT NULL DEFAULT 'pending',
    speedy_message TEXT,
    invoice_pdf TEXT,
    claimed INTEGER NOT NULL DEFAULT 0,
    claim_date TEXT,
    email_thread TEXT,
    outcome TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_runs (
    run_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    PRIMARY KEY (run_id, order_id)
);
"""


class BackToMenu(Exception):
    """Raised when the user chooses to bail out of the current action back
    to the main menu (e.g. after repeated Speedy login failures)."""


def die(msg):
    sys.exit(f"\nERROR: {msg}\n")


def run(cmd, env=None, desc=None):
    if desc:
        print(f"\n==> {desc}")
    print("    $ " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        die(f"step failed (exit code {result.returncode}): {' '.join(str(c) for c in cmd)}")


def check_uv():
    if not shutil.which("uv"):
        die(
            "`uv` is required but wasn't found on PATH.\nInstall it with:\n\n"
            "  curl -LsSf https://astral.sh/uv/install.sh | sh\n\nthen re-run this script."
        )


def ensure_playwright_browser():
    print("\n==> Checking Playwright's Chromium browser is installed...")
    check = subprocess.run(
        [
            "uv", "run", "--with", "requests", "--with", "playwright", "python3", "-c",
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    b = p.chromium.launch(headless=True); b.close()",
        ],
        cwd=SCRIPTS_DIR,
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        print("    OK.")
        return
    print("    Not installed yet -- downloading Chromium for Playwright (one-time, ~300MB)...")
    run(["uv", "run", "--with", "playwright", "playwright", "install", "chromium"])


def select(msg, options, default=None, allow_other=True):
    """Numbered menu -- pick by number, no free typing required. If 'Other'
    is picked, falls back to a single free-text prompt for that one value."""
    print(f"\n{msg}")
    opts = list(dict.fromkeys(options))  # de-dupe, keep order
    default_idx = opts.index(default) if default in opts else 0
    for i, opt in enumerate(opts, 1):
        marker = "  <- default" if i - 1 == default_idx else ""
        print(f"  {i}. {opt}{marker}")
    other_num = len(opts) + 1
    if allow_other:
        print(f"  {other_num}. Other (type your own)")
    while True:
        raw = input(f"Choice [{default_idx + 1}]: ").strip()
        if not raw:
            return opts[default_idx]
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(opts):
                return opts[n - 1]
            if allow_other and n == other_num:
                val = input("Enter value: ").strip()
                if val:
                    return val
                continue
        print("Please enter a number from the list.")


def find_zip():
    candidates = sorted(THIS_DIR.glob("*.zip"))
    if not candidates:
        die(
            "No .zip file found in this folder.\n\n"
            "Request your Amazon order history export at\n"
            "  https://www.amazon.com/hz/privacy-central/data-requests/preview.html\n"
            "(select \"Your Orders\"), wait for Amazon's download email, then save the .zip\n"
            f"it links to into this folder ({THIS_DIR}) and re-run this script."
        )
    if len(candidates) == 1:
        print(f"\nFound Amazon export: {candidates[0].name}")
        return candidates[0]
    names = [c.name for c in candidates]
    chosen = select("Multiple .zip files found -- which one is the Amazon orders export?", names)
    if chosen in names:
        return candidates[names.index(chosen)]
    p = Path(chosen).expanduser()
    if not p.is_file():
        die(f"No file found at {p}")
    return p


def bootstrap_config():
    print("\n" + "=" * 70)
    print("First run -- a few one-time setup questions (saved to config.json,")
    print("won't be asked again).")
    print("=" * 70)
    defaults = CONFIG_DEFAULTS
    cfg = {
        "to_email": defaults["to_email"],
        "from_email": defaults["from_email"],
        "refund_method": defaults["refund_method"],
        "attachment_size_limit_mb": defaults["attachment_size_limit_mb"],
    }

    print(f"Orders are tracked in {DB_PATH}; each run's claim files land in their own dated/timed folder here.")

    cfg["forwarder_name"] = select("Freight forwarder name:", [defaults["forwarder_name"]])
    cfg["to_email"] = select("Claim recipient email:", [defaults["to_email"]])
    cfg["from_email"] = ""
    while not cfg["from_email"]:
        cfg["from_email"] = input("\nYour email (From address): ").strip()
    cfg["refund_method"] = select(
        "Refund method (as it'll appear in the claim email):",
        [defaults["refund_method"], "my original payment method", "Amazon Gift Card credits"],
    )
    cfg["signature"] = select("Email signature (name to sign the claim email):", [cfg["from_email"].split("@")[0]])

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"\nSaved {CONFIG_PATH} -- edit it by hand any time; this won't ask again.")
    return cfg


def load_or_bootstrap_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return bootstrap_config()


def cookie_jar_age_hours():
    if not COOKIE_JAR.exists():
        return None
    return (dt.datetime.now().timestamp() - COOKIE_JAR.stat().st_mtime) / 3600


def speedy_login_if_needed():
    age = cookie_jar_age_hours()
    if age is not None and age < FRESH_SESSION_HOURS:
        choice = select(
            f"Found a Speedy session from {age:.1f}h ago.",
            ["Reuse it", "Log in again"],
            allow_other=False,
        )
        if choice == "Reuse it":
            return

    while True:
        print("\n==> Speedy Logistics CR login needed.")
        print("    Typed directly into Speedy's real login page by speedy_client.py --")
        print("    never saved to disk or printed.")
        username = input("    Speedy username/email: ").strip()
        password = getpass.getpass("    Speedy password: ")
        if not username or not password:
            print("    Username and password are both required.")
            continue

        env = dict(os.environ)
        env["SPEEDY_USERNAME"] = username
        env["SPEEDY_PASSWORD"] = password
        print("\n==> Logging into Speedy (a real browser window will open)...")
        result = subprocess.run(
            [
                "uv", "run", "--with", "requests", "--with", "playwright", "python3",
                str(SCRIPTS_DIR / "speedy_client.py"), "login",
            ],
            env=env,
        )
        if result.returncode == 0:
            return

        print(
            "\n    Login didn't go through -- likely an incorrect password (or Speedy needed\n"
            "    something in the browser window, like 2FA or a CAPTCHA, before it timed out)."
        )
        choice = select("What now?", ["Try again", "Back to main menu"], allow_other=False)
        if choice == "Back to main menu":
            raise BackToMenu()


# --- SQLite order tracking -------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _money(s):
    s = (s or "").replace("$", "").replace(",", "").strip()
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def db_upsert_orders(conn, orders_csv_path):
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    with open(orders_csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    new_count = 0
    for r in rows:
        oid = (r.get("Order ID") or "").strip()
        if not oid:
            continue
        fields = (
            r.get("Order Date", ""), r.get("Estimated Tax USD", "0"), r.get("Tracking Numbers", ""),
            r.get("Shipping Address", ""), r.get("Order Status", ""),
        )
        existing = conn.execute("SELECT 1 FROM orders WHERE order_id = ?", (oid,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE orders SET order_date=?, tax_usd=?, tracking=?, shipping_address=?, "
                "order_status=?, last_seen=? WHERE order_id=?",
                fields + (now_iso, oid),
            )
        else:
            conn.execute(
                "INSERT INTO orders (order_id, order_date, tax_usd, tracking, shipping_address, "
                "order_status, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (oid,) + fields + (now_iso, now_iso),
            )
            new_count += 1
    conn.commit()
    return new_count, len(rows)


def db_pending_for_speedy(conn):
    rows = conn.execute(
        "SELECT order_id, tax_usd, tracking FROM orders WHERE claimed=0 AND speedy_status != 'success'"
    ).fetchall()
    return [r for r in rows if _money(r["tax_usd"]) > 0]


def write_pending_csv(conn, path):
    pending = db_pending_for_speedy(conn)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Order ID", "Tracking Numbers"])
        for r in pending:
            w.writerow([r["order_id"], r["tracking"]])
    return len(pending)


def db_apply_speedy_attempts(conn, attempts_csv_path):
    with open(attempts_csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    best = {}
    for r in rows:
        oid = (r.get("Order ID") or "").strip()
        if not oid:
            continue
        status = r.get("status", "")
        if oid not in best or status == "success":
            best[oid] = (status, r.get("Invoice PDF", ""))
    for oid, (status, pdf_path) in best.items():
        if status == "success":
            conn.execute(
                "UPDATE orders SET speedy_status='success', speedy_message='', invoice_pdf=? WHERE order_id=?",
                (pdf_path, oid),
            )
        elif status == "no_tracking_number":
            conn.execute(
                "UPDATE orders SET speedy_status='no_tracking_number', speedy_message='' WHERE order_id=?",
                (oid,),
            )
        else:
            conn.execute(
                "UPDATE orders SET speedy_status='denied', speedy_message=? WHERE order_id=?",
                (status, oid),
            )
    conn.commit()


def db_write_id_csv(conn, where_sql, path):
    rows = conn.execute(f"SELECT order_id FROM orders WHERE {where_sql}").fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Order ID"])
        for r in rows:
            w.writerow([r["order_id"]])
    return len(rows)


def db_status_counts(conn):
    def count(where):
        return conn.execute(f"SELECT COUNT(*) FROM orders WHERE {where}").fetchone()[0]

    return {
        "total": count("1=1"),
        "claimed": count("claimed=1"),
        "ready": count("claimed=0 AND speedy_status='success'"),
        "never_checked": count("claimed=0 AND speedy_status='pending'"),
        "denied": count("claimed=0 AND speedy_status='denied'"),
        "no_tracking": count("claimed=0 AND speedy_status='no_tracking_number'"),
    }


# --- Interactive actions ----------------------------------------------------

def cmd_status():
    if not DB_PATH.exists():
        print(f"No {DB_PATH} yet -- run the pipeline first.")
        return
    conn = db_connect()
    s = db_status_counts(conn)
    print(f"Tracked orders in {DB_PATH}: {s['total']}")
    print(f"  Already claimed:                                  {s['claimed']}")
    print(f"  Ready to claim now (Speedy-confirmed, unclaimed): {s['ready']}")
    print(f"  Never checked with Speedy yet:                    {s['never_checked']}")
    print(f"  No Speedy proof so far (denied):                  {s['denied']}  (retried automatically next run)")
    print(f"  No tracking number to check:                      {s['no_tracking']}")


def cmd_mark_claimed():
    if not DB_PATH.exists():
        print(f"No {DB_PATH} yet -- run the pipeline first.")
        return
    conn = db_connect()
    runs = conn.execute(
        "SELECT c.run_id, COUNT(*) AS n FROM claim_runs c JOIN orders o ON o.order_id = c.order_id "
        "WHERE o.claimed = 0 GROUP BY c.run_id ORDER BY c.run_id DESC"
    ).fetchall()
    if not runs:
        print("No unclaimed runs found -- run the pipeline first.")
        return
    options = [f"{r['run_id']}  ({r['n']} unclaimed order(s))" for r in runs]
    choice = select("Which run's claim did you actually send?", options, allow_other=False)
    run_id = runs[options.index(choice)]["run_id"]

    email_thread = input("Optional note (e.g. email thread link) to store alongside [blank to skip]: ").strip()

    order_ids = [r["order_id"] for r in conn.execute("SELECT order_id FROM claim_runs WHERE run_id=?", (run_id,))]
    today = dt.date.today().isoformat()
    n = 0
    for oid in order_ids:
        conn.execute(
            "UPDATE orders SET claimed=1, claim_date=?, email_thread=?, outcome='pending' "
            "WHERE order_id=? AND claimed=0",
            (today, email_thread, oid),
        )
        n += 1
    conn.commit()
    print(f"Marked {n} order(s) as claimed in {DB_PATH}. They won't be included in future claims.")


def cmd_mark_outcome():
    if not DB_PATH.exists():
        print(f"No {DB_PATH} yet -- run the pipeline first.")
        return
    conn = db_connect()
    rows = conn.execute(
        "SELECT order_id, order_date, tax_usd FROM orders WHERE claimed=1 AND "
        "(outcome IS NULL OR outcome='pending') ORDER BY claim_date DESC"
    ).fetchall()
    if not rows:
        print("No claimed orders are awaiting an outcome.")
        return
    options = [f"{r['order_id']}  ({r['order_date']}, ${r['tax_usd']})" for r in rows]
    choice = select("Which order do you want to record Amazon's reply for?", options, allow_other=False)
    order_id = rows[options.index(choice)]["order_id"]
    outcome = select("Outcome:", ["refunded", "rejected", "more info requested"], allow_other=False)
    conn.execute("UPDATE orders SET outcome=? WHERE order_id=?", (outcome, order_id))
    conn.commit()
    print(f"Order {order_id} outcome set to '{outcome}'.")


def write_invoices_zip(order_ids, zip_path):
    """Zip every stored Speedy invoice for these orders (an order split across
    several packages has one PDF per tracking number)."""
    pdfs = []
    for oid in order_ids:
        pdfs.extend(sorted(PDF_STORE_DIR.glob(f"speedy_{oid}_*.pdf")))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pdfs:
            z.write(p, p.name)
    return pdfs


def compose_email(cfg, order_lines, n_orders, total, attachment_name):
    forwarder = cfg.get("forwarder_name", CONFIG_DEFAULTS["forwarder_name"])
    return (
        f"To: {cfg['to_email']}\n"
        f"From: {cfg['from_email']}\n"
        f"Subject: Sales tax refund request -- orders exported abroad ({n_orders} orders, ${total})\n"
        f"Attachment: {attachment_name}\n"
        "\n"
        "Hello,\n"
        "\n"
        "I am requesting a refund of the sales tax charged on the Amazon orders below. "
        f"They were shipped to my freight forwarder, {forwarder}, and exported outside the "
        "United States, so they are exempt from sales tax. The forwarder's proof-of-export "
        "invoices for every order are attached.\n"
        "\n"
        f"{order_lines}\n"
        "\n"
        f"Total sales tax to refund: ${total} across {n_orders} orders.\n"
        "\n"
        f"Please issue the refund to {cfg['refund_method']}.\n"
        "\n"
        "Thank you,\n"
        f"{cfg['signature']}\n"
    )


def run_pipeline():
    print("Amazon forwarder sales-tax refund -- batch claim runner")
    print("=" * 70)

    check_uv()
    zip_path = find_zip()

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staging_csv = STAGING_DIR / "orders.csv"
    run(
        [
            sys.executable, str(SCRIPTS_DIR / "normalize_data_request_csv.py"),
            "--csv", str(zip_path), "--out", str(staging_csv),
        ],
        desc="Normalizing Amazon export",
    )

    cfg = load_or_bootstrap_config()

    now = dt.datetime.now()
    run_id = now.strftime("%Y-%m-%d_%H-%M-%S")
    # Only the two deliverables (email.txt, invoices.zip) live under a visible
    # per-run folder -- everything else is plumbing between the individual
    # scripts and goes in a temp dir that's cleaned up automatically.
    run_dir = THIS_DIR / run_id

    conn = db_connect()
    try:
        with tempfile.TemporaryDirectory(prefix="amazon_tax_refund_") as work:
            work_dir = Path(work)
            new_count, total_count = db_upsert_orders(conn, staging_csv)
            print(f"\n==> Tracking {total_count} orders in {DB_PATH} ({new_count} new this run).")

            pending_csv = work_dir / "pending.csv"
            pending_count = write_pending_csv(conn, pending_csv)

            if pending_count:
                print(f"==> {pending_count} order(s) need a Speedy check.")
                PDF_STORE_DIR.mkdir(parents=True, exist_ok=True)
                ensure_playwright_browser()
                speedy_login_if_needed()

                attempts_csv = work_dir / "speedy-invoice-attempts.csv"
                try:
                    run(
                        [
                            "uv", "run", "--with", "requests", "--with", "playwright", "python3",
                            str(SCRIPTS_DIR / "speedy_client.py"), "generate-invoices",
                            "--orders", str(pending_csv), "--out", str(attempts_csv),
                            "--pdf-dir", str(PDF_STORE_DIR),
                        ],
                        desc="Fetching Speedy proof-of-export invoices",
                    )
                except KeyboardInterrupt:
                    # The subprocess saves whatever it checked before being
                    # interrupted -- apply that to the DB so a re-run doesn't
                    # re-check the same orders, then let the interrupt propagate.
                    if attempts_csv.exists():
                        db_apply_speedy_attempts(conn, attempts_csv)
                        print(f"\n==> Saved progress for the orders checked before the interrupt.")
                    raise
                db_apply_speedy_attempts(conn, attempts_csv)
            else:
                print("==> Every tracked order already has a Speedy result on file -- skipping Speedy (no login needed).")

            matched_csv = work_dir / "matched.csv"
            ledger_csv = work_dir / "already-claimed.csv"
            db_write_id_csv(conn, "claimed=0 AND speedy_status='success'", matched_csv)
            db_write_id_csv(conn, "claimed=1", ledger_csv)

            claim_dir = work_dir / "claim"
            run(
                [
                    sys.executable, str(SCRIPTS_DIR / "build_claim.py"),
                    "--csv", str(staging_csv), "--from", EARLIEST_DATE, "--to", str(now.date()),
                    "--ledger", str(ledger_csv), "--matched", str(matched_csv), "--out-dir", str(claim_dir),
                ],
                desc="Building the claim",
            )

            with open(claim_dir / "claim_orders.csv", encoding="utf-8-sig") as f:
                claim_rows = list(csv.DictReader(f))
            with open(claim_dir / "email_order_lines.txt") as f:
                order_lines = f.read().strip()
            with open(claim_dir / "summary.json") as f:
                total = json.load(f)["total_tax_usd"]

        if not claim_rows:
            print("\n==> Nothing new to claim this run.")
            return

        run_dir.mkdir(parents=True, exist_ok=True)
        zip_path = run_dir / "invoices.zip"
        pdfs = write_invoices_zip([r["Order ID"] for r in claim_rows], zip_path)
        email_path = run_dir / "email.txt"
        email_path.write_text(compose_email(cfg, order_lines, len(claim_rows), total, zip_path.name))

        conn.executemany(
            "INSERT OR IGNORE INTO claim_runs (run_id, order_id) VALUES (?, ?)",
            [(run_id, r["Order ID"]) for r in claim_rows],
        )
        conn.commit()

        missing = sorted({r["Order ID"] for r in claim_rows} - {p.name.split("_")[1] for p in pdfs})
        size_mb = zip_path.stat().st_size / (1024 * 1024)
        limit_mb = cfg.get("attachment_size_limit_mb", 25)

        print("\n" + "=" * 70)
        print("DONE")
        print("=" * 70)
        print(f"Orders in this claim: {len(claim_rows)}  |  Total tax: ${total}")
        print(f"\n  - {email_path}  (email to send, order list included)")
        print(f"  - {zip_path}  ({len(pdfs)} invoice(s), {size_mb:.1f} MB -- attach to the email)")
        if missing:
            print(f"\n==> WARNING: no invoice PDF found in {PDF_STORE_DIR} for: {', '.join(missing)}")
        if size_mb > limit_mb:
            print(f"\n==> WARNING: invoices.zip is over {limit_mb} MB -- split the claim into two emails.")
        print(
            "\nNothing was sent. Review the email, send it, then run python3 amazon_tax_refund.py again\n"
            f"and pick 'Mark orders as claimed' for this run ({run_id}), so they're never claimed twice."
        )
    finally:
        conn.close()


def cmd_purge():
    run_dirs = [d for d in THIS_DIR.iterdir() if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", d.name)]
    targets = [p for p in (DB_PATH, STAGING_DIR) if p.exists()] + sorted(run_dirs)
    if COOKIE_JAR.exists():
        keep = select("Also forget the saved Speedy login?", ["Keep it", "Forget it"], allow_other=False)
        if keep == "Forget it":
            targets.append(COOKIE_JAR.parent)
    if not targets:
        print("Nothing to purge.")
        return
    print("\nThis permanently deletes:")
    for t in targets:
        print(f"  - {t}")
    print("Kept: config/, scripts/, and your Amazon export .zip.")
    if input('Type "purge" to confirm: ').strip().lower() != "purge":
        print("Cancelled -- nothing deleted.")
        return
    for t in targets:
        shutil.rmtree(t) if t.is_dir() else t.unlink()
    print("Purged. The next pipeline run starts from scratch.")


def main():
    if len(sys.argv) > 1:
        die("This script is fully interactive -- run it with no arguments: python3 amazon_tax_refund.py")

    while True:
        action = select(
            "What would you like to do?",
            [
                "Run the claim pipeline",
                "Check status",
                "Mark orders as claimed (after sending the email)",
                "Record Amazon's reply for a claimed order",
                "Purge all data (start over)",
                "Quit",
            ],
            allow_other=False,
        )
        if action == "Quit":
            print("Bye.")
            return
        try:
            if action == "Check status":
                cmd_status()
            elif action == "Mark orders as claimed (after sending the email)":
                cmd_mark_claimed()
            elif action == "Purge all data (start over)":
                cmd_purge()
            elif action == "Record Amazon's reply for a claimed order":
                cmd_mark_outcome()
            else:
                run_pipeline()
        except BackToMenu:
            print("\n==> Back to the main menu.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
