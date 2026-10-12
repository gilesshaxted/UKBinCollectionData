# Bin Portal server (v6)

Council-aware API behind the Bin Portal WordPress plugin.

- `GET  /resolve?postcode=`   postcode -> council (postcodes.io + each scraper's `LAD24CD` in `input.json`)
- `GET  /councils`            every council, what it needs, and whether this server can run it
- `POST /addresses`           postcode -> addresses/UPRNs (council adapters; add more in `sbd/addresses.py`)
- `POST /bins`                `{module, uprn, postcode, house_number}` -> `{bins, meta}`; cache-first, stale-while-revalidate
- `GET  /feed.ics?...`        calendar feed served from cache only
- `GET  /status`              counts, queue length, what is unsupported and why
- `POST /get_bins`, `/get_addresses`, `GET /get_councils`  kept so plugin v5.x keeps working

How it stays cheap: schedules are cached (24h, 72h for browser councils) and served stale for up to 14 days
while a single background worker refreshes them one at a time, with a gap per council and back-off on failure.
Refreshing stops for properties nobody has asked about in 30 days. OS-derived addresses are never stored.

Settings (environment variables): `SBD_DATA_DIR` (SQLite location; on Render's free tier it is wiped on restart),
`SELENIUM_URL` (enables councils that need a browser), `SBD_API_TOKEN` (optional shared secret, header `X-SBD-Token`),
`SBD_FRESH_TTL`, `SBD_MODULE_GAP`, `SBD_MAX_PARALLEL`, `SBD_RATE_LIMIT`.

Tests: `python -m pytest tests_sbd -q --noconftest -c /dev/null --rootdir=tests_sbd`
Self-hosting: `docker compose up -d` (add `--profile browser` for the Selenium sidecar).
