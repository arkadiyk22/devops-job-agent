from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_agent.digest_remove import build_apply_yes_url, sign_action_token, verify_action_token
from job_agent.job_tracker_excel import (
    apply_tracker_to_digest_df,
    job_tracker_columns,
    load_tracker_df,
    record_job_apply,
    sync_digest_jobs_to_tracker,
    update_job_status,
)
from job_agent.util import normalize_url
import pandas as pd


class JobTrackerTests(unittest.TestCase):
    def test_apply_token(self) -> None:
        cfg = {"digest_remove": {"secret": "t"}, "_project_root": "/tmp"}
        link = "https://www.linkedin.com/jobs/view/1"
        token = sign_action_token(link, cfg, action="apply")
        got, err = verify_action_token(token, cfg, expected="apply")
        self.assertIsNone(err)
        self.assertEqual(got, normalize_url(link))
        self.assertIn("/apply?t=", build_apply_yes_url(link, cfg))

    def test_record_apply_and_email_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {
                "_project_root": str(root),
                "job_tracker": {"path": "job_tracker.xlsx"},
                "schedule": {"timezone": "Asia/Jerusalem"},
            }
            jobs = pd.DataFrame(
                [
                    {
                        "Job Title": "DevOps Manager",
                        "Company": "Acme",
                        "Network": "",
                        "Link": "https://example.com/j/1",
                        "Source": "test",
                        "Location": "Israel",
                    }
                ]
            )
            sync_digest_jobs_to_tracker(jobs, cfg, root=root)
            when = record_job_apply(
                "https://example.com/j/1",
                {"Job Title": "DevOps Manager", "Company": "Acme", "Link": "https://example.com/j/1"},
                cfg,
                root=root,
            )
            self.assertRegex(when, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            merged = apply_tracker_to_digest_df(jobs, cfg, root=root)
            self.assertEqual(merged.iloc[0]["Last updated"], when)
            self.assertEqual(merged.iloc[0]["Status"], "In Progress")
            tracker = load_tracker_df(cfg, root=root)
            self.assertIn("Last updated", tracker.columns)
            self.assertTrue(update_job_status("https://example.com/j/1", "Interview", cfg, root=root))
            merged2 = apply_tracker_to_digest_df(jobs, cfg, root=root)
            self.assertEqual(merged2.iloc[0]["Status"], "Interview")
            merged_new = apply_tracker_to_digest_df(
                pd.DataFrame([{"Link": "https://example.com/j/2", "Job Title": "X"}]),
                cfg,
                root=root,
            )
            self.assertEqual(merged_new.iloc[0]["Status"], "New")

    def test_tracker_columns_order(self) -> None:
        cols = job_tracker_columns()
        self.assertEqual(cols[-2:], ["Last updated", "Status"])
