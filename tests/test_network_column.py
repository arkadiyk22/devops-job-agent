"""Tests for Network column on job digest."""

from job_agent.models import Job
from job_agent.network import (
    companies_match,
    enrich_jobs_dataframe_with_network,
    network_column_text,
    read_connections_csv,
)
import pandas as pd
from pathlib import Path
import tempfile
import csv


def test_companies_match_fuzzy():
    assert companies_match("CyberArk Software Ltd.", "CyberArk")
    assert companies_match("Sunbit", "Sunbit")


def test_network_column_text():
    job = Job(
        source="linkedin_browser",
        company="Sunbit",
        title="DevOps Manager",
        location="Tel Aviv",
        link="https://example.com/j/1",
    )
    conns = [
        {
            "name": "Alice Cohen",
            "position": "VP Engineering",
            "profile_url": "https://www.linkedin.com/in/alice",
            "connection_company": "Sunbit",
        },
        {
            "name": "Bob Levy",
            "position": "SRE Manager",
            "profile_url": "https://www.linkedin.com/in/bob",
            "connection_company": "Sunbit",
        },
    ]
    text = network_column_text(job, conns, max_people=8)
    assert "Alice Cohen" in text and "VP Engineering" in text
    assert "Bob Levy" in text


def test_read_connections_csv_minimal():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "Connections.csv"
        with p.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["First Name", "Last Name", "URL", "Company", "Position"])
            w.writerow(["A", "B", "https://www.linkedin.com/in/ab", "Acme", "Engineer"])
        rows = read_connections_csv(p)
        assert len(rows) == 1
        assert rows[0]["connection_company"] == "Acme"
