from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from job_agent import db as job_db
from job_agent.digest_remove import (
    _apply_restore,
    build_remove_yes_url,
    build_restore_url,
    sign_action_token,
    verify_action_token,
)
from job_agent.ignore_store import (
    add_removed_record,
    load_removed_records,
    load_stored_ignore_links,
    merge_ignore_links,
    record_to_job,
    restore_removed_link,
)
from job_agent.main import _apply_digest_ignore
from job_agent.models import Job
from job_agent.util import normalize_url


class DigestRemoveTests(unittest.TestCase):
    def test_sign_and_verify_remove_token(self) -> None:
        cfg = {"digest_remove": {"secret": "test-secret"}}
        link = "https://www.linkedin.com/jobs/view/123456/"
        token = sign_action_token(link, cfg, action="remove")
        got, err = verify_action_token(token, cfg, expected="remove")
        self.assertIsNone(err)
        self.assertEqual(got, normalize_url(link))

    def test_restore_token(self) -> None:
        cfg = {"digest_remove": {"secret": "test-secret"}}
        link = "https://example.com/job/1"
        token = sign_action_token(link, cfg, action="restore")
        got, err = verify_action_token(token, cfg, expected="restore")
        self.assertIsNone(err)
        self.assertEqual(got, normalize_url(link))
        self.assertIn("/restore?t=", build_restore_url(link, cfg))

    def test_removed_record_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ignore.json"
            cfg = {"digest_remove": {"ignore_store_path": str(path)}}
            job = Job("linkedin_browser", "Acme", "DevOps Manager", "Israel", "https://example.com/j/1", score=5)
            self.assertTrue(add_removed_record({"link": job.link, "title": job.title, "company": job.company}, cfg))
            self.assertFalse(add_removed_record({"link": job.link, "title": job.title}, cfg))
            links = load_stored_ignore_links(cfg)
            self.assertIn(normalize_url(job.link), links)
            restored = restore_removed_link(job.link, cfg)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["title"], "DevOps Manager")
            self.assertEqual(load_stored_ignore_links(cfg), set())

    def test_merge_ignore_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ignore.json"
            path.write_text(
                json.dumps({"removed": [{"link": normalize_url("https://stored.example/j/1")}]}),
                encoding="utf-8",
            )
            cfg = {
                "digest_ignore_links": ["https://config.example/j/2"],
                "digest_remove": {"ignore_store_path": str(path)},
            }
            merged = merge_ignore_links(cfg)
            self.assertIn(normalize_url("https://stored.example/j/1"), merged)
            self.assertIn(normalize_url("https://config.example/j/2"), merged)

    def test_apply_digest_ignore_filters_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ignore.json"
            link = "https://boards.greenhouse.io/nice/jobs/999"
            path.write_text(json.dumps({"removed": [{"link": normalize_url(link)}]}), encoding="utf-8")
            cfg = {"digest_remove": {"ignore_store_path": str(path)}}
            jobs = [
                Job("greenhouse:nice", "Nice", "DevOps Manager", "Israel", link),
                Job("greenhouse:nice", "Other", "DevOps Manager", "Israel", "https://example.com/other"),
            ]
            out = _apply_digest_ignore(jobs, cfg)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0].link, "https://example.com/other")

    def test_restore_clears_emailed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "jobs.db"
            ignore_path = root / "ignore.json"
            link = normalize_url("https://www.linkedin.com/jobs/view/999")
            cfg = {
                "_project_root": str(root),
                "digest_remove": {"ignore_store_path": str(ignore_path)},
                "digest_include_jobs_seen_within_days": 2,
            }
            job = Job("linkedin_browser", "NVIDIA", "Manager, AIOps", "Israel", link, score=9)
            conn = job_db.connect(db_path)
            job_db.upsert_jobs(conn, [job], mark_emailed=True)
            conn.close()
            add_removed_record({"link": link, "title": job.title, "company": job.company}, cfg)
            ok, _ = _apply_restore(link, cfg)
            self.assertTrue(ok)
            conn = job_db.connect(db_path)
            row = conn.execute("SELECT emailed_at FROM jobs WHERE link = ?", (link,)).fetchone()
            conn.close()
            self.assertIsNone(row[0])

    def test_legacy_links_array_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ignore.json"
            path.write_text(json.dumps({"links": ["https://legacy.example/j/1"]}), encoding="utf-8")
            cfg = {"digest_remove": {"ignore_store_path": str(path)}}
            recs = load_removed_records(cfg)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["link"], normalize_url("https://legacy.example/j/1"))


if __name__ == "__main__":
    unittest.main()
