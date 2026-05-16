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
    "Network",
    "Link",
    "Source",
    "Location",
)

_DEFAULT_EMAIL_HEADERS: Dict[str, str] = {
    "Job Title": "Job title",
    "Company": "Company",
    "Network": "Your connections at this company",
    "Link": "Job link / URL",
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


def _jobs_email_headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Display names for the digest jobs table only (does not rename DataFrame columns)."""
    headers = _email_table_headers(cfg)
    excel = cfg.get("excel_column_labels")
    if isinstance(excel, dict):
        for k in _EMAIL_JOB_COLUMNS:
            if k in excel and str(excel[k]).strip():
                headers[k] = str(excel[k]).strip()
    return headers


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
                cells.append(
                    f'<td style="word-break:break-all;"><a href="{esc(s, quote=True)}">{esc(s)}</a></td>'
                )
            elif c == "Network" and s:
                cells.append(f'<td style="font-size:13px;">{esc(s)}</td>')
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
            "Network relation",
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
                cells.append(
                    f'<td style="word-break:break-all;"><a href="{esc(s, quote=True)}">{esc(s)}</a></td>'
                )
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


def _stats_block_html(title: str, df: pd.DataFrame, caption: str = "") -> str:
    """Generic HTML table for fetch stats / per-source counts."""
    if df is None or df.empty:
        return ""
    esc = html.escape
    cols = [str(c) for c in df.columns]
    ths = "".join(f"<th>{esc(c)}</th>" for c in cols)
    trs: List[str] = []
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            val = row.get(c, "")
            s = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val)
            cells.append(f"<td>{esc(s)}</td>")
        trs.append("<tr>" + "".join(cells) + "</tr>")
    cap = (
        f"<p style=\"font-family:sans-serif;font-size:13px;color:#444;\">{esc(caption)}</p>"
        if caption
        else ""
    )
    return (
        f"<h2 style=\"font-family:sans-serif;\">{esc(title)}</h2>{cap}"
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:14px;">'
        f"<thead><tr>{ths}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    )


def _stats_block_plain(title: str, df: pd.DataFrame) -> List[str]:
    if df is None or df.empty:
        return []
    lines = [title, ""]
    cols = [str(c) for c in df.columns]
    lines.append("\t".join(cols))
    for _, row in df.iterrows():
        lines.append("\t".join(str(row.get(c, "")) for c in cols))
    lines.append("")
    return lines


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
                cells.append(
                    f'<td style="word-break:break-all;"><a href="{esc(s, quote=True)}">{esc(s)}</a></td>'
                )
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
    fetch_stats_df: pd.DataFrame,
    digest_by_source_df: pd.DataFrame,
    *,
    digest_note: str = "",
) -> str:
    # Keep canonical column names (Link, etc.); excel_column_labels is for Excel sheets
    # and for <th> text here only — renaming the frame would drop the Link column.
    headers = _jobs_email_headers(cfg)
    intro = (
        "<p style=\"font-family:sans-serif;font-size:14px;\">"
        "DevOps leadership roles (aggregated from configured sources). "
        "Jobs are listed in <strong>relevance order</strong> (title match to your scoring rules in config). "
        "<strong>Your connections at this company</strong> = LinkedIn «People you can reach out to» "
        "(1st-degree at that employer), or matches from your Connections.csv export if configured. "
        "Empty if LinkedIn shows no section, scrape failed, or you have not exported Connections.csv."
        "</p>"
    )
    if digest_note.strip():
        intro += (
            f"<p style=\"font-family:sans-serif;font-size:13px;color:#444;\">"
            f"{html.escape(digest_note.strip())}</p>"
        )
    only_new = bool(cfg.get("digest_email_only_new", False))
    cap_fetch = (
        "Fetched = jobs returned from that site after source-specific title/role filters (before Israel location filter). "
        "Unique added = how many job URLs were new to this run's combined list (dedupe across sites in one run). "
    )
    if only_new:
        cap_fetch += "The jobs table lists only URLs not sent in a previous digest."
    else:
        cap_fetch += "The jobs table lists all stored matches from your search (repeat listings included each digest)."
    if cfg.get("location_hint_strict_location_or_title") and cfg.get("filter_jobs_by_location_hint"):
        cap_fetch += (
            " **Israel (strict):** a location alias must appear in the job **title or location** line "
            "(not company name or job description alone), so US-only Greenhouse rows are dropped."
        )
    fetch_block = _stats_block_html(
        "Sources checked (this run)",
        fetch_stats_df,
        cap_fetch,
    )
    jobs_heading = "New jobs" if only_new else "Jobs in this digest"
    src_heading = "New jobs in this email (by source)" if only_new else "Jobs in this digest (by source)"
    digest_src_block = _stats_block_html(
        src_heading,
        digest_by_source_df,
        "Source is the internal job id (e.g. greenhouse:duolingo). Counts match the jobs table below.",
    )
    jobs_table = _df_to_html_table(jobs_df, _EMAIL_JOB_COLUMNS, headers)
    network_block = _network_html_table(network_df)
    contacts_block = _contacts_html_table(contacts_df)
    return (
        "<html><body>"
        f"{intro}{fetch_block}{digest_src_block}<h2 style=\"font-family:sans-serif;\">{html.escape(jobs_heading)}</h2>{jobs_table}"
        f"{network_block}{contacts_block}"
        "</body></html>"
    )


def _build_digest_plain(
    jobs_df: pd.DataFrame,
    contacts_df: pd.DataFrame,
    network_df: pd.DataFrame,
    fetch_stats_df: pd.DataFrame,
    digest_by_source_df: pd.DataFrame,
    cfg: Dict[str, Any] | None = None,
) -> str:
    lines: List[str] = [
        "DevOps leadership digest",
        "",
    ]
    lines += _stats_block_plain("Sources checked (this run):", fetch_stats_df)
    only_new = bool((cfg or {}).get("digest_email_only_new", False))
    src_heading = "New jobs in this email (by source):" if only_new else "Jobs in this digest (by source):"
    lines += _stats_block_plain(src_heading, digest_by_source_df)
    lines += [
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
    fetch_stats_df: Optional[pd.DataFrame] = None,
    digest_by_source_df: Optional[pd.DataFrame] = None,
    digest_note: str = "",
    attach_excel: bool = False,
    excel_path: Optional[Path] = None,
    subject: Optional[str] = None,
) -> None:
    """Send multipart digest: plain text + HTML table in body; optional .xlsx attachment."""
    cfg = cfg or {}
    jobs_df = jobs_df.copy()
    if not jobs_df.empty and "Score" in jobs_df.columns:
        jobs_df = jobs_df.sort_values("Score", ascending=False).reset_index(drop=True)
    net = network_df if network_df is not None else pd.DataFrame()
    fstats = fetch_stats_df if fetch_stats_df is not None else pd.DataFrame()
    dsrc = digest_by_source_df if digest_by_source_df is not None else pd.DataFrame()

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

    plain = _build_digest_plain(jobs_df, contacts_df, net, fstats, dsrc, cfg)
    if digest_note.strip():
        plain = f"{digest_note.strip()}\n\n{plain}"
    html_body = _build_digest_html(jobs_df, contacts_df, net, cfg, fstats, dsrc, digest_note=digest_note)

    msg = EmailMessage()
    msg["Subject"] = (subject or "").strip() or "DevOps Manager/Director roles — digest"
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
