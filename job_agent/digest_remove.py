"""Signed remove/restore links in digest email + local HTTP handler."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Literal, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from job_agent.ignore_store import (
    add_removed_record,
    ignore_store_path,
    job_to_removed_record,
    load_removed_records,
    restore_removed_link,
)
from job_agent.settings import get_setting
from job_agent.util import normalize_url

Action = Literal["remove", "restore", "apply", "set_status"]

_SERVER_LOCK = threading.Lock()
_SERVER: Optional[ThreadingHTTPServer] = None
_SERVER_THREAD: Optional[threading.Thread] = None


def _digest_remove_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    block = cfg.get("digest_remove")
    return block if isinstance(block, dict) else {}


def digest_remove_enabled(cfg: Dict[str, Any]) -> bool:
    block = _digest_remove_block(cfg)
    return bool(block.get("enabled", True))


def job_tracker_apply_enabled(cfg: Dict[str, Any]) -> bool:
    from job_agent.job_tracker_excel import job_tracker_enabled

    if not job_tracker_enabled(cfg):
        return False
    block = cfg.get("job_tracker")
    if isinstance(block, dict) and "apply_links_enabled" in block:
        return bool(block.get("apply_links_enabled"))
    return digest_remove_enabled(cfg)


def remove_secret(cfg: Dict[str, Any]) -> str:
    block = _digest_remove_block(cfg)
    for key in ("secret",):
        v = (block.get(key) or "").strip()
        if v:
            return v
    env = get_setting("JOB_AGENT_REMOVE_SECRET", "DIGEST_REMOVE_SECRET").strip()
    if env:
        return env
    path = ignore_store_path(cfg)
    seed = f"job-agent-remove:{path}"
    return hashlib.sha256(seed.encode()).hexdigest()


def remove_base_url(cfg: Dict[str, Any]) -> str:
    block = _digest_remove_block(cfg)
    raw = (block.get("base_url") or "").strip()
    if raw:
        return raw.rstrip("/")
    port = int(block.get("port") or 8791)
    host = (block.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    return f"http://{host}:{port}"


def remove_listen_host_port(cfg: Dict[str, Any]) -> Tuple[str, int]:
    block = _digest_remove_block(cfg)
    port = int(block.get("port") or 8791)
    host = (block.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    return host, port


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def sign_action_token(
    link: str,
    cfg: Dict[str, Any],
    *,
    action: Action = "remove",
    status: str = "",
    ttl_days: int = 90,
) -> str:
    payload: Dict[str, Any] = {
        "link": normalize_url(link.strip()),
        "action": action,
        "exp": int(time.time()) + max(1, ttl_days) * 86400,
    }
    if status.strip():
        payload["status"] = status.strip()
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(remove_secret(cfg).encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def _decode_action_token(token: str, cfg: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    token = (token or "").strip()
    if "." not in token:
        return None, "Invalid token"
    body, sig = token.rsplit(".", 1)
    expected_sig = hmac.new(remove_secret(cfg).encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected_sig, sig):
        return None, "Invalid signature"
    try:
        payload = json.loads(_b64url_decode(body))
    except (json.JSONDecodeError, ValueError):
        return None, "Invalid token payload"
    if not isinstance(payload, dict):
        return None, "Invalid token payload"
    exp = int(payload.get("exp") or 0)
    if exp and time.time() > exp:
        return None, "This link expired"
    return payload, None


def verify_action_token(token: str, cfg: Dict[str, Any], *, expected: Action) -> Tuple[Optional[str], Optional[str]]:
    payload, err = _decode_action_token(token, cfg)
    if err or not payload:
        return None, err
    link = normalize_url(str(payload.get("link") or "").strip())
    if not link:
        return None, "Missing job link"
    action = str(payload.get("action") or "remove").strip().lower()
    if action != expected:
        return None, f"Invalid action (expected {expected})"
    return link, None


def verify_set_status_token(token: str, cfg: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    payload, err = _decode_action_token(token, cfg)
    if err or not payload:
        return None, None, err
    link = normalize_url(str(payload.get("link") or "").strip())
    if not link:
        return None, None, "Missing job link"
    action = str(payload.get("action") or "").strip().lower()
    if action != "set_status":
        return None, None, "Invalid action (expected set_status)"
    status = str(payload.get("status") or "").strip()
    if not status:
        return None, None, "Missing status"
    return link, status, None


def build_remove_yes_url(link: str, cfg: Dict[str, Any]) -> str:
    token = sign_action_token(link, cfg, action="remove")
    return f"{remove_base_url(cfg)}/remove?t={token}"


def build_restore_url(link: str, cfg: Dict[str, Any]) -> str:
    token = sign_action_token(link, cfg, action="restore")
    return f"{remove_base_url(cfg)}/restore?t={token}"


def build_apply_yes_url(link: str, cfg: Dict[str, Any]) -> str:
    token = sign_action_token(link, cfg, action="apply")
    return f"{remove_base_url(cfg)}/apply?t={token}"


def build_set_status_url(link: str, status: str, cfg: Dict[str, Any]) -> str:
    from job_agent.job_tracker_excel import normalize_status_label

    canonical = normalize_status_label(status, cfg, strict=True)
    token = sign_action_token(link, cfg, action="set_status", status=canonical)
    return f"{remove_base_url(cfg)}/status?t={token}"


def _html_page(title: str, body: str, *, status: int = 200) -> bytes:
    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:sans-serif;max-width:42em;margin:2em auto;line-height:1.5;">
<h1>{title}</h1>
{body}
<p style="color:#666;font-size:13px;">Job Agent — this link only works on the Mac where the remove server runs.</p>
</body></html>"""
    return page.encode("utf-8")


def _apply_remove(link: str, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    from job_agent import db as job_db

    snapshot: Dict[str, Any] = {"link": link}
    conn = job_db.connect()
    try:
        job = job_db.load_job_by_link(conn, link)
        if job is not None:
            snapshot = job_to_removed_record(job)
        job_db.delete_jobs(conn, [link])
    finally:
        conn.close()
    added = add_removed_record(snapshot, cfg)
    if added:
        title = snapshot.get("title") or link
        return True, f"<p><strong>Removed.</strong> «{title}» will not appear in future digests.</p>"
    return False, "<p><strong>Already removed.</strong> This job was already on your hide list.</p>"


def _project_root(cfg: Dict[str, Any]) -> "Path":
    from pathlib import Path

    raw = str(cfg.get("_project_root") or "").strip()
    return Path(raw).resolve() if raw else Path.cwd().resolve()


def _apply_set_status(link: str, status: str, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    from job_agent.job_tracker_excel import set_job_tracker_status

    try:
        canonical = set_job_tracker_status(link, status, cfg, root=_project_root(cfg))
    except ValueError as exc:
        return False, f"<p>{html.escape(str(exc))}</p>"
    title = link
    path = _project_root(cfg) / "job_tracker.xlsx"
    block = cfg.get("job_tracker")
    if isinstance(block, dict) and block.get("path"):
        from pathlib import Path

        p = Path(str(block["path"]).expanduser())
        path = p if p.is_absolute() else _project_root(cfg) / p
    tracker_msg = (
        f"<p><strong>Status updated.</strong> Set to <strong>{html.escape(canonical)}</strong>. "
        f"<strong>Last updated</strong> refreshed to now.</p>"
        f"<p>Saved in <code>{html.escape(str(path))}</code> — the next digest reads this file.</p>"
    )
    return True, tracker_msg


def _apply_mark_applied(link: str, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    from job_agent.job_tracker_excel import record_job_apply

    snapshot: Dict[str, Any] = {"link": link, "Link": link}
    from job_agent import db as job_db

    conn = job_db.connect()
    try:
        job = job_db.load_job_by_link(conn, link)
        if job is not None:
            snapshot = {
                "Job Title": job.title,
                "Company": job.company,
                "Location": job.location,
                "Link": job.link,
                "Source": job.source,
                "Network": "",
            }
    finally:
        conn.close()
    when = record_job_apply(link, snapshot, cfg, root=_project_root(cfg))
    title = snapshot.get("Job Title") or link
    return True, (
        f"<p><strong>Marked applied.</strong> «{title}» — Last updated set to "
        f"<strong>{when}</strong>, Status <strong>In Progress</strong>.</p>"
        f"<p>Saved in <code>job_tracker.xlsx</code> (Last updated + Status). "
        "Next digest will show both.</p>"
    )


def _apply_restore(link: str, cfg: Dict[str, Any]) -> Tuple[bool, str]:
    from job_agent import db as job_db
    from job_agent.ignore_store import record_to_job

    snapshot = restore_removed_link(link, cfg)
    if snapshot is None:
        return False, "<p><strong>Not found.</strong> This job was not on your removed list.</p>"
    conn = job_db.connect()
    try:
        if snapshot.get("title") or snapshot.get("company"):
            job_db.upsert_jobs(conn, [record_to_job(snapshot)], mark_emailed=False)
    finally:
        conn.close()
    title = snapshot.get("title") or link
    return True, f"<p><strong>Restored.</strong> «{title}» can appear in digests again on the next fetch.</p>"


class _RemoveHandler(BaseHTTPRequestHandler):
    cfg: Dict[str, Any] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[digest-remove] {self.address_string()} {fmt % args}", file=sys.stderr)

    def _send_html(self, status: int, title: str, body: str) -> None:
        data = _html_page(title, body, status=status)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route_path(self, parsed) -> str:
        path = (parsed.path or "/").split("?")[0].rstrip("/").lower() or "/"
        return path

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = self._route_path(parsed)
        if route == "/health":
            self._send_html(
                200,
                "OK",
                "<p>Remove / restore / apply server is running.</p>"
                "<p>Routes: <code>/remove</code>, <code>/restore</code>, <code>/apply</code>, "
                "<code>/status</code></p>",
            )
            return
        qs = parse_qs(parsed.query)
        token = (qs.get("t") or [""])[0]
        if route == "/remove":
            link, err = verify_action_token(token, self.cfg, expected="remove")
            if err or not link:
                self._send_html(400, "Could not remove", f"<p>{err or 'Unknown error'}.</p>")
                return
            _, msg = _apply_remove(link, self.cfg)
            self._send_html(200, "Job hidden", f"{msg}<p style=\"word-break:break-all;font-size:13px;\">{link}</p>")
            return
        if route == "/restore":
            link, err = verify_action_token(token, self.cfg, expected="restore")
            if err or not link:
                self._send_html(400, "Could not restore", f"<p>{err or 'Unknown error'}.</p>")
                return
            _, msg = _apply_restore(link, self.cfg)
            self._send_html(200, "Job restored", f"{msg}<p style=\"word-break:break-all;font-size:13px;\">{link}</p>")
            return
        if route == "/apply":
            link, err = verify_action_token(token, self.cfg, expected="apply")
            if err or not link:
                self._send_html(400, "Could not mark applied", f"<p>{err or 'Unknown error'}.</p>")
                return
            _, msg = _apply_mark_applied(link, self.cfg)
            self._send_html(200, "Marked applied", f"{msg}<p style=\"word-break:break-all;font-size:13px;\">{link}</p>")
            return
        if route == "/status":
            link, status, err = verify_set_status_token(token, self.cfg)
            if err or not link or not status:
                self._send_html(400, "Could not update status", f"<p>{err or 'Unknown error'}.</p>")
                return
            _, msg = _apply_set_status(link, status, self.cfg)
            self._send_html(200, "Status updated", f"{msg}<p style=\"word-break:break-all;font-size:13px;\">{link}</p>")
            return
        self._send_html(404, "Not found", "<p>Unknown path.</p>")


def _health_check(cfg: Dict[str, Any], timeout: float = 0.6) -> bool:
    host, port = remove_listen_host_port(cfg)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _server_has_tracker_routes(cfg: Dict[str, Any], timeout: float = 0.8) -> bool:
    """True if HTTP handler exposes /apply and /status (not an old remove-only process)."""
    import urllib.error
    import urllib.request

    for path in ("/apply", "/status"):
        url = f"{remove_base_url(cfg)}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                if resp.status >= 500:
                    return False
            continue
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 405):
                return False
        except OSError:
            return False
    return True


def start_remove_server(cfg: Dict[str, Any], *, background: bool = True) -> ThreadingHTTPServer:
    global _SERVER, _SERVER_THREAD
    with _SERVER_LOCK:
        if _SERVER is not None:
            return _SERVER
        host, port = remove_listen_host_port(cfg)
        handler_cls = type("CfgRemoveHandler", (_RemoveHandler,), {"cfg": cfg})
        httpd = ThreadingHTTPServer((host, port), handler_cls)
        _SERVER = httpd
        if background:
            thread = threading.Thread(target=httpd.serve_forever, name="digest-remove", daemon=True)
            thread.start()
            _SERVER_THREAD = thread
        return httpd


def _stop_background_server() -> None:
    global _SERVER, _SERVER_THREAD
    with _SERVER_LOCK:
        if _SERVER is not None:
            try:
                _SERVER.shutdown()
            except Exception:
                pass
            _SERVER = None
            _SERVER_THREAD = None


def ensure_remove_server_running(cfg: Dict[str, Any]) -> bool:
    if not digest_remove_enabled(cfg):
        return False
    if _health_check(cfg) and _server_has_tracker_routes(cfg):
        return True
    if _health_check(cfg) and not _server_has_tracker_routes(cfg):
        print(
            "Digest server on port is outdated (no /apply). Restarting with current code…",
            file=sys.stderr,
        )
        _stop_background_server()
        host, port = remove_listen_host_port(cfg)
        try:
            import urllib.request

            urllib.request.urlopen(f"http://{host}:{port}/shutdown-not-supported", timeout=0.3)
        except Exception:
            pass
    try:
        start_remove_server(cfg, background=True)
        return _health_check(cfg, timeout=1.5) and _server_has_tracker_routes(cfg, timeout=1.5)
    except OSError as exc:
        print(f"digest-remove server failed to start: {exc}", file=sys.stderr)
        return False


def run_remove_server_forever(cfg: Dict[str, Any]) -> None:
    host, port = remove_listen_host_port(cfg)
    httpd = start_remove_server(cfg, background=False)
    print(f"Digest remove/restore/apply server on http://{host}:{port} (Ctrl+C to stop)", file=sys.stderr)
    print(f"Ignore store: {ignore_store_path(cfg)}", file=sys.stderr)
    print(f"Removed jobs: {len(load_removed_records(cfg))}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)


# Backward-compatible alias
verify_remove_token = lambda token, cfg: verify_action_token(token, cfg, expected="remove")
sign_remove_token = lambda link, cfg, **kw: sign_action_token(link, cfg, action="remove", **kw)
