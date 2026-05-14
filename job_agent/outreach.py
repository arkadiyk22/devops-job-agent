from __future__ import annotations


def outreach_message(company: str, job_title: str, job_link: str = "") -> str:
    posting = f"\nPosting: {job_link.strip()}\n" if (job_link or "").strip() else "\n"
    return f"""Hi,

I came across the {job_title} role at {company} and it aligns closely with my background in leading DevOps/Platform teams.{posting}
I'd appreciate connecting and briefly discussing the role.

Thanks,
[Your Name]
"""
