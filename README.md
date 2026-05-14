# devops-job-agent

Aggregates **DevOps Manager / Director** (and related) roles from several sources, dedupes against SQLite, writes an **Excel** digest, and optionally emails it via **Gmail SMTP**.

## Sources (pluggable)

| Source | Config key | Notes |
|--------|------------|--------|
| **Greenhouse** public API | `greenhouse_boards` | Board slug from URL `boards.greenhouse.io/{slug}` |
| **Lever** public API | `lever_sites` | Site name from `jobs.lever.co/{site}` |
| **RSS** | `rss_feeds` | e.g. We Work Remotely category feed |
| **SerpAPI** (optional) | `serpapi_features` in `config.json` | Turn on only what you need; needs `SERPAPI_KEY` in `.env` |

**Default:** every `serpapi_features.*` flag is **false** — no SerpAPI (Greenhouse + Lever + RSS only). Legacy configs without `serpapi_features` still use **`use_serpapi`** to toggle all built-in SerpAPI paths together.

**SerpAPI free tier** is often **~250 searches/month** shared by every app using that API key. To block all SerpAPI from this process, set **`JOB_AGENT_NO_SERPAPI=1`** in `.env`.

URLs are **normalized** (tracking query params stripped) for deduplication.

## Setup

```bash
cd devops-job-agent
python3 -m venv .venv && source .venv/bin/activate   # recommended
python3 -m pip install -r requirements.txt
cp .env.example .env
# Edit .env: EMAIL_USER, EMAIL_PASS, EMAIL_TO (Gmail app password)
```

If you see `ModuleNotFoundError` (e.g. `feedparser`), the interpreter you use to run `python run.py` does not have dependencies installed — run **`pip install -r requirements.txt`** in that same environment (or activate `.venv` first).

Optional: shared JSON settings (same as before):

- `GENIE4CV_SETTINGS` — path to `local.settings.json` (defaults to `~/genie4cv/local.settings.json`).

Tune targets in **`config.json`**: Greenhouse/Lever slugs, RSS URLs, scoring keywords, filters.

### Optional SerpAPI (per feature)

In **`config.json`**, set **`serpapi_features`** — only **true** keys consume quota:

| Key | What it enables |
|-----|------------------|
| `google_jobs` | SerpAPI Google Jobs (`job_agent/sources/google_jobs.py`) |
| `google_site_ats` | Google `site:` ATS / LinkedIn organic (`google_site_ats.py`; also set `ats_google_site_search.enabled` if you use templates) |
| `contacts` | LinkedIn profile hints via Google (`contacts.py`) |

Example — **only** LinkedIn contact search:

```json
"serpapi_features": {
  "google_jobs": false,
  "google_site_ats": false,
  "contacts": true
}
```

**Custom code:** add your own flag, e.g. `"my_scraper": true`, and call **`serpapi_try("my_scraper", params, cfg)`** from `job_agent/serpapi_optional.py` (returns `None` when off or no key).

**Legacy:** if **`serpapi_features`** is **omitted**, `use_serpapi: true` turns on all three built-ins above; `false` leaves them off.

Add **`SERPAPI_KEY`** (or **`GOOGLE_JOBS_API_KEY`**) to **`.env`**. Tune **`serpapi_location`**, **`serpapi_gl`**, **`serpapi_google_domain`**, **`serpapi_hl`** when using Google Jobs / web.

### Israel / geography (non-SerpAPI)

- **`filter_jobs_by_location_hint`** — when `true`, keeps **Greenhouse / Lever / RSS** rows only if **`location_hint`** or any **`location_hint_aliases`** substring appears in title/location/company.
- **`location_filter_source_prefixes`** — which `Job.source` prefixes the text filter applies to (default `greenhouse:`, `lever:`, `rss:`). Use **`[]`** to skip that text filter entirely.

Greenhouse/Lever are **global** APIs: they are **not** geo-scoped. For strict Israel-only rows, enable **`filter_jobs_by_location_hint`** and tune **`location_hint_aliases`**, or use boards/sites that post Israel roles.

## Run

```bash
# Safe test: no DB update, no email; optional empty DB to see fresh rows
python run.py --dry-run --skip-contacts --db /tmp/jagent-test.db

# Default: Greenhouse + Lever + RSS only (no SerpAPI)
python run.py

# Only some sources
python run.py --sources greenhouse,rss

# Legacy entry (same as run.py)
python script.py --dry-run --skip-contacts
```

### Flags

| Flag | Effect |
|------|--------|
| `--dry-run` | Build `jobs_<date>.xlsx` under `/tmp`, **no** `jobs.db` updates, **no** email |
| `--skip-contacts` | Skip LinkedIn contact search (even if `serpapi_features.contacts` is true) |
| `--sources a,b,c` | `serpapi`, `google_site_ats`, `greenhouse`, `lever`, `rss` (SerpAPI CLI names are ignored unless the matching `serpapi_features.*` is true) |
| `--config path` | Alternate `config.json` |
| `--db path` | Alternate SQLite file |

## CV

**Not required** for the current pipeline. Later you can add a `CV.pdf` / `profile.md` and use it for LLM-based fit scoring or outreach personalization.

## Legal / etiquette

- Respect **Greenhouse**, **Lever**, and site **terms of use**.
- **SerpAPI** (if enabled): respect SerpAPI and Google terms.
- **LinkedIn** (via SerpAPI search only when enabled): use public search results only; avoid logged-in scraping.

## Layout

```
devops-job-agent/
  run.py                 # preferred CLI
  script.py              # legacy shim → same CLI
  config.json
  requirements.txt
  job_agent/
    main.py              # orchestration + argparse
    settings.py          # .env + optional Genie JSON; serpapi_feature_enabled()
    serpapi_optional.py  # serpapi_try / serpapi_search for custom SerpAPI-only code
    ...
```
