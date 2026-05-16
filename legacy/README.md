# Legacy search (v1)

Snapshot of the **pre–v2** job search design (archived when we started Israel-first redesign).

| File | What it was |
|------|-------------|
| `config.v1.json` | `config.json` as of archive date |
| `query_build.v1.py` | `job_agent/query_build.py` — boolean OR expansion, 19× `site:` queries, `geo_suffixes_israel_only`, etc. |

**Still active in the repo:** root `config.json` + `job_agent/query_build.py` (unchanged until you set `"search_version": 2` and we wire v2).

**New design:** see `docs/job-search-v2.md` and `config.v2.example.json`.

Do not delete this folder — it is the reference if we need to compare queries or roll back behavior.
