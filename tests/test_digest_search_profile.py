from job_agent.digest_search_profile import build_search_profile_df, build_search_profile_rows


def test_search_profile_includes_linkedin_and_scoring():
    cfg = {
        "location_hint": "Israel",
        "location_hint_aliases": ["tel aviv", "hybrid"],
        "linkedin": {
            "jobs_search": {
                "keywords": "devops manager OR sre manager",
                "location": "Israel",
            }
        },
        "role_focus": ["DevOps Manager"],
        "scoring": {"keywords": ["DevOps", "SRE"], "seniority": ["Manager"]},
        "greenhouse_boards": ["nice"],
        "google_web_browser": {"enabled": False},
        "ats_google_site_search": {"enabled": False},
    }
    rows = build_search_profile_rows(cfg)
    scopes = [r[0] for r in rows]
    assert any("LinkedIn" in s for s in scopes)
    assert any("Title keywords" in s for s in scopes)
    assert any("Greenhouse" in s for s in scopes)
    df = build_search_profile_df(cfg)
    assert list(df.columns) == ["Scope", "Keywords"]
    assert len(df) >= 4
