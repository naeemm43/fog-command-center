# FOG Industry Command Center

A three-tab interactive command center for the non-hazardous liquid waste / FOG (fats, oils, grease) industry, hosted on GitHub Pages and refreshed daily by a GitHub Actions cron.

- **News Feed** — recent industry / M&A headlines, filterable by category
- **Transaction Comps** — sortable, filterable database of M&A deals with CSV export
- **Interactive Map** — full FOG facility + WWTP map (Leaflet)

A daily GitHub Actions workflow runs `scripts/refresh_data.py`, which uses the Anthropic API's web-search tool to pull fresh news + deals, dedupe them, flag any deals near Tier 2 target markets, splice them back into `docs/index.html`, and push the commit. GitHub Pages redeploys automatically.

## Layout

```
fog-command-center/
├── .github/workflows/refresh.yml   # daily cron (07:00 CT)
├── docs/
│   └── index.html                  # served by GitHub Pages
├── scripts/
│   ├── build_index.py              # one-time: assemble docs/index.html
│   └── refresh_data.py             # daily: fetch news + splice into HTML
├── data/
│   ├── news_feed.json              # current news (last 180 days)
│   ├── comp_database.json          # M&A transaction database
│   └── news_archive.json           # older news, generated on first roll-off
├── requirements.txt
└── README.md
```

## First-time setup

### 1. Build `docs/index.html`

The build script reads the existing `fog_facility_map.html` (with its multi-megabyte FOG and WWTP data) and produces `docs/index.html`:

```bash
# defaults to ~/fog_map_project/fog_facility_map.html; override if needed
FOG_MAP_HTML=/path/to/fog_facility_map.html python scripts/build_index.py
```

The output is one self-contained HTML file (~8–15 MB). Open it locally to verify, then commit.

### 2. Push to GitHub

```bash
git init
git add .
git commit -m "Initial command center setup"
gh repo create fog-command-center --public --push
# or, without the GitHub CLI:
# create the repo on github.com, then:
# git remote add origin https://github.com/<you>/fog-command-center.git
# git push -u origin main
```

### 3. Add the API key as a repo secret

Repo → **Settings → Secrets and variables → Actions → New repository secret**

- Name: `ANTHROPIC_API_KEY`
- Value: your Anthropic API key

### 4. Enable GitHub Pages

Repo → **Settings → Pages**

- Source: **Deploy from a branch**
- Branch: `main`, folder: `/docs`

The site goes live at `https://<you>.github.io/fog-command-center/`.

### 5. Trigger a test refresh

Repo → **Actions → Daily Data Refresh → Run workflow**.

The job runs `scripts/refresh_data.py`. If the search returns new items, they are committed back to `main` and Pages redeploys.

## Daily operation

The cron fires twice (12:00 UTC and 13:00 UTC) so 07:00 Central is always covered across DST transitions. The script is idempotent — if nothing changed, the commit step is a no-op.

Cost: ~$0.10/day in Anthropic API usage (~$3/mo). Pages and Actions are free.

## Editing seed data

Both `data/news_feed.json` and `data/comp_database.json` are plain JSON. Edit, then either:

- run `python scripts/build_index.py` to rebuild the HTML from scratch, or
- run `python scripts/refresh_data.py` (it will splice the JSON into the HTML even if web search returns nothing new).

## Privacy

The site is publicly readable on GitHub Pages. The data inside is from public sources. The Anthropic API key only lives in GitHub Actions secrets — it is never written to the HTML or to any committed file.
