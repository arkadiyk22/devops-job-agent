"""Compare your CV/profile text to job descriptions for a digest «CV fit %» column."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import pandas as pd

from job_agent.job_page_details import fetch_greenhouse_job_content_http
from job_agent.models import Job
from job_agent.util import normalize_url, strip_html

CV_FIT_COLUMN = "CV fit %"
_DEFAULT_MIN_JOB_CHARS = 100
_MAX_JOB_CHARS = 12_000
_MAX_CV_CHARS = 80_000


def cv_fit_enabled(cfg: Dict[str, Any]) -> bool:
    block = cfg.get("cv_fit")
    if isinstance(block, dict) and "enabled" in block:
        return bool(block.get("enabled"))
    return False


def cv_fit_column_name(cfg: Dict[str, Any]) -> str:
    block = cfg.get("cv_fit")
    if isinstance(block, dict):
        name = str(block.get("column_name") or "").strip()
        if name:
            return name
    return CV_FIT_COLUMN


def resolve_cv_profile_path(root: Path, cfg: Dict[str, Any]) -> Optional[Path]:
    block = cfg.get("cv_fit") if isinstance(cfg.get("cv_fit"), dict) else {}
    raw = str(block.get("profile_path") or block.get("cv_path") or "").strip()
    candidates: List[Path] = []
    if raw:
        p = Path(raw).expanduser()
        candidates.append(p if p.is_absolute() else root / p)
    for name in ("CV.md", "profile.md", "CV.txt", "profile.txt", "CV.pdf", "resume.pdf"):
        candidates.append(root / name)
    for p in candidates:
        if p.is_file():
            return p.resolve()
    return None


def load_cv_profile_text(root: Path, cfg: Dict[str, Any]) -> str:
    path = resolve_cv_profile_path(root, cfg)
    if path is None:
        return ""
    suffix = path.suffix.lower()
    try:
        if suffix in (".md", ".txt", ".text"):
            return path.read_text(encoding="utf-8", errors="replace")[:_MAX_CV_CHARS]
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                try:
                    from PyPDF2 import PdfReader  # type: ignore
                except ImportError:
                    return ""
            reader = PdfReader(str(path))
            parts: List[str] = []
            for page in reader.pages[:40]:
                parts.append(page.extract_text() or "")
            return "\n".join(parts)[:_MAX_CV_CHARS]
    except OSError:
        return ""
    return ""


def _fit_term_list(cfg: Dict[str, Any]) -> List[str]:
    block = cfg.get("cv_fit") if isinstance(cfg.get("cv_fit"), dict) else {}
    extra = block.get("terms") or block.get("keywords") or []
    terms: List[str] = []
    if isinstance(extra, list):
        terms.extend(str(x).strip() for x in extra if str(x).strip())
    sc = cfg.get("scoring") if isinstance(cfg.get("scoring"), dict) else {}
    for key in ("keywords", "seniority"):
        for item in sc.get(key) or []:
            s = str(item).strip()
            if s:
                terms.append(s)
    for item in cfg.get("role_focus") or []:
        s = str(item).strip()
        if s:
            terms.append(s)
    # DevOps / leadership vocabulary commonly useful for overlap
    terms.extend(
        [
            "devops",
            "sre",
            "platform engineering",
            "kubernetes",
            "k8s",
            "terraform",
            "ci/cd",
            "cicd",
            "aws",
            "azure",
            "gcp",
            "linux",
            "python",
            "infrastructure",
            "cloud",
            "manager",
            "director",
            "head",
            "lead",
            "vp",
            "agile",
            "monitoring",
            "observability",
            "security",
            "helm",
            "docker",
            "git",
            "ansible",
            "דבאופס",
            "ענן",
            "מנהל",
        ]
    )
    seen: Set[str] = set()
    out: List[str] = []
    for t in terms:
        low = t.lower()
        if len(low) < 2 or low in seen:
            continue
        seen.add(low)
        out.append(t)
    return out


def _terms_in_text(terms: List[str], text: str) -> Set[str]:
    low = (text or "").lower()
    found: Set[str] = set()
    for t in terms:
        tl = t.lower()
        if len(tl) <= 3:
            if re.search(rf"\b{re.escape(tl)}\b", low):
                found.add(tl)
        elif tl in low:
            found.add(tl)
    return found


def job_description_text(job: Job, cfg: Dict[str, Any]) -> str:
    """Best-effort job description body for fit scoring."""
    block = cfg.get("cv_fit") if isinstance(cfg.get("cv_fit"), dict) else {}
    min_chars = int(block.get("min_job_text_chars") or _DEFAULT_MIN_JOB_CHARS)
    max_chars = int(block.get("max_job_text_chars") or _MAX_JOB_CHARS)

    parts = [job.title, job.company, job.location]
    raw = job.raw if isinstance(job.raw, dict) else {}
    for key in ("text", "description", "job_description", "snippet"):
        val = str(raw.get(key) or "").strip()
        if val:
            parts.append(strip_html(val))

    if "greenhouse.io" in (job.link or "").lower():
        gh = fetch_greenhouse_job_content_http(job.link)
        if gh:
            parts.append(strip_html(gh))

    combined = strip_html("\n".join(p for p in parts if p))
    combined = re.sub(r"\s+", " ", combined).strip()
    if len(combined) < min_chars:
        return ""
    return combined[:max_chars]


def compute_cv_fit_percent(cv_text: str, job: Job, cfg: Dict[str, Any]) -> Optional[int]:
    """
  Return 0–100 fit score, or None when scoring is not possible (→ show NA).

  Method: among skill/role terms that appear in your CV, what share also
  appear in the job title + description (overlap proxy, not an LLM judgment).
    """
    cv = (cv_text or "").strip()
    if not cv:
        return None

    job_body = job_description_text(job, cfg)
    if not job_body:
        return None

    terms = _fit_term_list(cfg)
    cv_terms = _terms_in_text(terms, cv)
    if not cv_terms:
        return None

    job_blob = f"{job.title}\n{job.company}\n{job_body}".lower()
    matched = sum(1 for t in cv_terms if t in job_blob or (len(t) > 3 and t in job_blob))
    # Also reward seniority alignment when title suggests leadership role
    title_low = (job.title or "").lower()
    senior_in_cv = any(x in cv_terms for x in ("manager", "director", "head", "lead", "vp", "מנהל"))
    senior_in_job = any(x in title_low for x in ("manager", "director", "head", "lead", "vp", "מנהל"))
    base = int(round(100 * matched / max(1, len(cv_terms))))
    if senior_in_cv and senior_in_job:
        base = min(100, base + 5)
    elif senior_in_cv != senior_in_job:
        base = max(0, base - 8)
    return max(0, min(100, base))


def format_cv_fit(value: Optional[int]) -> str:
    if value is None:
        return "NA"
    return f"{value}%"


def enrich_jobs_dataframe_with_cv_fit(
    df: pd.DataFrame,
    jobs: Iterable[Job],
    cfg: Dict[str, Any],
    *,
    root: Path,
) -> pd.DataFrame:
    col = cv_fit_column_name(cfg)
    out = df.copy()
    if not cv_fit_enabled(cfg):
        out[col] = ""
        return out

    cv_text = load_cv_profile_text(root, cfg)
    by_link = {normalize_url(j.link): j for j in jobs if j.link}
    values: List[str] = []
    for _, row in out.iterrows():
        link = normalize_url(str(row.get("Link") or "").strip())
        job = by_link.get(link)
        if not job or not cv_text:
            values.append("NA")
            continue
        values.append(format_cv_fit(compute_cv_fit_percent(cv_text, job, cfg)))
    out[col] = values
    return out
