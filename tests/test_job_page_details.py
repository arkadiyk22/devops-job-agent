from __future__ import annotations

import unittest

from job_agent.job_page_details import (
    fallback_title_for_link,
    fetch_greenhouse_job_details_http,
)


class JobPageDetailsTests(unittest.TestCase):
    def test_greenhouse_url_parse(self) -> None:
        link = "https://boards.eu.greenhouse.io/nice/jobs/4846504101"
        d = fetch_greenhouse_job_details_http(link)
        self.assertTrue(d.get("title"))
        self.assertIn("nice", (d.get("source") or "").lower())

    def test_fallback_greenhouse(self) -> None:
        t = fallback_title_for_link("https://boards.eu.greenhouse.io/nice/jobs/123")
        self.assertIn("Greenhouse job 123", t)
