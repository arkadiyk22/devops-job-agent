from __future__ import annotations

import html
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from job_agent.settings import get_setting

# Columns shown in the HTML email jobs table (no Recommended Search, Posted Date, or Score).
_EMAIL_JOB_COLUMNS: Sequence[str] = (
    "Job Title",
    "Company",
    "Link",
    "Source",
    "Location",
)

_DEFAULT_EMAIL_HEADERS: Dict[str, str] = {
    "Job Title": "Job title",
    "Company": "Company",
    "Link": "Apply link",
    "Source": "Source",
    "Location": "Location",
}


def _rename_job_columns(df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    labels = cfg.get("excel_column_labels")
    if not isinstance(labels, dict) or not labels:
        return df
    mapping = {str(k): str(v) for k, v in labels.items() if k in df.columns and str(v).strip()}
    return df.rename(columns=mapping) if mapping else df


def _email_table_headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    custom = cfg.get("email_table_headers")
    if isinstance(custom, dict):
        merged = dict(_DEFAULT_EMAIL_HEADERS)
        for k, v in custom.items():
            if k in _DEFAULT_EMAIL_HEADERS and isinstance(v, str) and v.strip():
                merged[str(k)] = v.strip()
        return merged
    return dict(_DEFAULT_EMAIL_HEADERS)


def _df_to_html_table(
    df: pd.DataFrame,
    columns: Sequence[str],
    header_map: Dict[str, str],
) -> str:
    if df.empty:
        return "<p><em>No rows.</em></p>"
    use = [c for c in columns if c in df.columns]
    if not use:
        return "<p><em>No columns.</em></p>"
    esc = html.escape
    ths = "".join(f"<th>{esc(header_map.get(c, c))}</th>" for c in use)
    trs: List[str] = []
    for _, row in df.iterrows():
        cells = []
        for c in use:
            val = row.get(c, "")
            s = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val)
            if c == "Link" and s.startswith("http"):
                cells.append(f'<td><a href="{esc(s, quote=True)}">Apply</a></td>')
            else:
                cells.append(f"<td>{esc(s)}</td>")
        trs.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:14px;">'
        f"<thead><tr>{ths}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    )


def _network_html_table(df: pd.DataFrame) -> str:
    if df.empty:
        return ""
    cols = [
        c
        for c in (
            "Connection",
            "Their company (export)",
            "Their role",
            "Company",
            "Job Title",
            "Job Link",
            "Profile",
        )
        if c in df.columns
    ]
    if not cols:
        return ""
    esc = html.escape
    ths = "".join(f"<th>{esc(c)}</th>" for c in cols)
    trs: List[str] = []
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            val = row.get(c, "")
            s = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val)
            if c in ("Job Link", "Profile") and s.startswith("http"):
                label = "Job" if c == "Job Link" else "Profile"
                cells.append(f'<td><a href="{esc(s, quote=True)}">{esc(label)}</a></td>')
            else:
                cells.append(f"<td>{esc(s)}</td>")
        trs.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<h2 style=\"font-family:sans-serif;\">Your network at these employers</h2>"
        "<p style=\"font-family:sans-serif;font-size:13px;color:#444;\">"
        "Matched from your offline LinkedIn <strong>Connections</strong> export (the employer "
        "text each person had in that file). People may have moved — confirm on LinkedIn "
        "before you reach out."
        "</p>"
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:14px;">'
        f"<thead><tr>{ths}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    )


def _contacts_html_table(df: pd.DataFrame) -> str:
    if df.empty:
        return ""
    cols = [c for c in ("Company", "Role Hint", "Job Link", "LinkedIn Profile", "Message") if c in df.columns]
    if not cols:
        return ""
    esc = html.escape
    ths = "".join(f"<th>{esc(c)}</th>" for c in cols)
    trs: List[str] = []
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            val = row.get(c, "")
            s = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val)
            if c in ("Job Link", "LinkedIn Profile") and s.startswith("http"):
                label = "Job" if c == "Job Link" else "Profile"
                cells.append(f'<td><a href="{esc(s, quote=True)}">{esc(label)}</a></td>')
            else:
                cells.append(f"<td>{esc(s)}</td>")
        trs.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<h2 style=\"font-family:sans-serif;\">Recruiter radar</h2>"
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:14px;">'
        f"<thead><tr>{ths}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    )


def _build_digest_html(
    jobs_df: pd.DataFrame,
    contacts_df: pd.DataFrame,
    network_df: pd.DataFrame,
    cfg: Dict[str, Any],
) -> str:
    jobs_view = _rename_job_columns(jobs_df, cfg)
    headers = _email_table_headers(cfg)
    intro = (
        "<p style=\"font-family:sans-serif;font-size:14px;\">"
        "New DevOps leadership roles (aggregated from configured sources). "
        "Jobs are listed in <strong>relevance order</strong> (title match to your scoring rules in config); "
        "there is no numeric score column in this email."
        "</p>"
    )
    jobs_table = _df_to_html_table(jobs_view, _EMAIL_JOB_COLUMNS, headers)
    network_block = _network_html_table(network_df)
    contacts_block = _contacts_html_table(contacts_df)
    return (
        "<html><body>"
        f"{intro}<h2 style=\"font-family:sans-serif;\">New jobs</h2>{jobs_table}"
        f"{network_block}{contacts_block}"
        "</body></html>"
    )


def _build_digest_plain(
    jobs_df: pd.DataFrame,
    contacts_df: pd.DataFrame,
    network_df: pd.DataFrame,
) -> str:
    lines: List[str] = [
        "DevOps leadership digest",
        "",
        "Jobs (relevance order — see config scoring; no score column in this mail):",
        "",
    ]
    cols = [c for c in _EMAIL_JOB_COLUMNS if c in jobs_df.columns]
    for _, row in jobs_df.iterrows():
        parts = [f"{c}: {row.get(c, '')}" for c in cols]
        lines.append(" | ".join(parts))
        lines.append("")
    if not network_df.empty:
        lines.append("Your network at these employers (from LinkedIn connections export):")
        for _, row in network_df.iterrows():
            lines.append(str(dict(row)))
            lines.append("")
    if not contacts_df.empty:
        lines.append("Contacts:")
        for _, row in contacts_df.iterrows():
            lines.append(str(dict(row)))
            lines.append("")
    return "\n".join(lines).strip()


def save_excel(
    jobs_df: pd.DataFrame,
    contacts_df: pd.DataFrame,
    out_dir: Path,
    cfg: Optional[Dict[str, Any]] = None,
    network_df: Optional[pd.DataFrame] = None,
) -> Path:
    cfg = cfg or {}
    sn = cfg.get("excel_sheet_names") or {}
    top_sheet = str(sn.get("top_jobs") or "Top Jobs")[:31]
    all_sheet = str(sn.get("all_jobs") or "All Jobs")[:31]
    contacts_sheet = str(sn.get("contacts") or "Contacts")[:31]
    network_sheet = str(sn.get("network") or "Your network")[:31]

    filename = out_dir / f"jobs_{datetime.now().date()}.xlsx"
    top = jobs_df.sort_values("Score", ascending=False).head(5)
    jobs_h = _rename_job_columns(jobs_df, cfg)
    top_h = _rename_job_columns(top, cfg)

    net = network_df if network_df is not None else pd.DataFrame()

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        top_h.to_excel(writer, sheet_name=top_sheet, index=False)
        jobs_h.to_excel(writer, sheet_name=all_sheet, index=False)
        contacts_df.to_excel(writer, sheet_name=contacts_sheet, index=False)
        if not net.empty:
            net.to_excel(writer, sheet_name=network_sheet, index=False)
    return filename


def send_digest_email(
    jobs_df: pd.DataFrame,
    contacts_df: pd.DataFrame,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    network_df: Optional[pd.DataFrame] = None,
    attach_excel: bool = False,
    excel_path: Optional[Path] = None,
) -> None:
    """Send multipart digest: plain text + HTML table in body; optional .xlsx attachment."""
    cfg = cfg or {}
    jobs_df = jobs_df.copy()
    if not jobs_df.empty and "Score" in jobs_df.columns:
        jobs_df = jobs_df.sort_values("Score", ascending=False).reset_index(drop=True)
    net = network_df if network_df is not None else pd.DataFrame()

    email_user = get_setting("EMAIL_USER", "GMAIL_EMAIL")
    email_pass = get_setting("EMAIL_PASS", "GMAIL_APP_PASSWORD")
    email_to = get_setting("EMAIL_TO", "SENDER_EMAIL", "GMAIL_EMAIL")

    if not email_user or not email_pass or not email_to:
        raise RuntimeError(
            "Missing email config: set EMAIL_USER, EMAIL_PASS, EMAIL_TO (or GMAIL_* in Genie settings)."
        )

    display = get_setting("EMAIL_FROM_DISPLAY", "EMAIL_DISPLAY_NAME").strip()
    if not display:
        display = str(cfg.get("email_from_display_name") or "").strip()
    if not display:
        display = "Job Agent"

    plain = _build_digest_plain(jobs_df, contacts_df, net)
    html_body = _build_digest_html(jobs_df, contacts_df, net, cfg)

    msg = EmailMessage()
    msg["Subject"] = "DevOps Manager/Director roles — digest"
    msg["From"] = formataddr((display, email_user))
    msg["To"] = email_to
    msg.set_content(plain)
    msg.add_alternative(html_body, subtype="html")

    if attach_excel and excel_path is not None and excel_path.is_file():
        data = excel_path.read_bytes()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=excel_path.name,
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(email_user, email_pass)
        smtp.send_message(msg)
