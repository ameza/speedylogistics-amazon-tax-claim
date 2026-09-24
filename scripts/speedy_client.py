#!/usr/bin/env python3
"""Speedy Logistics CR client -- talks to the customer portal's own
Next.js Server Action directly (no public REST API exists), replaying a
raw HTTP request captured from a real browser session.

*** Confirmed from a real HAR capture (2026-09-23) -- not a guess. ***
Two relevant things on https://app.speedylogisticscr.com/customer/packages:
  - A plain GET of the page (no Server Action, no special headers) embeds
    every package on the account -- id, trackingNumber, status, etc. --
    directly in the server-rendered payload. The "Página 1 de 10" pager
    shown in the UI is client-side only (confirmed: URL params like
    ?page=2 do nothing, and the row count already matches "N items /
    ~10 per page"), so this one GET is the account's whole package list,
    not a capped/recent window. `fetch_known_tracking_numbers()` below
    pulls the trackingNumber set out of it.
  - "Generar factura Amazon" (`generate_invoice()` below) is the action
    that takes a (trackingNumber, orderNumber) pair and returns the actual
    invoice PDF -- the list above doesn't include the PDF itself, so this
    call is still required for every order actually being claimed. But
    since the list already says which tracking numbers Speedy has, there's
    no need to call this action for the ones it doesn't -- empirically
    confirmed 1:1 (9/9 in initial testing: every tracking number present
    in the list succeeded here, every absent one was denied by it).
`cmd_generate_invoices()` fetches the list once, calls this action only for
tracking numbers found in it, and records everything else as denied
without a network round-trip. If the list fetch itself fails or looks
off, it falls back to checking every order individually (slower, but
never silently skips one on a bad assumption).

Auth: the portal uses NextAuth.js (confirmed via a GET to
/api/auth/session in the same capture, backed by an external
speedylogisticsapi.azurewebsites.net JWT issuer) with an httpOnly session
cookie. `login()` first tries NextAuth's standard Credentials-provider POST
directly via `requests` (no browser) -- this wasn't captured in a real HAR
(the capture this client was built from started after login), so the exact
field names are a best-effort guess, confirmed only if a follow-up
/api/auth/session call comes back genuinely signed in. If that doesn't pan
out, it falls back to a real Playwright browser the same way
amazon-orders-mcp's cookie_capture.py does: the user's own credentials,
read from SPEEDY_USERNAME/SPEEDY_PASSWORD environment variables and typed
into Speedy's real login form by the script -- never hardcoded. Either way the resulting session cookies are saved and
reused for the raw HTTP replay below. Cookies are short-lived per the
captured session (`accessTokenExpires: 86400` seconds -- about a day) but
NextAuth typically silently refreshes them on navigation within a live
browser; for this script's purposes, just re-run
`python3 speedy_client.py login` when calls start failing with 401.

*** KNOWN FRAGILITY: next-action / x-deployment-id below are specific to
Speedy's current Vercel deployment build and WILL go stale the next time
Speedy redeploys their frontend (Next.js ties Server Actions to a
build-specific hash). When that happens, calls will start failing --
see `SpeedyActionStale` below. Fix: re-capture a HAR the same way this one
was captured (DevTools Network tab -> click "Generar factura Amazon" on
a real package -> Copy as HAR / Copy as cURL) and update the constants.
This is an inherent trade-off of replaying a Server Action instead of
driving a real browser (Playwright) for this step.

Usage:
  export SPEEDY_USERNAME=...
  export SPEEDY_PASSWORD=...
  python3 speedy_client.py login
  python3 speedy_client.py generate-invoices \
    --orders amazon-export/orders.csv \
    --out speedy-matches.csv \
    --pdf-dir .cache/invoices/
"""
import argparse
import csv
import json
import os
import re
import sys
import time

import requests

BASE_URL = "https://app.speedylogisticscr.com"
PACKAGES_URL = f"{BASE_URL}/customer/packages"
LOGIN_URL = f"{BASE_URL}/signin"
COOKIE_JAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".speedy-session", "cookies.json")

# --- Captured from a real "Generar factura Amazon" request, 2026-09-23. ---
# See the fragility warning in the module docstring -- these are the two
# values most likely to need updating after a Speedy frontend redeploy.
NEXT_ACTION = "401defb86a5b06d44355ea94fbecc3811260cd4050"
X_DEPLOYMENT_ID = "dpl_8fKb3fcrG1L7DBmJqmu14sNm4jch"
NEXT_ROUTER_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(dashboard)%22%2C%7B%22children%22%3A"
    "%5B%22customer%22%2C%7B%22children%22%3A%5B%22packages%22%2C%7B%22children%22"
    "%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%2C4096%5D%7D%2Cnull%2Cnull%2C4096%5D"
    "%7D%2Cnull%2Cnull%2C4096%5D%7D%2Cnull%2Cnull%2C4096%5D%7D%2Cnull%2Cnull%2C4112%5D"
)
# -----------------------------------------------------------------------------


class SpeedyAuthError(RuntimeError):
    pass


class SpeedyActionStale(RuntimeError):
    """Raised when Speedy's response doesn't look like the expected RSC
    invoice payload -- most likely because next-action/x-deployment-id
    above are stale after a Speedy frontend redeploy. See the module
    docstring's fragility warning for how to fix."""

    pass


class SpeedyInvoiceDenied(RuntimeError):
    """Raised when Speedy responds successfully but declines to generate
    an invoice for this (tracking, order) pair -- e.g. no package on file
    with that tracking number for this account. This is the expected,
    non-error way we learn an order has no Speedy proof of export (hard
    rule #2), not a bug -- callers should catch this and record the order
    as excluded, not treat it as a script failure."""

    pass


def _cookie_header_from_jar():
    if not os.path.exists(COOKIE_JAR_PATH):
        raise SpeedyAuthError(
            f"No saved Speedy session at {COOKIE_JAR_PATH}. Run "
            "`python3 speedy_client.py login` first (needs SPEEDY_USERNAME / "
            "SPEEDY_PASSWORD environment variables and a real browser)."
        )
    with open(COOKIE_JAR_PATH) as f:
        cookies = json.load(f)
    return {c["name"]: c["value"] for c in cookies}


def _try_requests_login(username, password):
    """Best-effort NextAuth "Credentials" provider login via plain HTTP
    requests -- no browser. This is the standard NextAuth flow (GET
    /api/auth/csrf, then POST /api/auth/callback/credentials with that
    token), but the exact field names the Credentials provider's
    authorize() expects weren't captured in a real HAR (unlike the invoice
    action below), so this tries a couple of common spellings and only
    trusts the result if a follow-up /api/auth/session call actually comes
    back signed-in. Returns a `requests.Session` with a confirmed session
    on success, or None -- callers should fall back to the proven
    Playwright flow on None rather than trust a maybe.
    """
    for user_field in ("email", "username"):
        session = requests.Session()
        try:
            csrf_resp = session.get(f"{BASE_URL}/api/auth/csrf", timeout=15)
            csrf_resp.raise_for_status()
            csrf_token = csrf_resp.json().get("csrfToken")
            if not csrf_token:
                continue
            session.post(
                f"{BASE_URL}/api/auth/callback/credentials",
                data={
                    "csrfToken": csrf_token,
                    user_field: username,
                    "password": password,
                    "redirect": "false",
                    "json": "true",
                    "callbackUrl": f"{BASE_URL}/customer",
                },
                timeout=15,
            )
            check = session.get(f"{BASE_URL}/api/auth/session", timeout=15)
            payload = check.json() if check.ok else None
            if isinstance(payload, dict) and payload.get("user"):
                return session
        except (requests.RequestException, ValueError):
            pass
    return None


def login(username_env="SPEEDY_USERNAME", password_env="SPEEDY_PASSWORD"):
    """Log in and save session cookies for reuse by the raw-HTTP functions
    below. Tries a direct HTTP request first (_try_requests_login, no
    browser); only if that can't confirm a real session does it fall back
    to a real Playwright browser, mirroring amazon-orders-mcp's
    cookie_capture.py pattern -- credentials typed into Speedy's own form
    by this script, never hardcoded. Either way,
    credentials come only from the SPEEDY_USERNAME/SPEEDY_PASSWORD
    environment variables.
    """
    username = os.environ.get(username_env)
    password = os.environ.get(password_env)
    if not username or not password:
        sys.exit(
            f"Set {username_env} and {password_env} environment variables before "
            "running this. Never pass credentials as command-line arguments."
        )

    session = _try_requests_login(username, password)
    if session is not None:
        cookies = [{"name": name, "value": value} for name, value in session.cookies.get_dict().items()]
        os.makedirs(os.path.dirname(COOKIE_JAR_PATH), exist_ok=True)
        with open(COOKIE_JAR_PATH, "w") as f:
            json.dump(cookies, f)
        os.chmod(COOKIE_JAR_PATH, 0o600)
        print(f"Logged in via a direct HTTP request (no browser needed) -- saved {len(cookies)} cookies to {COOKIE_JAR_PATH}")
        return

    print("Direct HTTP login didn't confirm a session -- falling back to a real browser login...")
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        sys.exit(
            "Playwright not installed. Run: pip install playwright --break-system-packages "
            "&& playwright install chromium"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(LOGIN_URL)
        # TODO: verify these selectors against the real signin form the
        # first time this runs -- Speedy's exact field names/labels weren't
        # part of the HAR capture this client was built from (that capture
        # started after login). Adjust if the form doesn't match.
        page.fill('input[type="email"], input[name="email"], input[name="username"]', username)
        page.fill('input[type="password"], input[name="password"]', password)
        page.click('button[type="submit"]')
        try:
            # Speedy redirects to /customer (no trailing segment) on success --
            # match "left /signin" rather than a specific destination glob, so
            # this doesn't silently misfire if the exact redirect path changes.
            page.wait_for_url(lambda url: "/signin" not in url, timeout=30000)
        except PlaywrightTimeoutError:
            browser.close()
            sys.exit(
                "Speedy login did not complete -- still on the sign-in page after 30s. "
                "Usually an incorrect username/password, or Speedy asked for something "
                "(2FA, a CAPTCHA) in the browser window that needs a human -- never solve "
                "that yourself -- ask the user."
            )
        cookies = page.context.cookies()
        browser.close()

    session_cookies = [
        c for c in cookies
        if "next-auth" in c["name"].lower() or "session" in c["name"].lower()
    ]
    if not session_cookies:
        # Fall back to saving everything rather than guessing wrong and
        # silently saving nothing.
        session_cookies = cookies

    os.makedirs(os.path.dirname(COOKIE_JAR_PATH), exist_ok=True)
    with open(COOKIE_JAR_PATH, "w") as f:
        json.dump(session_cookies, f)
    os.chmod(COOKIE_JAR_PATH, 0o600)
    print(f"Saved {len(session_cookies)} cookies to {COOKIE_JAR_PATH}")


def _parse_rsc_invoice_response(raw: bytes):
    """Parse the React Server Components wire-format response from the
    invoice-generation action and pull out the embedded PDF.

    Confirmed shape from a real capture:
      0:{...}                                   (root/meta, ignored)
      1:{"status":"success","message":"...","data":"$B<hex>"}
      <hex>:["application/pdf","$<hex2>"]       (mime + ref to binary chunk)
      <hex2>:o<hexlen>,<raw PDF bytes>          (the binary chunk itself)

    Chunk IDs are assigned by the server per-response and are NOT fixed
    (don't assume "1", "2", "3" -- follow the "$B<hex>" reference).
    Raises SpeedyActionStale if the response doesn't match this shape at
    all (stale next-action), or SpeedyInvoiceDenied if it matches but
    status != "success" (e.g. no package found for this tracking number).
    """
    # Line 1 always carries the status/message/data-reference, and is
    # plain text (not binary), so it's safe to search as text.
    text_head = raw[:4096].decode("utf-8", errors="replace")
    m = re.search(r'1:\{"status":"(\w+)","message":"([^"]*)"(?:,"data":"\$B([0-9a-fA-F]+)")?', text_head)
    if not m:
        raise SpeedyActionStale(
            "Speedy's response didn't contain the expected status/message line. "
            "This usually means next-action/x-deployment-id in speedy_client.py "
            "are stale after a Speedy frontend redeploy -- re-capture a HAR and "
            "update them (see the module docstring). "
            f"First 300 bytes of response: {raw[:300]!r}"
        )
    status, message, blob_id_hex = m.group(1), m.group(2), m.group(3)
    if status != "success":
        raise SpeedyInvoiceDenied(f"Speedy declined to generate the invoice: {message!r}")
    if not blob_id_hex:
        raise SpeedyActionStale(
            f"Speedy reported success ({message!r}) but didn't include the expected "
            "PDF reference. Response shape may have changed -- re-capture a HAR."
        )

    # Find "<blob_id_hex>:[<mime>,"$<ref>"]" to learn the mime type and the
    # id of the chunk holding the actual bytes.
    meta_pat = re.compile(
        r"\n" + re.escape(blob_id_hex) + r':\["([^"]+)","\$([0-9a-fA-F]+)"\]'
    )
    meta_m = meta_pat.search(text_head) or meta_pat.search(raw.decode("utf-8", errors="replace"))
    if not meta_m:
        raise SpeedyActionStale(
            f"Couldn't find the blob metadata chunk '{blob_id_hex}:' in Speedy's response. "
            "Response shape may have changed -- re-capture a HAR."
        )
    mime_type, bin_id_hex = meta_m.group(1), meta_m.group(2)

    # Find "\n<bin_id_hex>:o<hexlen>," -- the binary chunk header -- then
    # read exactly hexlen bytes right after the comma. Must search the raw
    # bytes (not decoded text), since the payload itself is binary.
    header_pat = (f"\n{bin_id_hex}:o").encode("utf-8")
    idx = raw.find(header_pat)
    if idx == -1:
        raise SpeedyActionStale(
            f"Couldn't find binary chunk '{bin_id_hex}:o...' in Speedy's response. "
            "Response shape may have changed -- re-capture a HAR."
        )
    header_start = idx + len(header_pat)
    comma_idx = raw.find(b",", header_start)
    hexlen = raw[header_start:comma_idx]
    try:
        n = int(hexlen, 16)
    except ValueError:
        raise SpeedyActionStale(f"Couldn't parse binary chunk length {hexlen!r}.")
    payload_start = comma_idx + 1
    payload = raw[payload_start : payload_start + n]
    if len(payload) != n:
        raise SpeedyActionStale(
            f"Binary chunk was truncated: expected {n} bytes, got {len(payload)}."
        )
    return mime_type, payload


def generate_invoice(cookies, tracking_number, order_number, timeout=60):
    """Call Speedy's "Generar factura Amazon" Server Action for a single
    (tracking_number, order_number) pair. Returns (mime_type, pdf_bytes)
    on success. Raises SpeedyInvoiceDenied if Speedy has no matching
    package (this is the expected way to learn an order lacks Speedy
    proof of export), or SpeedyActionStale if the request/response shape
    itself has drifted (needs a fresh HAR capture, not a code bug).
    """
    body = json.dumps([{"trackingNumber": tracking_number, "orderNumber": order_number}])
    headers = {
        "accept": "text/x-component",
        # Deliberately NOT requesting brotli -- keeps decoding simple
        # (gzip/deflate are handled transparently by `requests`) without
        # needing the optional `brotli` package installed.
        "accept-encoding": "gzip, deflate",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": NEXT_ACTION,
        "next-router-state-tree": NEXT_ROUTER_STATE_TREE,
        "origin": BASE_URL,
        "referer": PACKAGES_URL,
        "x-deployment-id": X_DEPLOYMENT_ID,
    }
    resp = requests.post(PACKAGES_URL, headers=headers, cookies=cookies, data=body, timeout=timeout)
    if resp.status_code in (401, 403):
        raise SpeedyAuthError(
            f"Speedy returned {resp.status_code} -- the saved session has likely expired. "
            "Run `python3 speedy_client.py login` again."
        )
    if resp.status_code == 404:
        raise SpeedyActionStale(
            "Speedy returned 404 for the action call -- next-action is almost certainly "
            "stale after a redeploy. Re-capture a HAR and update speedy_client.py."
        )
    resp.raise_for_status()
    return _parse_rsc_invoice_response(resp.content)


def fetch_known_tracking_numbers(cookies):
    """GET the packages page once and pull out every trackingNumber Speedy
    has on file for this account (see the module docstring -- this is the
    account's whole package list, not a capped/recent window; no Server
    Action or pagination needed). Returns an uppercase set on success, or
    None if the fetch/parse doesn't look right -- callers should fall back
    to checking every order individually rather than trust a maybe.
    """
    try:
        resp = requests.get(PACKAGES_URL, cookies=cookies, timeout=30)
        if resp.status_code in (401, 403):
            raise SpeedyAuthError(
                f"Speedy returned {resp.status_code} fetching the packages list -- the saved "
                "session has likely expired. Run `python3 speedy_client.py login` again."
            )
        resp.raise_for_status()
    except SpeedyAuthError:
        raise
    except requests.RequestException:
        return None
    found = re.findall(r'\\"trackingNumber\\":\\"([^\\"]+)\\"', resp.text)
    if not found:
        return None
    return {t.upper() for t in found}


def cmd_login(args):
    login()


def _write_generate_invoices_output(args, results):
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Order ID", "Tracking", "Invoice PDF", "status"])
        w.writeheader()
        w.writerows(results)

    if args.matched_out:
        # Just the Order ID column, for orders that got a real invoice --
        # this is what build_claim.py's --matched expects. Deliberately a
        # separate, narrower file from --out (which keeps every attempt,
        # including denials, for the audit trail -- every order must be accounted for).
        with open(args.matched_out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["Order ID"])
            w.writeheader()
            seen = set()
            for r in results:
                if r["status"] == "success" and r["Order ID"] not in seen:
                    w.writerow({"Order ID": r["Order ID"]})
                    seen.add(r["Order ID"])


def cmd_generate_invoices(args):
    cookies = _cookie_header_from_jar()
    os.makedirs(args.pdf_dir, exist_ok=True)

    with open(args.orders, encoding="utf-8-sig") as f:
        orders = [o for o in csv.DictReader(f) if (o.get("Order ID") or "").strip()]
    total = len(orders)

    print("Fetching Speedy's package list to pre-filter (one request, no per-order calls)...", flush=True)
    known = fetch_known_tracking_numbers(cookies)
    if known is None:
        print(
            "  Couldn't get a usable package list -- falling back to checking every order "
            "individually (slower, but safe).",
            flush=True,
        )
        est_seconds = total * (args.delay + 0.5)  # rough: request latency + configured delay
        print(f"Checking {total} order(s) with Speedy, one request each (roughly {est_seconds / 60:.1f} min)...", flush=True)
    else:
        print(f"  Speedy has {len(known)} package(s) on file for this account.", flush=True)

    results = []
    try:
        for idx, o in enumerate(orders, 1):
            order_id = (o.get("Order ID") or "").strip()
            trackers = [t.strip() for t in (o.get("Tracking Numbers") or "").split(";") if t.strip()]
            prefix = f"  [{idx}/{total}] {order_id}"
            if not trackers:
                print(f"{prefix}: no tracking number -- skipped", flush=True)
                results.append({"Order ID": order_id, "Tracking": "", "Invoice PDF": "", "status": "no_tracking_number"})
                continue

            for raw_tracking in trackers:
                # Amazon's export sometimes wraps this as "CARRIER(TBA123...)" --
                # extract just the tracking number Speedy expects.
                m = re.search(r"\(([A-Z0-9]+)\)", raw_tracking.upper())
                tracking = m.group(1) if m else re.sub(r"[^A-Z0-9]", "", raw_tracking.upper())

                if known is not None and tracking not in known:
                    print(f"{prefix} ({tracking}): not in Speedy's package list -- skipped", flush=True)
                    results.append(
                        {"Order ID": order_id, "Tracking": tracking, "Invoice PDF": "", "status": "denied: not in Speedy's package list"}
                    )
                    continue

                try:
                    mime_type, pdf_bytes = generate_invoice(cookies, tracking, order_id)
                except SpeedyInvoiceDenied:
                    print(f"{prefix} ({tracking}): no proof on file", flush=True)
                    results.append(
                        {"Order ID": order_id, "Tracking": tracking, "Invoice PDF": "", "status": "denied: no package on file"}
                    )
                    continue
                except SpeedyAuthError:
                    raise  # Stop the whole run -- re-login and re-run.
                except SpeedyActionStale as e:
                    sys.exit(f"ERROR: {e}")

                ext = "pdf" if "pdf" in mime_type else mime_type.split("/")[-1]
                dest = os.path.join(args.pdf_dir, f"speedy_{order_id}_{tracking}.{ext}")
                with open(dest, "wb") as out:
                    out.write(pdf_bytes)
                print(f"{prefix} ({tracking}): invoice obtained", flush=True)
                results.append({"Order ID": order_id, "Tracking": tracking, "Invoice PDF": dest, "status": "success"})
                time.sleep(args.delay)
    except KeyboardInterrupt:
        _write_generate_invoices_output(args, results)
        sys.exit(
            f"\nStopped after {len(results)}/{total} order(s) -- progress so far was saved to "
            f"{args.out}. Re-run generate-invoices to pick up where this left off "
            "(orders already checked won't need re-checking)."
        )

    _write_generate_invoices_output(args, results)

    n_success = sum(1 for r in results if r["status"] == "success")
    print(f"Generated {n_success}/{len(results)} invoices. Wrote {args.out}")
    if n_success < len(results):
        print(
            "Orders without a successful invoice have no Speedy proof of export and "
            "must be excluded from the claim -- see the 'status' column.",
            file=sys.stderr,
        )
    if args.matched_out:
        print(f"Wrote matched order IDs to {args.matched_out}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="One-time (roughly daily) browser login, saves session cookies")
    p_login.set_defaults(func=cmd_login)

    p_gen = sub.add_parser(
        "generate-invoices",
        help="Attempt Speedy invoice generation for every order in a canonical orders.csv",
    )
    p_gen.add_argument("--orders", required=True, help="Canonical orders.csv (from normalize_data_request_csv.py)")
    p_gen.add_argument("--out", required=True, help="Output CSV: Order ID, Tracking, Invoice PDF, status (full audit trail, incl. denials)")
    p_gen.add_argument("--matched-out", help="Optional output CSV of just successful Order IDs, ready for build_claim.py --matched")
    p_gen.add_argument("--pdf-dir", required=True, help="Directory to save downloaded invoice PDFs")
    p_gen.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between requests (default 1.0)")
    p_gen.set_defaults(func=cmd_generate_invoices)

    args = ap.parse_args()
    try:
        args.func(args)
    except (SpeedyAuthError,) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
