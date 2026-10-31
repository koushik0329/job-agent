# Job Agent

Fetches jobs from free APIs every hour, shows them on a GitHub Pages dashboard (`docs/index.html`) and posts new matches to Discord.

- `fetch_jobs.py` - fetch, merge, filter, post to Discord (standard library only)
- `config.json` - sources and your Discord filters (keywords, remote only, locations)
- `.github/workflows/jobs.yml` - hourly GitHub Action
- `docs/` - the website (`index.html`) and its data (`jobs.json`)
- `data/` - bookkeeping (jobs already seen, last fetch time per source)

Secret needed: `DISCORD_WEBHOOK_URL` (repo Settings -> Secrets and variables -> Actions).

Local test (no Discord, nothing saved): `python3 fetch_jobs.py --dry-run --force`
