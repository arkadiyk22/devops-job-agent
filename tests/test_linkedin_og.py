from __future__ import annotations

import unittest

from job_agent.filters import filter_linkedin_post_jobs
from job_agent.linkedin_og import (
    hiring_signal_in_text,
    is_linkedin_post_url,
    matches_leadership_role_focus,
    parse_linkedin_hiring_title,
)
from job_agent.models import Job
from job_agent.sources.google_site_ats import _is_probable_job_url


class LinkedInOgTests(unittest.TestCase):
    def test_parse_hiring_og_title(self) -> None:
        parsed = parse_linkedin_hiring_title(
            "Acme Corp hiring DevOps Manager in Tel Aviv-Yafo, Israel | LinkedIn"
        )
        self.assertEqual(parsed["company"], "Acme Corp")
        self.assertEqual(parsed["title"], "DevOps Manager")
        self.assertIn("Tel Aviv", parsed["location"])

    def test_post_url_accepted(self) -> None:
        link = "https://www.linkedin.com/posts/acme_devops-manager-activity-1234567890-abcd"
        self.assertTrue(is_linkedin_post_url(link))
        self.assertTrue(_is_probable_job_url(link))

    def test_filter_linkedin_post_role(self) -> None:
        cfg = {
            "linkedin_posts": {"enabled": True, "require_hiring_signal": True, "filter_by_role_focus": True},
            "scoring": {"keywords": ["DevOps"], "seniority": ["Manager"]},
            "role_focus": ["DevOps Manager"],
        }
        good = Job(
            "google_browser:linkedin_post",
            "Acme",
            "DevOps Manager",
            "Israel",
            "https://www.linkedin.com/posts/foo-activity-1",
            raw={"text": "Acme hiring DevOps Manager in Israel"},
        )
        bad = Job(
            "google_browser:linkedin_post",
            "Other",
            "Marketing Intern",
            "Israel",
            "https://www.linkedin.com/posts/bar-activity-2",
            raw={"text": "We're hiring a marketing intern"},
        )
        out = filter_linkedin_post_jobs([good, bad], cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "DevOps Manager")

    def test_hiring_signal(self) -> None:
        self.assertTrue(hiring_signal_in_text("Foo hiring DevOps Manager in Israel | LinkedIn"))
        self.assertFalse(hiring_signal_in_text("Great conference recap from our team"))


if __name__ == "__main__":
    unittest.main()
