# Job search v2 — browser login (personal → product later)

**Direction:** No SerpAPI. Your **logged-in browser session** (Playwright + saved profile), same idea as WhatsApp Web: login once, reuse cookies.

## Personal use now

1. Copy config: `cp config.browser.example.json config.json`
2. Install browser automation:
   ```bash
   pip install playwright
   playwright install chromium
   ```
3. Log in once:
   ```bash
   python3 run.py --linkedin-login
   ```
4. Run searches:
   ```bash
   python3 run.py --print-queries   # shows LinkedIn Jobs URL
   python3 run.py                   # headless fetch → filter → email/db
   ```

Profile data lives under `~/.job-agent/browser/linkedin` (not in git).

## Architecture

```
config.json  (search_mode: browser)
       │
       ├── linkedin_browser.py   ← Playwright, persistent profile
       ├── rss / greenhouse / lever  ← optional, no login
       │
       ▼
filter → jobs.db → digest email
```

SerpAPI code remains in the repo for **legacy/v1** only (`search_version: 1`).

## Config keys

| Key | Purpose |
|-----|---------|
| `search_mode` | `"browser"` → no SerpAPI |
| `browser.user_data_dir` | Chromium profile root |
| `browser.headless` | `false` only for debugging |
| `linkedin.jobs_search.keywords` | LinkedIn Jobs search box |
| `linkedin.jobs_search.location` | e.g. `Israel` |
| `linkedin.jobs_search.max_pages` | Pagination / scroll rounds |

## Product / delivery later

Keep a clean split:

- `job_agent/browser/` — session + paths (reusable for Comeet, company portals, etc.)
- `job_agent/sources/linkedin_browser.py` — LinkedIn-specific selectors
- Config profiles: `personal` vs `hosted` (documented; not built yet)

Hosted delivery will need: consent, rate limits, selector maintenance, optional headed re-auth flow.

## Risks (you accept for personal use)

- LinkedIn may change the Jobs UI → update selectors in `linkedin_browser.py`.
- Automation with a personal account can trigger restrictions if run too aggressively.
- Do not commit `~/.job-agent/` or Cookies to git.

## Open next steps

- [ ] Add Israeli RSS feeds to config (when stable HTTPS feeds are found)
- [x] `--linkedin-headed` for debugging
- [x] `israel_or_il_signals` location filter
- [x] Greenhouse boards (`nice`, `taboola`) without login
- [ ] Second site (e.g. Comeet) with same browser profile pattern
- [ ] Contacts via browser (instead of SerpAPI Google)
