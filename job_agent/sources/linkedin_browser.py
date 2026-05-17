"""LinkedIn Jobs via your logged-in browser session (Playwright persistent profile)."""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from job_agent.browser.session import open_linkedin_login, playwright_available, with_linkedin_context
from job_agent.models import Job
from job_agent.network import (
    REACH_OUT_LINKEDIN_SOURCE,
    is_usable_reach_out_person,
    linkedin_reach_out_snapshot_ok,
    person_display_name,
    reach_out_people_have_full_names,
)
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


_REACH_OUT_PRESENT_JS = """
() => {
  const isHiringBlock = (el) => {
    const cls = String(el.className || '');
    if (/hiring[-_]?team|hiringteam/i.test(cls)) return true;
    const t = (el.innerText || '');
    return /meet the hiring team|hiring team|צוות הגיוס|צוות גיוס/i.test(t) &&
      !/people[-_]?who[-_]?can[-_]?help|people-who-can-help/i.test(cls);
  };
  const isReachOutBlock = (el) => {
    if (isHiringBlock(el)) return false;
    const cls = String(el.className || '');
    if (/people[-_]?who[-_]?can[-_]?help|people-who-can-help/i.test(cls)) return true;
    const t = (el.innerText || '');
    return /people you can reach out to|in your network|אנשים ש|לפנות|ברשת שלך/i.test(t);
  };
  for (const el of document.querySelectorAll(
    '[class*="job-details-people-who-can-help"], [class*="people-who-can-help"]'
  )) {
    if (isReachOutBlock(el)) return true;
  }
  return [...document.querySelectorAll('h2, h3, [role="heading"]')].some((el) => {
    const t = (el.innerText || '').trim();
    return /people you can reach out to|אנשים ש/i.test(t) && !/hiring team|צוות הגיוס/i.test(t);
  });
}
"""

_REACH_OUT_EXTRACT_JS = """
() => {
  const people = [];
  const seen = new Set();
  const isHiringBlock = (el) => {
    const cls = String(el.className || '');
    if (/hiring[-_]?team|hiringteam/i.test(cls)) return true;
    const t = (el.innerText || '');
    return /meet the hiring team|hiring team|צוות הגיוס|צוות גיוס/i.test(t) &&
      !/people[-_]?who[-_]?can[-_]?help|people-who-can-help/i.test(cls);
  };
  const isReachOutBlock = (el) => {
    if (isHiringBlock(el)) return false;
    const cls = String(el.className || '');
    if (/people[-_]?who[-_]?can[-_]?help|people-who-can-help/i.test(cls)) return true;
    const t = (el.innerText || '');
    return /people you can reach out to|in your network|אנשים ש|לפנות|ברשת שלך/i.test(t);
  };
  const findReachOutRoot = () => {
    for (const modal of document.querySelectorAll('.artdeco-modal, [role="dialog"]')) {
      const t = modal.innerText || '';
      if (/meet the hiring team|hiring team|צוות הגיוס|job poster/i.test(t)) continue;
      if (!/in your network|people you can reach out|ברשת|אנשים ש/i.test(t)) continue;
      if (modal.querySelector('a[href*="/in/"]')) return modal;
    }
    for (const card of document.querySelectorAll(
      '[class*="job-details-connections-card"], [class*="people-who-can-help__connections-card"]'
    )) {
      if (isReachOutBlock(card)) return card;
    }
    for (const card of document.querySelectorAll(
      '[class*="job-details-people-who-can-help"], [class*="people-who-can-help"]'
    )) {
      if (isReachOutBlock(card)) return card;
    }
    const reachH = [...document.querySelectorAll('h2, h3, [role="heading"]')].find((el) => {
      const t = (el.innerText || '').trim();
      return /people you can reach out to|אנשים ש/i.test(t) && !/hiring team|צוות הגיוס/i.test(t);
    });
    if (reachH) {
      return (
        reachH.closest('[class*="people-who-can-help"]') ||
        reachH.closest('section') ||
        reachH.parentElement?.parentElement ||
        null
      );
    }
    return null;
  };
  const anchorReachOutScope = (a) => {
    const hiring = a.closest('[class*="hiring-team"], [class*="hiring_team"]');
    if (hiring) return null;
    const card = a.closest(
      '[class*="people-who-can-help"], [class*="job-details-people-who-can-help"]'
    );
    if (card && isReachOutBlock(card)) return card;
    const root = findReachOutRoot();
    if (root && root.contains(a)) return root;
    return null;
  };
  const root = findReachOutRoot();
  if (!root) return [];
  const pickFullName = (candidates) => {
    for (const raw of candidates) {
      const s = (raw || '').replace(/\\s+/g, ' ').trim();
      const degAt = s.search(/\\s·\\s*\\d(?:st|nd|rd|th)\\b/i);
      if (degAt > 0) {
        let before = s.slice(0, degAt).replace(/\\s*is verified\\s*/gi, ' ').replace(/\\s*profile\\s*photo\\s*/gi, ' ');
        before = before.replace(/\\s+/g, ' ').trim();
        const words = before.split(' ').filter(Boolean);
        if (words.length >= 4 && words.length % 2 === 0) {
          const half = words.length / 2;
          if (words.slice(0, half).join(' ').toLowerCase() === words.slice(half).join(' ').toLowerCase()) {
            before = words.slice(0, half).join(' ');
          }
        }
        const w2 = before.split(/\\s+/).filter(Boolean);
        if (w2.length >= 3 && w2[0].toLowerCase() === w2[1].toLowerCase()) {
          before = w2.slice(1).join(' ');
        }
        if (before.split(/\\s+/).length >= 2) return before;
      }
    }
    let best = '';
    let bestScore = -1;
    for (const raw of candidates) {
      let s = (raw || '').replace(/\\s+/g, ' ').trim();
      if (!s) continue;
      s = s.replace(/\\s*profile\\s*photo\\s*/gi, ' ').replace(/\\s+/g, ' ').trim();
      s = s.replace(/^view\\s+/i, '').replace(/(?:'s|'s|’s)\\s+profile$/i, '').trim();
      if (!s) continue;
      s = s.split(' is verified')[0].trim();
      const head = s.includes('·') ? s.split('·')[0].trim() : s;
      if (!head || head.length > 80) continue;
      if (/^\\d|linkedin/i.test(head)) continue;
      const words = head.split(/\\s+/).filter(Boolean);
      if (!words.length) continue;
      const score = words.length * 100 + head.length;
      if (score > bestScore) {
        bestScore = score;
        best = head;
      }
    }
    return best;
  };
  const parseAnchor = (a) => {
    const scope = anchorReachOutScope(a);
    if (!scope) return null;
    const href = (a.href || a.getAttribute('href') || '').split('?')[0];
    if (!href || seen.has(href) || !/linkedin\\.com\\/in\\//i.test(href)) return null;
    const blob = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    const row =
      a.closest('li.artdeco-list__item, li, div[class*="entity-result"], div[class*="lockup"]') || a.parentElement;
    let bundleText = row ? (row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim() : '';
    let ctx = row ? row.parentElement : null;
    for (let i = 0; i < 8 && ctx; i++) {
      const t = (ctx.innerText || '').replace(/\\s+/g, ' ').trim();
      if (/·\\s*\\d(?:st|nd|rd|th)\\b/i.test(t)) {
        bundleText = t;
        break;
      }
      ctx = ctx.parentElement;
    }
    if (/school alumni/i.test(bundleText)) return null;
    if (!/·\\s*1st\\b/i.test(bundleText)) return null;
    const rowText = bundleText;
    const aria = (a.getAttribute('aria-label') || '').trim();
    const imgAlt = (a.querySelector('img[alt]')?.getAttribute('alt') || '').trim();
    const candidates = [];
    if (aria) candidates.push(aria);
    if (row) {
      if (rowText) candidates.push(rowText);
      row.querySelectorAll(
        '[class*="lockup__title"], [class*="actor-name"], [class*="name"], strong, span[dir="ltr"]'
      ).forEach((el) => {
        const t = (el.innerText || '').trim().split('\\n')[0].trim();
        if (t) candidates.push(t);
      });
    }
    candidates.push(blob, imgAlt);
    if (!candidates.some((c) => (c || '').trim())) return null;
    seen.add(href);
    const name = pickFullName(candidates);
    if (!name) return null;
    const parts = name.split(/\\s+/).filter(Boolean);
    const first_name = parts[0] || '';
    const last_name = parts.length > 1 ? parts.slice(1).join(' ') : '';
    let role = '';
    const rm = (rowText || blob).match(/·\\s*1st\\s*(.+)$/i);
    if (rm) role = rm[1].trim().replace(/\\s+Message\\s*$/i, '').slice(0, 120);
    return { name, first_name, last_name, role, profile_url: href };
  };
  root.querySelectorAll('a[href*="/in/"]').forEach(a => {
    const row = parseAnchor(a);
    if (row) people.push(row);
  });
  if (people.length === 0 && root) {
    const text = (root.innerText || '').replace(/\\s+/g, ' ');
    const re = /([\\p{L}][\\p{L}'\\-]*(?:\\s+[\\p{L}][\\p{L}'\\-]*)+)\\s*[·•]\\s*1st\\b/gu;
    let m;
    const seenNames = new Set();
    while ((m = re.exec(text)) !== null) {
      let name = m[1].replace(/\\s*profile\\s*photo\\s*/gi, ' ').replace(/\\s+/g, ' ').trim();
      const w = name.split(/\\s+/).filter(Boolean);
      if (w.length >= 3 && w[0].toLowerCase() === w[1].toLowerCase()) name = w.slice(1).join(' ');
      const key = name.toLowerCase();
      if (!name || seenNames.has(key)) continue;
      seenNames.add(key);
      const parts = name.split(/\\s+/).filter(Boolean);
      people.push({
        name,
        first_name: parts[0] || '',
        last_name: parts.length > 1 ? parts.slice(1).join(' ') : '',
        role: '',
        profile_url: '',
      });
    }
  }
  return people;
}
"""


def _wait_for_reach_out_section(page, timeout_ms: int = 18_000) -> bool:
    try:
        page.wait_for_function(_REACH_OUT_PRESENT_JS, timeout=timeout_ms)
        return True
    except Exception:
        return False


def _scroll_reach_out_into_view(page) -> None:
    try:
        page.evaluate(
            """
            () => {
              const pick = () => {
                for (const el of document.querySelectorAll(
                  '[class*="people-who-can-help"], [class*="job-details-people-who-can-help"]'
                )) {
                  const cls = String(el.className || '');
                  if (/hiring[-_]?team/i.test(cls)) continue;
                  if (/people[-_]?who[-_]?can[-_]?help|people-who-can-help/i.test(cls)) return el;
                }
                return [...document.querySelectorAll('h2, h3')].find((el) =>
                  /people you can reach out to|אנשים ש/i.test(el.innerText || '')
                );
              };
              const el = pick();
              if (el) el.scrollIntoView({ block: 'center', behavior: 'instant' });
            }
            """
        )
    except Exception:
        pass


def _click_reach_out_show_all(page) -> None:
    try:
        block = page.locator(
            '[class*="job-details-connections-card"], [class*="people-who-can-help__connections-card"]'
        ).first
        for sel in (
            "button.job-details-people-who-can-help__connections-card-summary-card-action",
            'button:has-text("Show all")',
            'button:has-text("הצג הכל")',
            'a:has-text("Show all")',
            'a:has-text("הצג הכל")',
        ):
            btn = block.locator(sel).first if block.count() > 0 else page.locator(sel).first
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


_REACH_OUT_SUMMARY_JS = """
() => {
  const card =
    document.querySelector('[class*="connections-card-summary"]') ||
    document.querySelector('[class*="people-who-can-help__connections-card"]');
  if (!card) return '';
  let t = (card.innerText || '').replace(/\\s+/g, ' ').trim();
  t = t.replace(/\\s*Show all\\s*$/i, '').replace(/\\s*הצג הכל\\s*$/i, '').trim();
  t = t.replace(/^[\\w\\s]+ logo\\s+/i, '').trim();
  if (/meet the hiring team|hiring team|צוות הגיוס/i.test(t)) return '';
  if (/in your network|ברשת|אנשים ש|others in your network/i.test(t)) return t;
  return '';
}
"""


def _extract_reach_out_collapsed_summary(page) -> str:
    try:
        text = page.evaluate(_REACH_OUT_SUMMARY_JS)
        return str(text or "").strip()[:200]
    except Exception:
        return ""


def _apply_reach_out_scrape_to_job(
    job: Job,
    people: List[Dict[str, str]],
    summary: str,
    *,
    prior_ok: bool,
) -> None:
    if not isinstance(job.raw, dict):
        job.raw = {}
    if people:
        job.raw["reach_out_people"] = people
        job.raw["reach_out_source"] = REACH_OUT_LINKEDIN_SOURCE
        job.raw.pop("reach_out_summary", None)
    elif prior_ok:
        pass
    else:
        job.raw["reach_out_people"] = []
        job.raw.pop("reach_out_source", None)
    if summary and not reach_out_people_have_full_names(job.raw.get("reach_out_people") or []):
        job.raw["reach_out_summary"] = summary
    elif people:
        job.raw.pop("reach_out_summary", None)


def _extract_reach_out_people(page) -> List[Dict[str, str]]:
    try:
        found = _wait_for_reach_out_section(page, timeout_ms=18_000)
        _scroll_job_details_pane(page)
        _scroll_reach_out_into_view(page)
        time.sleep(1.2 if found else 2.0)
        raw = page.evaluate(_REACH_OUT_EXTRACT_JS)
        # LinkedIn usually shows 1–3 faces inline; full list is behind «Show all».
        if found or (isinstance(raw, list) and len(raw) > 0):
            _click_reach_out_show_all(page)
            time.sleep(2.5)
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
        full = person_display_name(
            {
                "name": str(item.get("name") or ""),
                "first_name": str(item.get("first_name") or ""),
                "last_name": str(item.get("last_name") or ""),
            }
        )
        if not full:
            continue
        row = {
            "name": full[:80],
            "first_name": str(item.get("first_name") or "").strip()[:40],
            "last_name": str(item.get("last_name") or "").strip()[:40],
            "role": str(item.get("role") or "").strip()[:120],
            "profile_url": str(item.get("profile_url") or "").strip(),
        }
        if is_usable_reach_out_person(row):
            out.append(row)
    return out


_JOB_VIEW_DETAIL_JS = """
() => {
  const pickText = (selectors) => {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (t) return t.split('·')[0].trim();
    }
    return '';
  };
  let title = pickText([
    'h1.job-details-jobs-unified-top-card__job-title',
    'h1.top-card-layout__title',
    'h1.t-24',
    'h1'
  ]);
  let company = pickText([
    '.job-details-jobs-unified-top-card__company-name a',
    '.topcard__org-name-link',
    'a[data-tracking-control-name="public_jobs_topcard-org-name"]',
    '.jobs-unified-top-card__company-name a',
    '.job-details-jobs-unified-top-card__company-name'
  ]);
  let location = pickText([
    '.job-details-jobs-unified-top-card__bullet',
    '.topcard__flavor--bullet',
    '.jobs-unified-top-card__bullet',
    '.job-details-jobs-unified-top-card__primary-description-container'
  ]);
  const scripts = document.querySelectorAll('script[type="application/ld+json"]');
  for (const node of scripts) {
    try {
      const data = JSON.parse(node.textContent || '');
      const items = Array.isArray(data) ? data : [data];
      for (const d of items) {
        if (!d || (d['@type'] !== 'JobPosting' && !(d['@type'] || '').includes('JobPosting'))) continue;
        title = title || (d.title || d.name || '').trim();
        const hirer = d.hiringOrganization || d.employer;
        if (hirer && typeof hirer === 'object') company = company || (hirer.name || '').trim();
        const loc = d.jobLocation;
        if (loc) {
          if (typeof loc === 'string') location = location || loc.trim();
          else if (Array.isArray(loc) && loc[0] && loc[0].address) {
            const a = loc[0].address;
            location = location || [a.addressLocality, a.addressRegion, a.addressCountry].filter(Boolean).join(', ');
          } else if (loc.address) {
            const a = loc.address;
            location = location || [a.addressLocality, a.addressRegion, a.addressCountry].filter(Boolean).join(', ');
          }
        }
      }
    } catch (e) { /* ignore */ }
  }
  const ogTitle = (document.querySelector('meta[property="og:title"]') || {}).content || '';
  if (ogTitle && !title) {
    const m = ogTitle.match(/^(.+?)\\s+hiring\\s+(.+?)\\s+in\\s+(.+?)\\s*\\|/i);
    if (m) {
      company = company || m[1].trim();
      title = title || m[2].trim();
      location = location || m[3].trim();
    }
  }
  if (!location) {
    const locRe = /Israel|ישראל|Remote|Hybrid|Tel Aviv|Herzliya|Haifa|Jerusalem/i;
    const nodes = document.querySelectorAll(
      '.topcard__flavor, .jobs-unified-top-card__bullet, .job-details-jobs-unified-top-card__primary-description-container span'
    );
    for (const el of nodes) {
      const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
      if (t && t.length < 100 && locRe.test(t)) {
        location = t;
        break;
      }
    }
  }
  return { title, company, location };
}
"""


_OG_TITLE_RE = re.compile(
    r'property="og:title"\s+content="([^"]+)"',
    re.I,
)


def _parse_linkedin_og_title(og_title: str) -> Dict[str, str]:
    from job_agent.linkedin_og import parse_linkedin_hiring_title

    return parse_linkedin_hiring_title(og_title)


def fetch_linkedin_job_details_http(link: str) -> Dict[str, str]:
    """Fetch job title/company/location from public LinkedIn HTML (no browser)."""
    from job_agent.linkedin_og import fetch_linkedin_og_details_http

    return fetch_linkedin_og_details_http(link)


def _infer_source_from_link(link: str) -> str:
    low = (link or "").lower()
    if "linkedin.com/jobs" in low:
        return "linkedin_browser"
    if "boards.greenhouse.io" in low:
        return "greenhouse"
    if "jobs.lever.co" in low or "lever.co" in low:
        return "lever"
    return ""


def fetch_linkedin_job_details(page, view_url: str, cfg: Dict[str, Any] | None = None) -> Dict[str, str]:
    """Read title / company / location from a LinkedIn job (view page or jobs search split)."""
    key = normalize_url(view_url.split("?")[0])
    jid = _job_id_from_href(view_url)
    out: Dict[str, str] = {}
    try:
        page.goto(view_url.split("?")[0], wait_until="domcontentloaded", timeout=90_000)
        try:
            page.wait_for_selector(
                'h1, meta[property="og:title"], script[type="application/ld+json"], a[href*="/jobs/view/"]',
                timeout=15_000,
            )
        except Exception:
            pass
        time.sleep(1.5)
        if "authwall" not in (page.url or "").lower():
            raw = page.evaluate(_JOB_VIEW_DETAIL_JS)
            if isinstance(raw, dict):
                out = {
                    "title": str(raw.get("title") or "").strip()[:300],
                    "company": str(raw.get("company") or "").strip()[:120],
                    "location": str(raw.get("location") or "").strip()[:120],
                }
    except Exception:
        pass

    if (out.get("title") and out.get("company")) or not jid or not cfg:
        return out

    # Fallback: open the job in the jobs search split pane (same UI as main fetch).
    try:
        search_url = build_linkedin_jobs_search_url(cfg)
        split_url = _search_url_with_job_id(search_url, jid)
        _dismiss_modal(page)
        page.goto(split_url, wait_until="domcontentloaded", timeout=90_000)
        time.sleep(1.2)
        for card in _extract_cards_from_page(page):
            if normalize_url(card.get("href") or "") == key:
                return {
                    "title": card.get("title") or out.get("title") or "",
                    "company": card.get("company") or out.get("company") or "",
                    "location": card.get("location") or out.get("location") or "",
                }
    except Exception:
        pass
    return out


def _apply_details_to_removed_record(rec: Dict[str, Any], details: Dict[str, str], link: str) -> bool:
    if not details:
        return False
    if details.get("title"):
        rec["title"] = details["title"]
    if details.get("company"):
        rec["company"] = details["company"]
    if details.get("location"):
        rec["location"] = details["location"]
    if details.get("source") and not rec.get("source"):
        rec["source"] = details["source"]
    return bool(details.get("title") or details.get("company"))


def enrich_removed_records(records: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fill missing title/company/location for hidden jobs (legacy link-only rows)."""
    from job_agent.ignore_store import record_needs_detail, save_all_removed_records
    from job_agent.job_page_details import fetch_job_page_details_http

    if not records:
        return records
    by_link = {normalize_url(str(r.get("link") or "")): dict(r) for r in records if r.get("link")}
    http_enriched = 0
    for link, rec in list(by_link.items()):
        if not record_needs_detail(rec):
            continue
        try:
            details = fetch_job_page_details_http(link)
            if _apply_details_to_removed_record(rec, details, link):
                http_enriched += 1
                print(
                    f"Removed job details (HTTP): {rec.get('company') or '?'} — {(rec.get('title') or '')[:60]}",
                    file=sys.stderr,
                )
            by_link[link] = rec
        except Exception as exc:
            print(f"Removed job HTTP enrich skip {link}: {exc}", file=sys.stderr)

    need_browser = [r for r in by_link.values() if record_needs_detail(r) and "linkedin.com/jobs" in str(r.get("link") or "").lower()]
    browser_enriched = 0
    if need_browser and playwright_available():
        pause = float(_jobs_search_block(cfg).get("reach_out_pause_seconds") or 1.8)
        pw, context = with_linkedin_context(cfg)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for rec in need_browser:
                link = str(rec.get("link") or "")
                try:
                    details = fetch_linkedin_job_details_http(link)
                    if not (details.get("title") and details.get("company")):
                        _dismiss_modal(page)
                        details = fetch_linkedin_job_details(page, link, cfg)
                    elif not details.get("location"):
                        extra = fetch_linkedin_job_details(page, link, cfg)
                        details["location"] = extra.get("location") or details.get("location") or ""
                    if _apply_details_to_removed_record(rec, details, link):
                        browser_enriched += 1
                        print(
                            f"Removed job details (browser): {rec.get('company') or '?'} — "
                            f"{(rec.get('title') or '')[:60]}",
                            file=sys.stderr,
                        )
                    by_link[link] = rec
                    time.sleep(pause)
                except Exception as exc:
                    print(f"Removed job enrich skip {link}: {exc}", file=sys.stderr)
        finally:
            context.close()
            pw.stop()
    elif need_browser:
        print("Removed jobs: LinkedIn rows still missing details (Playwright not installed).", file=sys.stderr)

    out = list(by_link.values())
    total = http_enriched + browser_enriched
    if total or any(record_needs_detail(r) for r in out):
        save_all_removed_records(out, cfg)
    if total:
        print(f"Removed jobs: enriched {total} row(s) ({http_enriched} HTTP, {browser_enriched} browser).", file=sys.stderr)
    elif any(record_needs_detail(r) for r in out):
        print(
            "Removed jobs: some rows still missing title/company. "
            "Re-remove from a fresh digest to save a full snapshot, or edit ~/.job-agent/digest_ignore_links.json.",
            file=sys.stderr,
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
        if for_email:
            need.append(job)
            continue
        if linkedin_reach_out_snapshot_ok(raw):
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
    if for_email:
        pause = max(pause, 2.5)
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
        raw = job.raw if isinstance(job.raw, dict) else {}
        if not for_email and linkedin_reach_out_snapshot_ok(raw):
            if reach_out_people_have_full_names(raw.get("reach_out_people") or []):
                with_people += 1
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
            prior_ok = linkedin_reach_out_snapshot_ok(raw) and not for_email
            people = _extract_reach_out_people(page)
            summary = _extract_reach_out_collapsed_summary(page)
            _apply_reach_out_scrape_to_job(job, people, summary, prior_ok=prior_ok)
            if people:
                with_people += 1
                print(
                    f"LinkedIn reach-out: {len(people)} at {job.company or '?'} — {job.title[:50]}",
                    file=sys.stderr,
                )
            elif summary:
                with_people += 1
                print(
                    f"LinkedIn reach-out: summary only for {job.company or '?'} — {summary[:60]}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"LinkedIn reach-out: 0 names for job {jid} "
                    f"({job.company or '?'}) — section missing or no 1st-degree matches",
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
