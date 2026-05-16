"""LinkedIn Jobs via your logged-in browser session (Playwright persistent profile)."""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

from job_agent.browser.session import open_linkedin_login, playwright_available, with_linkedin_context
from job_agent.models import Job
from job_agent.scoring import score_title
from job_agent.util import normalize_url

_JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/]+-)?(\d+)")


def _linkedin_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    block = cfg.get("linkedin")
    return block if isinstance(block, dict) else {}


def _jobs_search_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    block = _linkedin_block(cfg)
    js = block.get("jobs_search")
    return js if isinstance(js, dict) else {}


def build_linkedin_jobs_search_url(cfg: Dict[str, Any]) -> str:
    js = _jobs_search_block(cfg)
    keywords = (js.get("keywords") or "devops manager").strip()
    location = (js.get("location") or "Israel").strip()
    params = f"keywords={quote_plus(keywords)}&location={quote_plus(location)}"
    geo_id = (js.get("geo_id") or "").strip()
    if geo_id:
        params += f"&geoId={quote_plus(geo_id)}"
    f_tpr = (js.get("f_TPR") or "").strip()
    if f_tpr:
        params += f"&f_TPR={quote_plus(f_tpr)}"
    return f"https://www.linkedin.com/jobs/search/?{params}"


def linkedin_login(cfg: Dict[str, Any], *, wait_minutes: int = 10) -> bool:
    return open_linkedin_login(cfg, wait_minutes=wait_minutes)


def _job_id_from_href(href: str) -> str:
    m = _JOB_ID_RE.search(href or "")
    return m.group(1) if m else ""


def _default_search_location(cfg: Dict[str, Any]) -> str:
    return (_jobs_search_block(cfg).get("location") or "Israel").strip() or "Israel"


def _extract_cards_from_page(page) -> List[Dict[str, str]]:
    """Best-effort parse of visible job cards (LinkedIn DOM changes often)."""
    script = """
    () => {
      const locRe = /Israel|ישראל|Remote|Hybrid|On-site|District|Tel Aviv|Herzliya|Haifa|Jerusalem|Ramat Gan|Rehovot|Ra'anana|Beer Sheva|באר שבע|תל אביב|הרצליה|ירושלים/i;
      const out = [];
      const seen = new Set();
      const anchors = document.querySelectorAll('a[href*="/jobs/view/"]');
      for (const a of anchors) {
        let href = a.href || a.getAttribute('href') || '';
        if (!href || seen.has(href)) continue;
        seen.add(href);
        const card = a.closest('li[data-occludable-job-id]')
          || a.closest('li.scaffold-layout__list-item')
          || a.closest('[data-job-id]')
          || a.closest('li')
          || a.closest('div[class*="job-card"]')
          || a.parentElement;
        let title = (a.innerText || a.textContent || '').trim();
        let company = '';
        let location = '';
        if (card) {
          const titleEl = card.querySelector(
            '.job-card-list__title, .base-search-card__title, [class*="job-card-list__title"], ' +
            'a[href*="/jobs/view/"] strong, a[href*="/jobs/view/"] span[aria-hidden="true"]'
          );
          const companyEl = card.querySelector(
            '.artdeco-entity-lockup__subtitle, .job-card-container__company-name, ' +
            '.base-search-card__subtitle, [class*="subtitle"]'
          );
          const locEl = card.querySelector(
            '.artdeco-entity-lockup__caption, .job-card-container__metadata-wrapper, ' +
            '.job-search-card__location, .job-card-container__metadata-item, [class*="location"]'
          );
          const ariaTitle = a.querySelector('span[aria-hidden="true"]');
          if (ariaTitle) title = (ariaTitle.innerText || '').trim() || title;
          else if (titleEl) title = (titleEl.innerText || '').trim() || title;
          if (title.includes('\\n')) title = title.split('\\n')[0].trim();
          if (companyEl) company = (companyEl.innerText || '').trim();
          if (locEl) location = (locEl.innerText || '').trim();
          if (!location) {
            const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
            for (const line of lines) {
              if (line.length > 120) continue;
              if (locRe.test(line) && !/alumni|Promoted|Easy Apply|Viewed|verification/i.test(line)) {
                location = line;
                break;
              }
            }
          }
          if (!company && title) {
            const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
            const ti = lines.findIndex(l => l === title || l.startsWith(title));
            if (ti >= 0 && ti + 1 < lines.length) {
              const cand = lines[ti + 1];
              if (!locRe.test(cand) && !/alumni|Promoted|Easy Apply|Viewed|verification/i.test(cand)) {
                company = cand;
              }
            }
          }
        }
        if (!title) continue;
        out.push({ href, title, company, location });
      }
      return out;
    }
    """
    try:
        raw = page.evaluate(script)
    except Exception:
        raw = []
    if not isinstance(raw, list):
        return []
    rows: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or "").strip()
        title = str(item.get("title") or "").strip().split("\n")[0].strip()
        if href and title:
            rows.append(
                {
                    "href": href,
                    "title": title[:300],
                    "company": str(item.get("company") or "").strip()[:120],
                    "location": str(item.get("location") or "").strip()[:120],
                }
            )
    return rows


def _search_url_with_job_id(search_url: str, job_id: str) -> str:
    parsed = urlparse(search_url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["currentJobId"] = job_id
    return urlunparse(parsed._replace(query=urlencode(q)))


_REACH_OUT_EXTRACT_JS = """
() => {
  const people = [];
  const seen = new Set();
  const findReachOutRoot = () => {
    const modal = document.querySelector('.artdeco-modal, [role="dialog"]');
    if (modal) return modal;
    const h = [...document.querySelectorAll('h2, h3')].find(el =>
      /people you can reach out to|in your network/i.test((el.innerText || '').trim())
    );
    if (h) {
      return (
        h.closest('section') ||
        h.closest('[class*="people-who-can-help"]') ||
        h.closest('[class*="job-details-people"]') ||
        h.parentElement?.parentElement ||
        null
      );
    }
    const card = document.querySelector(
      '[class*="job-details-people-who-can-help"], [class*="people-who-can-help"]'
    );
    return card;
  };
  const root = findReachOutRoot() || document;
  const scoped = root !== document;
  const parseAnchor = (a) => {
    const href = (a.href || a.getAttribute('href') || '').split('?')[0];
    if (!href || seen.has(href) || !/linkedin\\.com\\/in\\//i.test(href)) return null;
    const blob = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!blob || /^profile photo$/i.test(blob)) return null;
    // Outside the reach-out block, keep only obvious 1st-degree rows.
    if (!scoped && !/·\\s*1st\\b/i.test(blob) && !/\\b1st\\b/i.test(blob)) return null;
    seen.add(href);
    const img = a.querySelector('img[alt]');
    let name = (img?.getAttribute('alt') || '').replace(/\\s*profile photo\\s*$/i, '').trim();
    const nameSpan = a.querySelector('[class*="actor-name"], [class*="lockup__title"], strong, span[dir="ltr"]');
    if (nameSpan) {
      const t = (nameSpan.innerText || '').trim().split('\\n')[0].trim();
      if (t && t.length > name.length) name = t;
    }
    if (!name) {
      const head = blob.split(' is verified')[0].split('·')[0].trim();
      name = head.slice(0, 80);
    }
    const aria = (a.getAttribute('aria-label') || '').trim();
    if (aria && aria.length > name.length && aria.length < 80) name = aria;
    if (!name || name.length > 80) return null;
    let role = '';
    const m = blob.match(/·\\s*\\d+(?:st|nd|rd|th)\\s*(.+)$/i);
    if (m) role = m[1].trim().slice(0, 120);
    return { name, role, profile_url: href };
  };
  root.querySelectorAll('a[href*="/in/"]').forEach(a => {
    const row = parseAnchor(a);
    if (row) people.push(row);
  });
  return people;
}
"""


def _wait_for_reach_out_section(page, timeout_ms: int = 12_000) -> bool:
    try:
        page.wait_for_selector(
            'h2:has-text("People you can reach out to"), '
            'h3:has-text("People you can reach out to"), '
            '[class*="people-who-can-help"]',
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _click_reach_out_show_all(page) -> None:
    try:
        for sel in (
            "button.job-details-people-who-can-help__connections-card-summary-card-action",
            'button:has-text("Show all")',
            'a:has-text("Show all")',
        ):
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=5000)
                time.sleep(2.0)
                return
    except Exception:
        pass


def _dismiss_modal(page) -> None:
    try:
        page.keyboard.press("Escape")
        time.sleep(0.4)
    except Exception:
        pass


def _scroll_job_details_pane(page) -> None:
    try:
        page.evaluate(
            """
            () => {
              const pane =
                document.querySelector('.jobs-search__job-details') ||
                document.querySelector('.jobs-details') ||
                document.querySelector('[class*="job-details"]');
              if (pane) pane.scrollTop = Math.min(900, pane.scrollHeight);
              window.scrollBy(0, 400);
            }
            """
        )
    except Exception:
        pass


def _extract_reach_out_people(page) -> List[Dict[str, str]]:
    try:
        _wait_for_reach_out_section(page, timeout_ms=10_000)
        _scroll_job_details_pane(page)
        time.sleep(0.8)
        raw = page.evaluate(_REACH_OUT_EXTRACT_JS)
        # LinkedIn usually shows 1–3 faces inline; full list is behind «Show all».
        if page.locator('h2:has-text("People you can reach out to"), h3:has-text("People you can reach out to")').count():
            _click_reach_out_show_all(page)
            time.sleep(1.5)
            expanded = page.evaluate(_REACH_OUT_EXTRACT_JS)
            if isinstance(expanded, list) and len(expanded) > len(raw or []):
                raw = expanded
    except Exception:
        raw = []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name[:80],
                "role": str(item.get("role") or "").strip()[:120],
                "profile_url": str(item.get("profile_url") or "").strip(),
            }
        )
    return out


def enrich_reach_out_for_jobs(
    jobs: List[Job],
    cfg: Dict[str, Any],
    *,
    for_email: bool = False,
) -> None:
    """Open LinkedIn and scrape «People you can reach out to» for jobs missing that data."""
    if not jobs or not _linkedin_block(cfg).get("enabled", True):
        return
    if not playwright_available():
        return
    js = _jobs_search_block(cfg)
    if not js.get("scrape_reach_out_people", True):
        return
    need: List[Job] = []
    for job in jobs:
        if job.source != "linkedin_browser":
            continue
        raw = job.raw if isinstance(job.raw, dict) else {}
        scraped = raw.get("reach_out_people")
        if isinstance(scraped, list) and scraped:
            continue
        need.append(job)
    if not need:
        return
    search_url = build_linkedin_jobs_search_url(cfg)
    pw, context = with_linkedin_context(cfg)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        _enrich_jobs_reach_out_people(page, need, search_url, cfg, for_email=for_email)
    finally:
        context.close()
        pw.stop()


def _job_view_url(job: Job) -> str:
    link = (job.link or "").strip()
    if link:
        return link.split("?")[0]
    jid = ""
    if isinstance(job.raw, dict):
        jid = str(job.raw.get("linkedin_job_id") or "")
    if not jid:
        jid = _job_id_from_href(job.link)
    if jid:
        return f"https://www.linkedin.com/jobs/view/{jid}/"
    return ""


def _enrich_jobs_reach_out_people(
    page,
    jobs: List[Job],
    search_url: str,
    cfg: Dict[str, Any],
    *,
    for_email: bool = False,
) -> None:
    js = _jobs_search_block(cfg)
    if not js.get("scrape_reach_out_people", True):
        return
    cap_cfg = int(js.get("max_jobs_reach_out_scrape") or 20)
    if for_email:
        # Digest email: scrape every LinkedIn row we are about to send (no 30-job cap).
        cap = len(jobs) if cap_cfg <= 0 else max(cap_cfg, len(jobs))
    else:
        cap = max(0, cap_cfg)
    if cap == 0:
        return
    pause = float(js.get("reach_out_pause_seconds") or 1.8)
    with_people = 0
    targets = jobs[:cap]
    for job in targets:
        jid = ""
        if isinstance(job.raw, dict):
            jid = str(job.raw.get("linkedin_job_id") or "")
        if not jid:
            jid = _job_id_from_href(job.link)
        if not jid:
            continue
        view_url = _search_url_with_job_id(search_url, jid)
        try:
            _dismiss_modal(page)
            page.goto(view_url, wait_until="domcontentloaded", timeout=90_000)
            time.sleep(pause)
            if "authwall" in (page.url or "").lower():
                print(
                    f"LinkedIn reach-out: auth wall on job {jid} — run python3 run.py --linkedin-login",
                    file=sys.stderr,
                )
                continue
            people = _extract_reach_out_people(page)
            if not isinstance(job.raw, dict):
                job.raw = {}
            job.raw["reach_out_people"] = people
            if people:
                with_people += 1
                print(
                    f"LinkedIn reach-out: {len(people)} at {job.company or '?'} — {job.title[:50]}",
                    file=sys.stderr,
                )
            _dismiss_modal(page)
        except Exception as exc:
            print(f"LinkedIn reach-out: skip job {jid} ({exc})", file=sys.stderr)
            _dismiss_modal(page)
            continue
    print(
        f"LinkedIn browser: «People you can reach out to» scraped for {with_people}/{len(targets)} jobs",
        file=sys.stderr,
    )


def fetch_linkedin_jobs(cfg: Dict[str, Any]) -> List[Job]:
    """Search LinkedIn Jobs while logged in; requires prior ``--linkedin-login``."""
    if not _linkedin_block(cfg).get("enabled", True):
        return []
    if not playwright_available():
        print(
            "LinkedIn browser: skipped (install Playwright: pip install playwright && playwright install chromium)",
            file=sys.stderr,
        )
        return []

    js = _jobs_search_block(cfg)
    max_pages = max(1, int(js.get("max_pages") or 3))
    scroll_pause = float(js.get("scroll_pause_seconds") or 1.5)
    search_url = build_linkedin_jobs_search_url(cfg)
    default_location = _default_search_location(cfg)

    print(f"LinkedIn browser: opening {search_url}", file=sys.stderr)

    out: List[Job] = []
    seen_ids: set[str] = set()

    pw, context = with_linkedin_context(cfg)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(search_url, wait_until="domcontentloaded", timeout=90_000)
        try:
            page.wait_for_selector('a[href*="/jobs/view/"]', timeout=25_000)
        except Exception:
            pass
        time.sleep(3.0)

        url_low = (page.url or "").lower()
        title_low = (page.title() or "").lower()
        if (
            ("login" in url_low or "signup" in title_low or "sign up" in title_low)
            and "session_redirect" not in url_low
            and page.locator('a[href*="/jobs/view/"]').count() == 0
        ):
            print(
                "LinkedIn browser: not logged in (auth wall). Run: python3 run.py --linkedin-login",
                file=sys.stderr,
            )
            return []

        for page_idx in range(max_pages):
            rows = _extract_cards_from_page(page)
            if not rows and page_idx == 0:
                time.sleep(4.0)
                rows = _extract_cards_from_page(page)
            for row in rows:
                href = row["href"]
                if href.startswith("/"):
                    href = urljoin("https://www.linkedin.com", href)
                jid = _job_id_from_href(href)
                if jid and jid in seen_ids:
                    continue
                if jid:
                    seen_ids.add(jid)
                link_n = normalize_url(href.split("?")[0])
                title = row["title"]
                company = row["company"] or "Unknown"
                location = row["location"] or default_location
                out.append(
                    Job(
                        source="linkedin_browser",
                        company=company,
                        title=title,
                        location=location,
                        link=link_n,
                        posted="recent",
                        score=score_title(title, cfg),
                        raw={"search_url": search_url, "linkedin_job_id": jid},
                    )
                )

            if page_idx + 1 >= max_pages:
                break
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            time.sleep(scroll_pause)
            try:
                btn = page.query_selector('button[aria-label="View next page"], button.jobs-search-pagination__button--next')
                if btn and btn.is_enabled():
                    btn.click()
                    time.sleep(scroll_pause)
                else:
                    break
            except Exception:
                break

        print(f"LinkedIn browser: collected {len(out)} jobs ({len(seen_ids)} unique ids)", file=sys.stderr)
        if out:
            _enrich_jobs_reach_out_people(page, out, search_url, cfg)
    finally:
        context.close()
        pw.stop()

    return out
