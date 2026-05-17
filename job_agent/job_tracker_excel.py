"""Job application tracker (.xlsx): apply links, status, digest email merge."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from job_agent.excel_email import (
    DIGEST_JOB_TABLE_COLUMNS,
    JOB_TRACKER_EXTRA_COLUMNS,
    TRACKER_COL_LAST_UPDATED,
    _rename_job_columns,
)
from job_agent.util import normalize_url

STATUS_NEW = "New"
STATUS_IN_PROGRESS = "In Progress"
STATUS_INTERVIEW = "Interview"
STATUS_REJECTED = "Rejected"

# Canonical list for digest email + tracker (edit in config.json → job_tracker.status_values).
DEFAULT_STATUS_VALUES: Sequence[str] = (
    STATUS_NEW,
    STATUS_IN_PROGRESS,
    STATUS_INTERVIEW,
    STATUS_REJECTED,
)

# Excel / CLI aliases → canonical label ("Rejected" = company said no; "Declined" accepted too).
STATUS_ALIASES: Dict[str, str] = {
    "in progress": STATUS_IN_PROGRESS,
    "inprogress": STATUS_IN_PROGRESS,
    "in_progress": STATUS_IN_PROGRESS,
    "declined": STATUS_REJECTED,
    "reject": STATUS_REJECTED,
    "new": STATUS_NEW,
    "interview": STATUS_INTERVIEW,
}


def job_tracker_columns() -> List[str]:
    return list(DIGEST_JOB_TABLE_COLUMNS) + list(JOB_TRACKER_EXTRA_COLUMNS)


def job_tracker_enabled(cfg: Dict[str, Any]) -> bool:
    block = cfg.get("job_tracker")
    if isinstance(block, dict) and "enabled" in block:
        return bool(block.get("enabled"))
    return True


def default_job_tracker_path(root: Path, cfg: Optional[Dict[str, Any]] = None) -> Path:
    cfg = cfg or {}
    block = cfg.get("job_tracker")
    if isinstance(block, dict):
        raw = str(block.get("path") or "").strip()
        if raw:
            p = Path(raw).expanduser()
            return p if p.is_absolute() else root / p
    return root / "job_tracker.xlsx"


def job_tracker_sheet_name(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or {}
    block = cfg.get("job_tracker")
    if isinstance(block, dict):
        name = str(block.get("sheet_name") or "").strip()
        if name:
            return name[:31]
    return "Jobs in this digest"


def tracker_timezone(cfg: Dict[str, Any]) -> ZoneInfo:
    sched = cfg.get("schedule")
    tz_name = ""
    if isinstance(sched, dict):
        tz_name = str(sched.get("timezone") or "").strip()
    if not tz_name:
        block = cfg.get("job_tracker")
        if isinstance(block, dict):
            tz_name = str(block.get("timezone") or "").strip()
    try:
        return ZoneInfo(tz_name or "Asia/Jerusalem")
    except Exception:
        return ZoneInfo("Asia/Jerusalem")


def format_apply_datetime(dt: datetime, cfg: Dict[str, Any]) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tracker_timezone(cfg))
    else:
        dt = dt.astimezone(tracker_timezone(cfg))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def status_default_new(cfg: Dict[str, Any]) -> str:
    block = cfg.get("job_tracker")
    if isinstance(block, dict):
        raw = str(block.get("status_default_new") or "").strip()
        if raw:
            return raw
    return STATUS_NEW


def status_on_apply(cfg: Dict[str, Any]) -> str:
    block = cfg.get("job_tracker")
    if isinstance(block, dict):
        raw = str(block.get("status_default_on_apply") or "").strip()
        if raw:
            return raw
    return STATUS_IN_PROGRESS


def allowed_status_values(cfg: Dict[str, Any]) -> List[str]:
    block = cfg.get("job_tracker")
    if isinstance(block, dict):
        raw = block.get("status_values")
        if isinstance(raw, list) and raw:
            return [str(s).strip() for s in raw if str(s).strip()]
    return list(DEFAULT_STATUS_VALUES)


def normalize_status_label(raw: str, cfg: Dict[str, Any], *, strict: bool = False) -> str:
    """Map free text / Excel values to a canonical status from config."""
    s = _cell_str(raw)
    if not s:
        return status_default_new(cfg)
    allowed = allowed_status_values(cfg)
    for label in allowed:
        if label.lower() == s.lower():
            return label
    alias = STATUS_ALIASES.get(s.lower())
    if alias and alias in allowed:
        return alias
    if strict:
        raise ValueError(f"Unknown status {s!r}. Allowed: {', '.join(allowed)}")
    return s


def status_links_enabled(cfg: Dict[str, Any]) -> bool:
    block = cfg.get("job_tracker")
    if isinstance(block, dict) and "status_links_enabled" in block:
        return bool(block.get("status_links_enabled"))
    return job_tracker_enabled(cfg)


def _label_to_canonical_map(cfg: Dict[str, Any]) -> Dict[str, str]:
    labels = cfg.get("excel_column_labels")
    inv: Dict[str, str] = {}
    if isinstance(labels, dict):
        for canon, label in labels.items():
            if str(label).strip():
                inv[str(label).strip()] = str(canon)
    for c in job_tracker_columns():
        inv[c] = c
    inv["Job link / URL"] = "Link"
    for legacy in ("Apply Date", "Did U Apply", "Applied?", "Last Update Date", "Last Modify"):
        inv[legacy] = TRACKER_COL_LAST_UPDATED
    return inv


def _canonical_to_label_map(cfg: Dict[str, Any]) -> Dict[str, str]:
    labels = cfg.get("excel_column_labels")
    out: Dict[str, str] = {}
    if isinstance(labels, dict):
        for k, v in labels.items():
            if str(v).strip():
                out[str(k)] = str(v).strip()
    for c in job_tracker_columns():
        out.setdefault(c, c)
    return out


def _last_updated_source_columns(columns: Sequence[Any]) -> List[str]:
    sources: List[str] = []
    for c in columns:
        name = str(c).strip()
        low = name.lower()
        if low == TRACKER_COL_LAST_UPDATED.lower():
            sources.append(name)
        elif name in ("Apply Date", "Did U Apply", "Applied?", "Last Update Date", "Last Modify"):
            sources.append(name)
        elif low.startswith("__legacy_") and "last updated" in low:
            sources.append(name)
    return sources


def _coalesce_last_updated_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Merge legacy Apply Date / Did U Apply / duplicate headers into one column."""
    out = df.copy()
    sources = _last_updated_source_columns(out.columns)
    if not sources:
        return out

    combined: List[str] = []
    for _, row in out.iterrows():
        picked = ""
        for col in sources:
            v = _cell_str(row.get(col, ""))
            if not v or v.lower() == "yes":
                continue
            picked = v
            break
        combined.append(picked)
    out[TRACKER_COL_LAST_UPDATED] = combined
    drop_cols = [c for c in sources if c != TRACKER_COL_LAST_UPDATED]
    if drop_cols:
        out = out.drop(columns=drop_cols, errors="ignore")
    return out


def _to_canonical_df(df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    inv = _label_to_canonical_map(cfg)
    rename: Dict[str, str] = {}
    used_targets: Dict[str, int] = {}
    for c in df.columns:
        key = str(c).strip()
        target = inv.get(key, key)
        if target == TRACKER_COL_LAST_UPDATED:
            n = used_targets.get(target, 0)
            used_targets[target] = n + 1
            if n:
                target = f"__legacy_{n}_{TRACKER_COL_LAST_UPDATED}"
        rename[key] = target
    out = df.rename(columns=rename)
    out = _coalesce_last_updated_columns(out)
    for col in job_tracker_columns():
        if col not in out.columns:
            out[col] = ""
    return out[job_tracker_columns()]


def _to_labeled_df(df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    return _rename_job_columns(_to_canonical_df(df, cfg).copy(), cfg)


def _cell_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and pd.isna(val):
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    return s


def _is_populated(val: Any) -> bool:
    return bool(_cell_str(val))


def _display_status(val: Any, *, applied: bool, cfg: Dict[str, Any]) -> str:
    s = _cell_str(val)
    if s:
        return normalize_status_label(s, cfg, strict=False)
    return status_on_apply(cfg) if applied else status_default_new(cfg)


def _normalize_tracker_frame(df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        out[col] = out[col].apply(_cell_str)
    if "Status" in out.columns:
        def _fix_status(row: pd.Series) -> str:
            applied = _is_populated(row.get(TRACKER_COL_LAST_UPDATED, ""))
            return _display_status(row.get("Status", ""), applied=applied, cfg=cfg)

        out["Status"] = out.apply(_fix_status, axis=1)
    return out


@contextmanager
def _tracker_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_tracker_df(cfg: Dict[str, Any], *, root: Path | None = None) -> pd.DataFrame:
    path = default_job_tracker_path(root or Path.cwd(), cfg)
    if not path.is_file():
        return pd.DataFrame(columns=job_tracker_columns())
    with _tracker_lock(path):
        raw = pd.read_excel(path, sheet_name=job_tracker_sheet_name(cfg), engine="openpyxl")
    return _normalize_tracker_frame(_to_canonical_df(raw, cfg), cfg)


def save_tracker_df(df: pd.DataFrame, cfg: Dict[str, Any], *, root: Path | None = None) -> Path:
    path = default_job_tracker_path(root or Path.cwd(), cfg)
    labeled = _to_labeled_df(df, cfg)
    with _tracker_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            labeled.to_excel(writer, sheet_name=job_tracker_sheet_name(cfg), index=False)
    return path


def tracker_index_by_link(df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if df.empty or "Link" not in df.columns:
        return out
    for _, row in df.iterrows():
        link = normalize_url(str(row.get("Link") or "").strip())
        if link:
            out[link] = {c: _cell_str(row.get(c, "")) for c in job_tracker_columns()}
    return out


def sync_digest_jobs_to_tracker(jobs_df: pd.DataFrame, cfg: Dict[str, Any], *, root: Path) -> None:
    """Append digest rows missing from the tracker (keeps existing apply/status)."""
    if jobs_df.empty or not job_tracker_enabled(cfg):
        return
    tracker = load_tracker_df(cfg, root=root)
    by_link = tracker_index_by_link(tracker)
    new_rows: List[Dict[str, Any]] = []
    for _, row in jobs_df.iterrows():
        link = normalize_url(str(row.get("Link") or "").strip())
        if not link or link in by_link:
            continue
        snap = {c: row.get(c, "") for c in DIGEST_JOB_TABLE_COLUMNS if c in row.index}
        snap["Link"] = link
        snap[TRACKER_COL_LAST_UPDATED] = ""
        snap["Status"] = STATUS_NEW
        new_rows.append(snap)
        by_link[link] = snap
    if not new_rows:
        return
    updated = pd.concat([tracker, pd.DataFrame(new_rows)], ignore_index=True)
    save_tracker_df(updated, cfg, root=root)


def touch_last_updated(cfg: Dict[str, Any]) -> str:
    return format_apply_datetime(datetime.now(tracker_timezone(cfg)), cfg)


def record_job_apply(link: str, job_snapshot: Dict[str, Any], cfg: Dict[str, Any], *, root: Path) -> str:
    """Mark job as applied in tracker; returns formatted last-updated timestamp."""
    link = normalize_url(link.strip())
    when = touch_last_updated(cfg)
    status = status_on_apply(cfg)
    tracker = load_tracker_df(cfg, root=root)
    by_link = tracker_index_by_link(tracker)

    if link in by_link:
        row = dict(by_link[link])
        for k, v in job_snapshot.items():
            if _is_populated(v) and k in DIGEST_JOB_TABLE_COLUMNS:
                row[k] = v
    else:
        row = {c: job_snapshot.get(c, "") for c in job_tracker_columns()}
        row["Link"] = link

    row[TRACKER_COL_LAST_UPDATED] = when
    row["Status"] = normalize_status_label(status, cfg, strict=False)
    by_link[link] = row
    rows = [by_link[k] for k in sorted(by_link.keys())]
    save_tracker_df(pd.DataFrame(rows), cfg, root=root)
    return when


def set_job_tracker_status(
    link: str,
    status: str,
    cfg: Dict[str, Any],
    *,
    root: Path,
    job_snapshot: Optional[Dict[str, Any]] = None,
) -> str:
    """Set Status in job_tracker.xlsx (creates row if missing). Returns canonical status."""
    link = normalize_url(link.strip())
    canonical = normalize_status_label(status, cfg, strict=True)
    tracker = load_tracker_df(cfg, root=root)
    by_link = tracker_index_by_link(tracker)

    if link in by_link:
        row = dict(by_link[link])
    else:
        snap = job_snapshot or {}
        row = {c: _cell_str(snap.get(c, "")) for c in job_tracker_columns()}
        row["Link"] = link
        row[TRACKER_COL_LAST_UPDATED] = ""
        row["Status"] = status_default_new(cfg)

    row["Status"] = canonical
    row[TRACKER_COL_LAST_UPDATED] = touch_last_updated(cfg)
    by_link[link] = row
    save_tracker_df(pd.DataFrame([by_link[k] for k in sorted(by_link.keys())]), cfg, root=root)
    return canonical


def update_job_status(link: str, status: str, cfg: Dict[str, Any], *, root: Path) -> bool:
    try:
        set_job_tracker_status(link, status, cfg, root=root)
        return True
    except ValueError:
        return False


def ensure_tracker_status_has_timestamp(cfg: Dict[str, Any], *, root: Path) -> int:
    """If Status was set without Last updated (manual edit or old code), backfill timestamp."""
    tracker = load_tracker_df(cfg, root=root)
    by_link = tracker_index_by_link(tracker)
    changed = 0
    for link, row in by_link.items():
        status = normalize_status_label(row.get("Status", ""), cfg, strict=False)
        if status == status_default_new(cfg):
            continue
        if _is_populated(row.get(TRACKER_COL_LAST_UPDATED, "")):
            continue
        row[TRACKER_COL_LAST_UPDATED] = touch_last_updated(cfg)
        row["Status"] = status
        by_link[link] = row
        changed += 1
    if changed:
        save_tracker_df(pd.DataFrame([by_link[k] for k in sorted(by_link.keys())]), cfg, root=root)
    return changed


def apply_tracker_to_digest_df(jobs_df: pd.DataFrame, cfg: Dict[str, Any], *, root: Path) -> pd.DataFrame:
    """Merge tracker last-updated / status into digest table for email rendering."""
    if jobs_df.empty or not job_tracker_enabled(cfg):
        return jobs_df
    ensure_tracker_status_has_timestamp(cfg, root=root)
    out = jobs_df.copy()
    by_link = tracker_index_by_link(load_tracker_df(cfg, root=root))
    last_updated_col: List[str] = []
    status_col: List[str] = []
    for _, row in out.iterrows():
        link = normalize_url(str(row.get("Link") or "").strip())
        rec = by_link.get(link) or {}
        updated = _cell_str(rec.get(TRACKER_COL_LAST_UPDATED, ""))
        status = normalize_status_label(rec.get("Status", ""), cfg, strict=False)
        if not status:
            status = status_on_apply(cfg) if _is_populated(updated) else status_default_new(cfg)
        last_updated_col.append(updated)
        status_col.append(status)
    out[TRACKER_COL_LAST_UPDATED] = last_updated_col
    out["Status"] = status_col
    return out


def job_row_snapshot_from_df(row: pd.Series) -> Dict[str, Any]:
    return {c: row.get(c, "") for c in DIGEST_JOB_TABLE_COLUMNS if c in row.index}


def create_empty_job_tracker(
    path: Path,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    overwrite: bool = False,
) -> Path:
    path = path.expanduser()
    if path.exists() and not overwrite:
        raise FileExistsError(f"Job tracker already exists: {path} (use overwrite=True to replace)")

    empty = pd.DataFrame(columns=job_tracker_columns())
    cfg = cfg or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _to_labeled_df(empty, cfg).to_excel(writer, sheet_name=job_tracker_sheet_name(cfg), index=False)
    return path
