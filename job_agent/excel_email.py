from __future__ import annotations

import html
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Set

import pandas as pd

from job_agent.digest_remove import (
    build_remove_yes_url,
    build_restore_url,
    build_set_status_url,
    digest_remove_enabled,
    job_tracker_digest_columns_enabled,
)
from job_agent.cv_fit import CV_FIT_COLUMN
from job_agent.digest_search_profile import build_search_profile_with_fetch_stats_df
from job_agent.ignore_store import merge_ignore_links
DigestTableAction = Literal["remove", "restore"]
from job_agent.settings import get_setting
from job_agent.util import normalize_url

# Columns shown in the HTML email jobs table (no Recommended Search, Posted Date, or Score).
_EMAIL_JOB_COLUMNS: Sequence[str] = (
    "Job Title",
    "Company",
    "Network",
    "Link",
    "Source",
    "Location",
    CV_FIT_COLUMN,
)

# Removed-jobs subsection (and removed-only email): no Network column.
_REMOVED_JOBS_EMAIL_COLUMNS: Sequence[str] = tuple(
    c for c in _EMAIL_JOB_COLUMNS if c != "Network"
)

# Same columns as the digest email jobs table (shared with job_tracker.xlsx).
DIGEST_JOB_TABLE_COLUMNS: Sequence[str] = _EMAIL_JOB_COLUMNS

TRACKER_COL_LAST_UPDATED = "Last updated"
JOB_TRACKER_EXTRA_COLUMNS: Sequence[str] = (TRACKER_COL_LAST_UPDATED, "Status")

# Shown in digest email after core job columns (before Remove).
DIGEST_TRACKER_EMAIL_COLUMNS: Sequence[str] = (TRACKER_COL_LAST_UPDATED, "Status")

_DEFAULT_EMAIL_HEADERS: Dict[str, str] = {
    "Job Title": "Job title",
    "Company": "Company",
    "Network": "Your connections at this company",
    "Link": "Job link / URL",
    "Source": "Source",
    "Location": "Location",
    CV_FIT_COLUMN: "CV fit",
    "Remove": "Remove",
    "Restore": "Restore",
    TRACKER_COL_LAST_UPDATED: "Last updated",
    "Status": "Status",
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
        for k in list(_EMAIL_JOB_COLUMNS) + list(DIGEST_TRACKER_EMAIL_COLUMNS):
            if k in excel and str(excel[k]).strip():
                headers[k] = str(excel[k]).strip()
    return headers


# Digest Status: In Progress blue, Interview green, Rejected red.
_STATUS_COLORS: Dict[str, str] = {
    "in progress": "#1565c0",
    "interview": "#2e7d32",
    "rejected": "#c62828",
}
# Removed jobs subsection: uniform gray text.
_REMOVED_JOBS_ROW_COLOR = "#757575"


def _status_color(label: str) -> str:
    return _STATUS_COLORS.get((label or "").strip().lower(), "")


def _is_new_status(label: str, cfg: Dict[str, Any] | None = None) -> bool:
    s = (label or "").strip().lower()
    if not s or s == "new":
        return True
    if cfg:
        from job_agent.job_tracker_excel import status_default_new

        return s == status_default_new(cfg).strip().lower()
    return False


def _row_color_for_status(status: str, cfg: Dict[str, Any] | None = None) -> str:
    """Row text color only for In Progress / Interview / Rejected; New stays default."""
    if _is_new_status(status, cfg):
        return ""
    return _status_color(status)


def _td_style(*, color: str = "", extra: str = "") -> str:
    parts: List[str] = []
    if color:
        parts.append(f"color:{color};")
    if extra:
        parts.append(extra if extra.endswith(";") else f"{extra};")
    return f' style="{"".join(parts)}"' if parts else ""


def _link_html(url: str, label: str, *, color: str = "", extra_style: str = "") -> str:
    esc = html.escape
    style = extra_style
    if color:
        style = f"{style}color:{color};" if style else f"color:{color};"
    style_attr = f' style="{style}"' if style else ""
    return f'<a href="{esc(url, quote=True)}"{style_attr}>{esc(label)}</a>'


def _status_label_html(label: str, *, link: str = "") -> str:
    esc = html.escape
    text = esc(label)
    color = _status_color(label)
    if link:
        style = "font-size:12px;font-weight:600;"
        if color:
            style += f"color:{color};"
        return f'<a href="{esc(link, quote=True)}" style="{style}">{text}</a>'
    if color:
        return f'<strong style="color:{color};">{text}</strong>'
    return f"<strong>{text}</strong>"


def _status_cell_html(
    link: str, current: str, cfg: Dict[str, Any], *, row_color: str = ""
) -> str:
    from job_agent.job_tracker_excel import allowed_status_values, status_links_enabled

    if not link:
        return f"<td{_td_style(color=row_color)}>—</td>"
    current_norm = (current or "").strip() or "New"
    # New: default text on the cell; only the quick-set links keep their own colors.
    cell_color = "" if _is_new_status(current_norm, cfg) else row_color
    parts = [_status_label_html(current_norm)]
    if status_links_enabled(cfg) and digest_remove_enabled(cfg):
        choices = allowed_status_values(cfg)
        links: List[str] = []
        for label in choices:
            if label.lower() == current_norm.lower():
                continue
            url = build_set_status_url(link, label, cfg)
            links.append(_status_label_html(label, link=url))
        if links:
            parts.append(
                '<div style="margin-top:4px;font-size:12px;">'
                + " · ".join(links)
                + "</div>"
            )
    return (
        f"<td{_td_style(color=cell_color, extra='font-size:13px;vertical-align:top;')}>"
        f"{''.join(parts)}</td>"
    )


def _action_cell_html(
    link: str, cfg: Dict[str, Any], action: DigestTableAction, *, row_color: str = ""
) -> str:
    if not digest_remove_enabled(cfg) or not link:
        return f"<td{_td_style(color=row_color, extra='text-align:center;')}>—</td>"
    if action == "remove":
        url = build_remove_yes_url(link, cfg)
        label = "Yes"
    else:
        url = build_restore_url(link, cfg)
        label = "Restore"
    return (
        f"<td{_td_style(color=row_color, extra='text-align:center;white-space:nowrap;')}>"
        f'{_link_html(url, label, color=row_color, extra_style="font-size:13px;")}'
        "</td>"
    )


def _df_to_html_table(
    df: pd.DataFrame,
    columns: Sequence[str],
    header_map: Dict[str, str],
    *,
    cfg: Dict[str, Any] | None = None,
    table_action: DigestTableAction | None = "remove",
    fixed_row_color: str = "",
) -> str:
    if df.empty:
        return "<p><em>No rows.</em></p>"
    action_col = "Restore" if table_action == "restore" else "Remove"
    use = [
        c
        for c in columns
        if c in df.columns
        or (table_action and c == action_col)
        or (c == TRACKER_COL_LAST_UPDATED and job_tracker_digest_columns_enabled(cfg))
        or (c == "Status" and job_tracker_digest_columns_enabled(cfg))
    ]
    if not use:
        return "<p><em>No columns.</em></p>"
    esc = html.escape
    cfg = cfg or {}
    ths = "".join(f"<th>{esc(header_map.get(c, c))}</th>" for c in use)
    trs: List[str] = []
    for _, row in df.iterrows():
        cells = []
        link_val = str(row.get("Link", "") or "")
        row_status = ""
        if fixed_row_color:
            row_color = fixed_row_color
        else:
            if job_tracker_digest_columns_enabled(cfg) and "Status" in df.columns:
                row_status = str(row.get("Status", "") or "").strip() or "New"
            row_color = _row_color_for_status(row_status, cfg)
        for c in use:
            if c == TRACKER_COL_LAST_UPDATED and job_tracker_digest_columns_enabled(cfg):
                updated = str(row.get(TRACKER_COL_LAST_UPDATED, "") or "").strip()
                if updated:
                    cells.append(
                        f"<td{_td_style(color=row_color, extra='white-space:nowrap;')}>{esc(updated)}</td>"
                    )
                else:
                    cells.append(f"<td{_td_style(color=row_color, extra='text-align:center;')}>—</td>")
                continue
            if c == "Status" and job_tracker_digest_columns_enabled(cfg):
                cur = str(row.get("Status", "") or "").strip()
                cells.append(_status_cell_html(link_val, cur, cfg, row_color=row_color))
                continue
            if c in ("Remove", "Restore") and table_action:
                cells.append(_action_cell_html(link_val, cfg, table_action, row_color=row_color))
                continue
            val = row.get(c, "")
            s = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val)
            if c == "Link" and s.startswith("http"):
                cells.append(
                    f"<td{_td_style(color=row_color, extra='word-break:break-all;')}>"
                    f"{_link_html(s, s, color=row_color)}</td>"
                )
            elif c == "Network" and s:
                cells.append(f"<td{_td_style(color=row_color, extra='font-size:13px;')}>{esc(s)}</td>")
            else:
                cells.append(f"<td{_td_style(color=row_color)}>{esc(s)}</td>")
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


def _include_removed_table_in_digest(cfg: Dict[str, Any], table_action: DigestTableAction | None) -> bool:
    if table_action == "restore":
        return False
    if not digest_remove_enabled(cfg):
        return False
    if "digest_email_include_removed_table" in cfg:
        return bool(cfg.get("digest_email_include_removed_table"))
    return True


def _removed_jobs_email_block(
    removed_jobs_df: pd.DataFrame,
    cfg: Dict[str, Any],
    headers: Dict[str, str],
) -> str:
    if removed_jobs_df is None or removed_jobs_df.empty:
        return ""
    restore_cols = [c for c in _REMOVED_JOBS_EMAIL_COLUMNS if c in removed_jobs_df.columns] + ["Restore"]
    table = _df_to_html_table(
        removed_jobs_df,
        restore_cols,
        headers,
        cfg=cfg,
        table_action="restore",
        fixed_row_color=_REMOVED_JOBS_ROW_COLOR,
    )
    return (
        "<h2 style=\"font-family:sans-serif;\">Removed jobs</h2>"
        "<p style=\"font-family:sans-serif;font-size:13px;color:#444;\">"
        "Hidden from future digests. Click <strong>Restore</strong> to bring a job back."
        "</p>"
        f"{table}"
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
    table_action: DigestTableAction | None = "remove",
    removed_jobs_df: Optional[pd.DataFrame] = None,
) -> str:
    # Keep canonical column names (Link, etc.); excel_column_labels is for Excel sheets
    # and for <th> text here only — renaming the frame would drop the Link column.
    headers = _jobs_email_headers(cfg)
    intro = ""
    custom_intro = cfg.get("digest_email_intro_html")
    if isinstance(custom_intro, str) and custom_intro.strip():
        intro = f"<p style=\"font-family:sans-serif;font-size:14px;\">{custom_intro.strip()}</p>"
    if digest_note.strip():
        intro += (
            f"<p style=\"font-family:sans-serif;font-size:14px;\">"
            f"{html.escape(digest_note.strip())}</p>"
        )
    only_new = bool(cfg.get("digest_email_only_new", False))
    search_block = ""
    if table_action != "restore":
        profile_df = build_search_profile_with_fetch_stats_df(cfg, fetch_stats_df)
        if not profile_df.empty:
            search_block = _stats_block_html(
                "Search profile",
                profile_df,
                "Unique added = new job links from this run for that source (— = filter or not fetched).",
            )
    fetch_block = ""
    if table_action == "restore":
        jobs_heading = "Removed jobs"
    else:
        jobs_heading = "New jobs" if only_new else "Jobs in this digest"
    action_col = "Restore" if table_action == "restore" else "Remove"
    email_cols = list(
        _REMOVED_JOBS_EMAIL_COLUMNS if table_action == "restore" else _EMAIL_JOB_COLUMNS
    )
    if table_action != "restore" and job_tracker_digest_columns_enabled(cfg):
        email_cols += [c for c in DIGEST_TRACKER_EMAIL_COLUMNS]
    email_cols += [action_col] if digest_remove_enabled(cfg) and table_action else []
    jobs_table = _df_to_html_table(
        jobs_df,
        email_cols,
        headers,
        cfg=cfg,
        table_action=table_action,
    )
    removed_block = ""
    if _include_removed_table_in_digest(cfg, table_action):
        rdf = removed_jobs_df if removed_jobs_df is not None else pd.DataFrame()
        removed_block = _removed_jobs_email_block(rdf, cfg, headers)
    jobs_block = (
        f"<h2 style=\"font-family:sans-serif;\">{html.escape(jobs_heading)}</h2>{jobs_table}"
    )
    network_block = _network_html_table(network_df)
    contacts_block = _contacts_html_table(contacts_df)
    return (
        "<html><body>"
        f"{intro}{jobs_block}{removed_block}{search_block}{fetch_block}"
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
    table_action: DigestTableAction | None = "remove",
    removed_jobs_df: Optional[pd.DataFrame] = None,
) -> str:
    lines: List[str] = [
        "DevOps leadership digest",
        "",
    ]
    lines += [
        "Jobs (sorted by company name):",
        "",
    ]
    job_cols = _REMOVED_JOBS_EMAIL_COLUMNS if table_action == "restore" else _EMAIL_JOB_COLUMNS
    cols = [c for c in job_cols if c in jobs_df.columns]
    if table_action != "restore" and job_tracker_digest_columns_enabled(cfg or {}):
        cols += [c for c in DIGEST_TRACKER_EMAIL_COLUMNS if c in jobs_df.columns]
    for _, row in jobs_df.iterrows():
        parts = [f"{c}: {row.get(c, '')}" for c in cols]
        link = str(row.get("Link", "") or "")
        if digest_remove_enabled(cfg or {}) and link and table_action == "remove":
            parts.append(f"Remove: Yes — {build_remove_yes_url(link, cfg or {})}")
        elif digest_remove_enabled(cfg or {}) and link and table_action == "restore":
            parts.append(f"Restore — {build_restore_url(link, cfg or {})}")
        lines.append(" | ".join(parts))
        lines.append("")
    cfg_eff = cfg or {}
    if _include_removed_table_in_digest(cfg_eff, table_action) and removed_jobs_df is not None and not removed_jobs_df.empty:
        lines.append("Removed jobs (click Restore in HTML mail):")
        lines.append("")
        rcols = [c for c in _REMOVED_JOBS_EMAIL_COLUMNS if c in removed_jobs_df.columns]
        for _, row in removed_jobs_df.iterrows():
            parts = [f"{c}: {row.get(c, '')}" for c in rcols]
            link = str(row.get("Link", "") or "")
            if digest_remove_enabled(cfg_eff) and link:
                parts.append(f"Restore — {build_restore_url(link, cfg_eff)}")
            lines.append(" | ".join(parts))
            lines.append("")
    if table_action != "restore":
        profile_df = build_search_profile_with_fetch_stats_df(cfg_eff, fetch_stats_df)
        if not profile_df.empty:
            lines += _stats_block_plain("Search profile:", profile_df)
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


def _sort_digest_jobs_by_company(jobs_df: pd.DataFrame) -> pd.DataFrame:
    if jobs_df.empty or "Company" not in jobs_df.columns:
        return jobs_df
    by = ["Company"]
    if "Job Title" in jobs_df.columns:
        by.append("Job Title")
    return (
        jobs_df.sort_values(
            by,
            key=lambda col: col.astype(str).str.strip().str.casefold(),
            ascending=True,
            na_position="last",
        )
        .reset_index(drop=True)
    )


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
    table_action: DigestTableAction | None = "remove",
    removed_jobs_df: Optional[pd.DataFrame] = None,
) -> None:
    """Send multipart digest: plain text + HTML table in body; optional .xlsx attachment."""
    cfg = cfg or {}
    jobs_df = _sort_digest_jobs_by_company(jobs_df.copy())
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

    removed = removed_jobs_df if removed_jobs_df is not None else pd.DataFrame()
    plain = _build_digest_plain(
        jobs_df, contacts_df, net, fstats, dsrc, cfg, table_action=table_action, removed_jobs_df=removed
    )
    if digest_note.strip():
        plain = f"{digest_note.strip()}\n\n{plain}"
    html_body = _build_digest_html(
        jobs_df,
        contacts_df,
        net,
        cfg,
        fstats,
        dsrc,
        digest_note=digest_note,
        table_action=table_action,
        removed_jobs_df=removed,
    )

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
