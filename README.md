# Amazon Tax Refund for Freight Forwarder Exports

Amazon charges US sales tax on orders shipped to a Miami freight forwarder,
even though the goods are exported. Amazon refunds that tax when you email
tax-exempt@amazon.com with proof of export (see Amazon's rules:
[Tax Refunds on Freight Forward Exports](https://www.amazon.com/gp/help/customer/display.html?nodeId=202074690)).
This tool finds every order that
qualifies, downloads the proof-of-export invoice for each one from
**Speedy Logistics CR**, and writes the claim email for you.

It never sends anything. You review the email and send it yourself.

## Requirements

- Python 3 (standard library only)
- [`uv`](https://astral.sh/uv), used to run the Speedy client with its own
  dependencies (`requests`, `playwright`). On the first run it downloads
  Playwright's Chromium (~300 MB).
- A Speedy Logistics CR customer account

## Usage

1. Request your order history at
   <https://www.amazon.com/hz/privacy-central/data-requests/preview.html>
   (select **Your Orders**) and wait for Amazon's download email.
2. Save the `.zip` it links to in this folder. Don't extract it.
3. Run:

   ```sh
   python3 amazon_tax_refund.py
   ```

4. Pick **Run the claim pipeline** from the menu. It asks for your Speedy
   username and password only when there are orders to check and no recent
   session. Those credentials are typed into Speedy's real login page and
   never saved.
5. Open the new dated folder (e.g. `2026-09-24_10-15-00/`). It contains:
   - `email.txt`: the complete email (To, From, Subject and body with the
     numbered order list and total)
   - `invoices.zip`: the Speedy invoices to attach
6. Send the email with the zip attached.
7. Run the script again and pick **Mark orders as claimed** for that run,
   so those orders are never claimed twice.

## Menu

| Option | What it does |
| --- | --- |
| Run the claim pipeline | Imports the Amazon export, checks new orders against Speedy, writes `email.txt` and `invoices.zip` |
| Check status | Counts of claimed, ready, unchecked and no-proof orders |
| Mark orders as claimed | Records that you sent a run's email |
| Record Amazon's reply | Sets an order's outcome: refunded, rejected or more info requested |
| Purge all data | Deletes `orders.db`, `.cache/` and all run folders (asks you to type `purge`) |

## How orders are selected

An order goes into a claim when:

- it has sales tax,
- it isn't cancelled or possibly refunded,
- Speedy has a package with its tracking number (so it went through the
  forwarder), and
- it hasn't been claimed already.

Every order is tracked in `orders.db`, so each run only checks new orders with
Speedy. Orders Speedy didn't recognize are retried on the next run.

## Configuration

The first run asks a few questions and saves the answers to
`config/config.json` (git-ignored; see `config/config.example.json`). Edit it any time:

```json
{
  "to_email": "tax-exempt@amazon.com",
  "from_email": "you@example.com",
  "refund_method": "my original payment method or as Amazon Gift Card credit",
  "attachment_size_limit_mb": 25,
  "forwarder_name": "Speedy Logistics CR",
  "signature": "Your Name"
}
```

## Files

| Path | Contents |
| --- | --- |
| `amazon_tax_refund.py` | The interactive tool (start here) |
| `scripts/normalize_data_request_csv.py` | Turns Amazon's export into a clean orders CSV |
| `scripts/speedy_client.py` | Logs in to Speedy and downloads invoices |
| `scripts/build_claim.py` | Picks the orders to claim and totals the tax |
| `config/config.json` | Your settings |
| `orders.db` | Every order and its Speedy/claim status |
| `.cache/` | Temporary data, including downloaded invoices |
| `scripts/.speedy-session/` | Saved Speedy login cookies (~20 h) |
| `YYYY-MM-DD_HH-MM-SS/` | One folder per run: `email.txt` + `invoices.zip` |

## Known limitations

- **Speedy portal changes:** `speedy_client.py` replays the portal's internal
  request for its Amazon invoice button ("Generar factura Amazon"). When Speedy
  redeploys their site, that request can break and has to be captured again
  (see the notes at the top of that file).
- **Proof documents:** [Amazon's rules](https://www.amazon.com/gp/help/customer/display.html?nodeId=202074690) ask for a bill of lading or air waybill
  showing a destination outside the US. Speedy's invoice shows the order
  number, tracking number and Costa Rica as the destination, but it isn't a
  waybill. Amazon may ask for more.
- **Attachment size:** if `invoices.zip` is bigger than
  `attachment_size_limit_mb`, the script warns you. Split the claim into two
  emails.
