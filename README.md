# yorulmaz.nl

Personal dashboard at <https://yorulmaz.nl>, served by GitHub Pages from `main`.

## What's on it

- **Gold per gram, in euro** — fetched live in the browser from a public spot
  price API plus a public FX rate, no key and no backend.
- **Vanguard Global Stock Index Fund** (IE00BFPM9N11) — daily NAV, one per
  business day, refreshed by the workflow below.
- **Headlines** from Tweakers, NOS, RTL Nieuws, Security.NL, CNN and CNN Türk.
- **Trend lines** under both prices, drawn from the daily history kept in
  `data.json`.

Deliberately public: no personal data, no internal network details, no secrets.

## Files

| Path | Purpose |
|---|---|
| `index.html` | The dashboard. Single file, inline CSS/JS, no framework. |
| `data.json` | Generated. Fund NAV, performance figures and headlines. |
| `scripts/fetch_data.py` | Fetches every source, writes `data.json`. Stdlib only. |
| `.github/workflows/update-data.yml` | Runs the fetch weekdays at 11:30 CET, commits if changed. |
| `CNAME` | `yorulmaz.nl` |

## How the data gets refreshed

`fetch_data.py` makes exactly **one request per source, once per run**, with a
descriptive user agent and no retries. If a source fails, the previous values in
`data.json` are kept and marked stale rather than retried in a loop.

CNN stopped publishing RSS in April 2023, so its headlines come from
`lite.cnn.com`, CNN's own lightweight text site.

## Where the trend lines get their history

Both tiles keep a rolling history in `data.json`, extended by one point per run:

- **Gold** — seeded once from NBP, Poland's central bank, which publishes an
  official daily gold price per gram plus EUR/PLN mid rates, both keyless. After
  that it appends the daily spot snapshot.
- **Fund** — seeded once from ABN AMRO's own chart series, rescaled onto the
  current NAV (the series is exactly proportional; verified against their
  published 1-week return). After that it appends the daily NAV.

Seeding only happens while the stored history is short, so neither source is
polled for history on every run.

To refresh by hand: **Actions → Update dashboard data → Run workflow**.
