"""Tests for CV fit scoring."""

from pathlib import Path

from job_agent.cv_fit import (
    compute_cv_fit_percent,
    enrich_jobs_dataframe_with_cv_fit,
    format_cv_fit,
    job_description_text,
    load_cv_profile_text,
)
from job_agent.models import Job
import pandas as pd


def test_format_cv_fit():
    assert format_cv_fit(72) == "72%"
    assert format_cv_fit(None) == "NA"


def test_compute_cv_fit_greenhouse_job(tmp_path: Path):
    cv = """
    DevOps Manager with 10 years experience. Kubernetes, Terraform, AWS, CI/CD,
    platform engineering, team leadership, Python, monitoring.
    """
    job = Job(
        source="greenhouse:nice",
        company="Acme",
        title="DevOps Manager",
        location="Tel Aviv, Israel",
        link="https://boards.greenhouse.io/acme/jobs/123",
        raw={
            "text": (
                "We need a DevOps Manager for Kubernetes, Terraform, AWS, CI/CD, "
                "platform engineering, observability, and agile leadership."
            )
        },
    )
    cfg = {"cv_fit": {"min_job_text_chars": 50}, "scoring": {"keywords": ["DevOps"]}}
    pct = compute_cv_fit_percent(cv, job, cfg)
    assert pct is not None
    assert 40 <= pct <= 100


def test_compute_cv_fit_na_without_description():
    job = Job(
        source="linkedin_browser",
        company="X",
        title="DevOps Manager",
        location="Israel",
        link="https://www.linkedin.com/jobs/view/123",
        raw={},
    )
    cfg = {"cv_fit": {"min_job_text_chars": 200}}
    assert compute_cv_fit_percent("devops kubernetes terraform", job, cfg) is None


def test_enrich_dataframe(tmp_path: Path):
    cv_file = tmp_path / "CV.md"
    cv_file.write_text("DevOps Manager Kubernetes Terraform AWS CI/CD platform engineering", encoding="utf-8")
    job = Job(
        source="greenhouse:x",
        company="Acme",
        title="DevOps Director",
        location="Israel",
        link="https://boards.greenhouse.io/acme/jobs/1",
        raw={"text": "Kubernetes Terraform AWS DevOps director platform engineering required."},
    )
    cfg = {
        "_project_root": str(tmp_path),
        "cv_fit": {"enabled": True, "profile_path": "CV.md", "min_job_text_chars": 40},
    }
    df = pd.DataFrame([job.as_row()])
    out = enrich_jobs_dataframe_with_cv_fit(df, [job], cfg, root=tmp_path)
    assert out.iloc[0]["CV fit %"] != "NA"
    assert "%" in str(out.iloc[0]["CV fit %"])
