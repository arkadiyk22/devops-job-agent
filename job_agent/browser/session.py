"""Playwright persistent browser profile (WhatsApp-Web style: login once, reuse session)."""

from __future__ import annotations

import sys
import time
from typing import Any, Dict

from job_agent.browser.paths import resolve_browser_user_data_dir


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def _launch_persistent(cfg: Dict[str, Any], *, headless: bool, service: str = "linkedin"):
    from playwright.sync_api import sync_playwright

    block = cfg.get("browser") if isinstance(cfg.get("browser"), dict) else {}
    user_data = resolve_browser_user_data_dir(cfg, service=service)
    user_data.mkdir(parents=True, exist_ok=True)
    slow_mo = int(block.get("slow_mo_ms") or 0)
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        str(user_data),
        headless=headless,
        slow_mo=slow_mo,
        viewport={"width": 1280, "height": 900},
        locale=(block.get("locale") or "he-IL"),
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
    )
    return pw, context


def _page_looks_logged_in(page) -> bool:
    url = (page.url or "").lower()
    title = (page.title() or "").lower()
    if "sign up" in title or "sign in" in title and "jobs/search" not in url:
        if "login" in url or "signup" in url or "uas/login" in url:
            return False
    if "authwall" in url:
        return False
    if "linkedin.com/feed" in url:
        return True
    if "linkedin.com/jobs" in url and "login" not in url:
        return True
    if page.locator('a[href*="/jobs/view/"]').count() > 0:
        return True
    if page.locator('img.global-nav__me-photo, button.global-nav__primary-link-me-menu-trigger').count() > 0:
        return True
    return False


def linkedin_session_ready(cfg: Dict[str, Any], *, headless: bool = True) -> bool:
    """Quick check whether the saved browser profile can access LinkedIn (not auth wall)."""
    if not playwright_available():
        return False
    pw, context = _launch_persistent(cfg, headless=headless, service="linkedin")
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            "https://www.linkedin.com/jobs/search/?keywords=devops&location=Israel",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        time.sleep(2.5)
        return _page_looks_logged_in(page)
    except Exception:
        return False
    finally:
        context.close()
        pw.stop()


def open_linkedin_login(cfg: Dict[str, Any], *, wait_minutes: int = 10) -> bool:
    """Open a visible browser; wait until login is detected or timeout. Returns True if logged in."""
    if not playwright_available():
        print(
            "Playwright is not installed.\n"
            "  pip install playwright\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        raise SystemExit(1)

    user_data = resolve_browser_user_data_dir(cfg, service="linkedin")
    print(f"LinkedIn browser profile: {user_data}")
    print("A Chromium window will open — log in to LinkedIn in that window.")
    print(f"Waiting up to {wait_minutes} min for login (or press Enter when done)…\n")

    pw, context = _launch_persistent(cfg, headless=False, service="linkedin")
    logged_in = False
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60_000)
        deadline = time.time() + max(1, wait_minutes) * 60
        while time.time() < deadline:
            if _page_looks_logged_in(page):
                logged_in = True
                print("Login detected — saving session…")
                break
            time.sleep(2)
        if not logged_in:
            print("Still waiting — press Enter after you finish logging in…")
            try:
                import select

                if select.select([sys.stdin], [], [], 0)[0]:
                    sys.stdin.readline()
                else:
                    input()
            except EOFError:
                pass
            page.goto(
                "https://www.linkedin.com/jobs/search/?location=Israel",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            time.sleep(2)
            logged_in = _page_looks_logged_in(page)
        if logged_in:
            print("Session saved. You can run: python3 run.py")
        else:
            print(
                "Could not confirm login. Try again: python3 run.py --linkedin-login",
                file=sys.stderr,
            )
    finally:
        context.close()
        pw.stop()
    return logged_in


def with_linkedin_context(cfg: Dict[str, Any], *, headless: bool | None = None):
    """Context manager: (playwright, browser_context). Caller must close both."""
    block = cfg.get("browser") if isinstance(cfg.get("browser"), dict) else {}
    if headless is None:
        headless = bool(block.get("headless", True))
    pw, context = _launch_persistent(cfg, headless=headless, service="linkedin")
    return pw, context


def _page_looks_google_ok(page) -> bool:
    url = (page.url or "").lower()
    if "sorry" in url and "google" in url:
        return False
    if _page_has_google_captcha_text(page):
        return False
    return "google." in url


def _page_has_google_captcha_text(page) -> bool:
    try:
        body = (page.locator("body").inner_text(timeout=4000) or "").lower()
    except Exception:
        return False
    return "unusual traffic" in body or "not a robot" in body


def open_google_login(cfg: Dict[str, Any], *, wait_minutes: int = 10) -> bool:
    """Open visible Google in the google browser profile; confirm search works."""
    if not playwright_available():
        print(
            "Playwright is not installed.\n"
            "  pip install playwright\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        raise SystemExit(1)

    user_data = resolve_browser_user_data_dir(cfg, service="google")
    print(f"Google browser profile: {user_data}")
    print("Log in to Google if prompted (account helps avoid CAPTCHAs).")
    print(f"Waiting up to {wait_minutes} min…\n")

    pw, context = _launch_persistent(cfg, headless=False, service="google")
    ok = False
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.google.co.il", wait_until="domcontentloaded", timeout=60_000)
        deadline = time.time() + max(1, wait_minutes) * 60
        while time.time() < deadline:
            if _page_looks_google_ok(page) and not _page_has_google_captcha_text(page):
                ok = True
                break
            time.sleep(2)
        if not ok:
            print("Press Enter after you can use Google search normally…")
            try:
                input()
            except EOFError:
                pass
            page.goto(
                "https://www.google.co.il/search?q=devops+manager+israel",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            time.sleep(2)
            ok = _page_looks_google_ok(page) and not _page_has_google_captcha_text(page)
        if ok:
            print("Google session saved. Run: python3 run.py")
        else:
            print("Could not confirm Google — try: python3 run.py --google-login", file=sys.stderr)
    finally:
        context.close()
        pw.stop()
    return ok


def with_google_context(cfg: Dict[str, Any], *, headless: bool | None = None):
    """Persistent Chromium profile for Google Web (separate from LinkedIn)."""
    block = cfg.get("browser") if isinstance(cfg.get("browser"), dict) else {}
    gblock = cfg.get("google_web_browser") if isinstance(cfg.get("google_web_browser"), dict) else {}
    if headless is None:
        headless = bool(gblock.get("headless", block.get("headless", True)))
    pw, context = _launch_persistent(cfg, headless=headless, service="google")
    return pw, context
