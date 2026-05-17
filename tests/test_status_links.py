from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_agent.digest_remove import build_set_status_url, sign_action_token, verify_set_status_token
from job_agent.job_tracker_excel import (
    TRACKER_COL_LAST_UPDATED,
    allowed_status_values,
    ensure_tracker_status_has_timestamp,
    load_tracker_df,
    normalize_status_label,
    set_job_tracker_status,
)
import time
from job_agent.util import normalize_url


class StatusLinkTests(unittest.TestCase):
    def test_normalize_status_aliases(self) -> None:
        cfg = {"job_tracker": {"status_values": ["New", "In Progress", "Interview", "Rejected"]}}
        self.assertEqual(normalize_status_label("declined", cfg), "Rejected")
        self.assertEqual(normalize_status_label("in progress", cfg), "In Progress")

    def test_set_status_token_and_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {
                "_project_root": str(root),
                "digest_remove": {"secret": "s"},
                "job_tracker": {
                    "path": "job_tracker.xlsx",
                    "status_values": ["New", "In Progress", "Interview", "Rejected"],
                },
            }
            link = normalize_url("https://example.com/j/99")
            set_job_tracker_status(link, "New", cfg, root=root)
            token = sign_action_token(link, cfg, action="set_status", status="Interview")
            got_link, got_status, err = verify_set_status_token(token, cfg)
            self.assertIsNone(err)
            self.assertEqual(got_link, link)
            self.assertEqual(got_status, "Interview")
            self.assertIn("/status?t=", build_set_status_url(link, "Rejected", cfg))
            set_job_tracker_status(link, "Rejected", cfg, root=root)
            df = load_tracker_df(cfg, root=root)
            row = df[df["Link"] == link].iloc[0]
            self.assertEqual(row["Status"], "Rejected")

    def test_status_change_refreshes_last_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {
                "_project_root": str(root),
                "job_tracker": {
                    "path": "job_tracker.xlsx",
                    "status_values": ["New", "In Progress", "Interview", "Rejected"],
                },
            }
            link = normalize_url("https://example.com/j/1")
            set_job_tracker_status(link, "New", cfg, root=root)
            first = load_tracker_df(cfg, root=root).iloc[0][TRACKER_COL_LAST_UPDATED]
            time.sleep(1.1)
            set_job_tracker_status(link, "Interview", cfg, root=root)
            row = load_tracker_df(cfg, root=root).iloc[0]
            self.assertEqual(row["Status"], "Interview")
            second = str(row[TRACKER_COL_LAST_UPDATED])
            self.assertTrue(second)
            self.assertNotEqual(second, str(first))

    def test_backfill_status_without_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {
                "_project_root": str(root),
                "job_tracker": {
                    "path": "job_tracker.xlsx",
                    "status_values": ["New", "In Progress", "Interview", "Rejected"],
                },
            }
            from job_agent.job_tracker_excel import save_tracker_df
            import pandas as pd
            from job_agent.job_tracker_excel import job_tracker_columns

            df = pd.DataFrame([{
                "Job Title": "Head of DevOps",
                "Company": "Acme",
                "Network": "",
                "Link": "https://example.com/j/9",
                "Source": "test",
                "Location": "Israel",
                "Last updated": "",
                "Status": "Rejected",
            }])
            save_tracker_df(df, cfg, root=root)
            n = ensure_tracker_status_has_timestamp(cfg, root=root)
            self.assertEqual(n, 1)
            row = load_tracker_df(cfg, root=root).iloc[0]
            self.assertEqual(row["Status"], "Rejected")
            self.assertTrue(str(row[TRACKER_COL_LAST_UPDATED]))
