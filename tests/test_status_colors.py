"""Status label colors in digest email HTML."""

import unittest

import pandas as pd

from job_agent.excel_email import (
    _df_to_html_table,
    _REMOVED_JOBS_ROW_COLOR,
    _row_color_for_status,
    _status_cell_html,
    _status_label_html,
)


class TestStatusColors(unittest.TestCase):
    def test_colored_labels(self) -> None:
        self.assertIn('color:#1565c0', _status_label_html("In Progress"))
        self.assertIn('color:#2e7d32', _status_label_html("Interview"))
        self.assertIn('color:#c62828', _status_label_html("Rejected"))
        self.assertNotIn("color:", _status_label_html("New"))

    def test_status_cell_includes_colors(self) -> None:
        cfg = {
            "digest_remove": {"enabled": True},
            "job_tracker": {"enabled": True, "status_links_enabled": True},
        }
        html = _status_cell_html("https://example.com/j/1", "New", cfg)
        self.assertIn("In Progress", html)
        self.assertIn("#1565c0", html)
        self.assertIn("#2e7d32", html)
        self.assertIn("#c62828", html)

    def test_new_status_has_no_row_color(self) -> None:
        self.assertEqual(_row_color_for_status("New", {}), "")
        self.assertEqual(_row_color_for_status("", {}), "")
        self.assertEqual(_row_color_for_status("In Progress", {}), "#1565c0")

    def test_new_status_row_cells_unstyled(self) -> None:
        cfg = {
            "job_tracker": {"enabled": True, "status_links_enabled": True},
            "digest_remove": {"enabled": True, "secret": "test"},
        }
        df = pd.DataFrame(
            [
                {
                    "Job Title": "DevOps Lead",
                    "Company": "Acme",
                    "Link": "https://example.com/j/1",
                    "Source": "test",
                    "Location": "IL",
                    "Status": "New",
                }
            ]
        )
        cols = ["Job Title", "Company", "Link", "Source", "Location", "Status"]
        table = _df_to_html_table(df, cols, {}, cfg=cfg, table_action=None)
        self.assertIn("<td>DevOps Lead</td>", table)
        self.assertIn("<td>Acme</td>", table)
        self.assertNotIn('<td style="color:', table.split("<th>Status</th>")[0])
        self.assertIn("In Progress", table)
        self.assertIn("#1565c0", table)

    def test_row_text_color_for_status(self) -> None:
        cfg = {"job_tracker": {"enabled": True}, "digest_remove": {"enabled": False}}
        df = pd.DataFrame(
            [
                {
                    "Job Title": "DevOps Lead",
                    "Company": "Acme",
                    "Link": "https://example.com/j/1",
                    "Source": "test",
                    "Location": "IL",
                    "Status": "Interview",
                }
            ]
        )
        cols = ["Job Title", "Company", "Link", "Source", "Location", "Status"]
        table = _df_to_html_table(df, cols, {}, cfg=cfg, table_action=None)
        self.assertIn("color:#2e7d32", table)
        self.assertNotIn("background-color:#e8f5e9", table)
        self.assertGreaterEqual(table.count("color:#2e7d32"), len(cols))

    def test_removed_jobs_gray_text(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "Job Title": "DevOps Lead",
                    "Company": "Acme",
                    "Link": "https://example.com/j/removed",
                    "Source": "test",
                    "Location": "IL",
                }
            ]
        )
        cols = ["Job Title", "Company", "Link", "Source", "Location", "Restore"]
        table = _df_to_html_table(
            df,
            cols,
            {},
            cfg={},
            table_action="restore",
            fixed_row_color=_REMOVED_JOBS_ROW_COLOR,
        )
        self.assertIn(f"color:{_REMOVED_JOBS_ROW_COLOR}", table)
        self.assertGreaterEqual(table.count(f"color:{_REMOVED_JOBS_ROW_COLOR}"), len(cols))


if __name__ == "__main__":
    unittest.main()
