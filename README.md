# pc-flip-scanner

Runs on GitHub Actions twice a day (7:17 AM / 3:47 PM ET) or on demand (Actions → Flip scan → Run workflow).

- Scrapes Micro Center search results for each store in `config.json` (Charlotte 175, Fairfax 081).
- Opens every in-stock product page to confirm price + stock at that store. Unconfirmed = dropped.
- Margin = part-out value (config `part_resale`) − price × (1 + store tax).
- Output: run page summary (Actions tab), `report.html` (sortable, in the run's artifacts and committed to the repo), `data/latest.json`, `data/history/`.
- Push alerts via ntfy for margin ≥ `min_profit_alert`: install the ntfy app, subscribe to the topic in `config.json`.
- If Micro Center blocks the runner, the run fails with "NO DATA" and the pages it got back are in the run's `debug/` artifact.
