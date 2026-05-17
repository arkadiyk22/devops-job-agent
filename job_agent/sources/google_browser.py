"""Google Web job search via your logged-in Chromium profile (no SerpAPI)."""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Dict, List
from urllib.parse import quote_plus

from job_agent.browser.session import open_google_login, playwright_available, with_google_context
from job_agent.models import Job
from job_agent.query_build import build_ats_google_site_queries, build_google_browser_queries
from job_agent.scoring import score_title
from job_agent.sources.google_site_ats import (
    _is_probable_job_url,
    _site_label_from_query,
    _split_title_company,
)
from job_agent.linkedin_og import is_linkedin_post_url, split_linkedin_google_result
from job_agent.util import normalize_url

_CAPTCHA_RE = re.compile(r"unusual traffic|captcha|sorry, we can't verify", re.I)


def _google_web_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    block = cfg.get("google_web_browser")
    return block if isinstance(block, dict) else {}


def google_login(cfg: Dict[str, Any], *, wait_minutes: int = 10) -> bool:
    return open_google_login(cfg, wait_minutes=wait_minutes)


def _search_base_url(cfg: Dict[str, Any]) -> str:
    raw = (_google_web_block(cfg).get("search_url") or "https://www.google.co.il/search").strip()
    return raw.rstrip("/")


def _build_search_url(cfg: Dict[str, Any], query: str) -> str:
    base = _search_base_url(cfg)
    q = quote_plus(query)
    # hl/gl help Israel-focused results when logged in
    return f"{base}?q={q}&hl=he&gl=il"


def _page_has_captcha(page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False
    return bool(_CAPTCHA_RE.search(body or ""))


def _extract_organic_results(page, *, max_results: int) -> List[Dict[str, str]]:
    cap = max(1, max_results)
    script = """
    (maxN) => {
      const out = [];
      const seen = new Set();
      const push = (href, title, snippet) => {
        if (!href || !href.startsWith('http') || seen.has(href)) return;
        if (href.includes('google.com/') || href.includes('google.co.il/')) return;
        if (href.includes('webcache.googleusercontent')) return;
        seen.add(href);
        out.push({
          href,
          title: (title || '').trim().slice(0, 300),
          snippet: (snippet || '').trim().slice(0, 500),
        });
      };
      const blocks = document.querySelectorAll('#search .g, div[data-sokoban-container] div[data-hveid]');
      for (const block of blocks) {
        const a = block.querySelector('a[href^="http"] h3')
          ? block.querySelector('a[href^="http"]:has(h3)')
          : block.querySelector('a[href^="http"]');
        if (!a) continue;
        const h3 = a.querySelector('h3');
        const title = h3 ? (h3.innerText || '') : (a.innerText || '');
        const href = a.href || '';
        let snippet = '';
        const sn = block.querySelector('[data-sncf], .VwiC3b, .IsZvec, .st');
        if (sn) snippet = sn.innerText || '';
        push(href, title, snippet);
        if (out.length >= maxN) break;
      }
      if (out.length < maxN) {
        document.querySelectorAll('a[href^="http"] h3').forEach(h3 => {
          if (out.length >= maxN) return;
          const a = h3.closest('a');
          if (!a) return;
          const card = h3.closest('.g') || h3.parentElement;
          let snippet = '';
          if (card) {
            const sn = card.querySelector('[data-sncf], .VwiC3b, .IsZvec, .st');
            if (sn) snippet = sn.innerText || '';
          }
          push(a.href || '', h3.innerText || '', snippet);
        });
      }
      return out;
    }
    """
    try:
        rows = page.evaluate(script, cap)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        href = str(row.get("href") or "").strip()
        if href:
            out.append(
                {
                    "href": href,
                    "title": str(row.get("title") or "").strip(),
                    "snippet": str(row.get("snippet") or "").strip(),
                }
            )
    return out


def _guess_location(snippet: str, query: str) -> str:
    blob = f"{snippet} {query}"
    if re.search(r"\bIsrael\b|ישראל|Tel Aviv|תל אביב|Herzliya|Haifa|Jerusalem|Hybrid|Remote", blob, re.I):
        m = re.search(
            r"([A-Za-z\u0590-\u05FF][^|\\n]{0,80}(?:Israel|District|Hybrid|Remote|On-site)[^|\\n]{0,40})",
            snippet,
        )
        if m:
            return m.group(1).strip()[:120]
        if re.search(r"Israel|ישראל", blob, re.I):
            return "Israel"
    return ""


def fetch_google_web_browser(cfg: Dict[str, Any]) -> List[Job]:
    """Run configured ``site:`` Google queries in a persistent browser session."""
    block = _google_web_block(cfg)
    if not block.get("enabled", True):
        return []
    if not playwright_available():
        print(
            "Google browser: skipped (pip install playwright && playwright install chromium)",
            file=sys.stderr,
        )
        return []

    queries = build_google_browser_queries(cfg)
    if not queries:
        print("Google browser: no queries (enable ats_google_site_search or set google_web_browser.queries)", file=sys.stderr)
        return []

    max_q = max(1, int(block.get("max_queries_per_run") or 4))
    queries = queries[:max_q]
    per_query = max(3, int(block.get("results_per_query") or 12))
    pause = float(block.get("pause_seconds") or 3.0)

    out: List[Job] = []
    seen: set[str] = set()

    print(f"Google browser: {len(queries)} query/queries (profile: google)", file=sys.stderr)

    pw, context = with_google_context(cfg)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        for i, q in enumerate(queries):
            url = _build_search_url(cfg, q)
            print(f"Google browser: [{i + 1}/{len(queries)}] {q[:100]}{'…' if len(q) > 100 else ''}", file=sys.stderr)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            except Exception as exc:
                print(f"Google browser: navigation failed ({exc})", file=sys.stderr)
                continue
            time.sleep(pause)
            if _page_has_captcha(page):
                print(
                    "Google browser: CAPTCHA / unusual-traffic page — complete it in the profile "
                    "(`python3 run.py --google-login`), then rerun.",
                    file=sys.stderr,
                )
                break

            organic = _extract_organic_results(page, max_results=per_query)
            label = _site_label_from_query(q)
            for row in organic:
                link = row.get("href") or ""
                if not _is_probable_job_url(link):
                    continue
                link_n = normalize_url(link)
                if link_n in seen:
                    continue
                seen.add(link_n)
                title, company = _split_title_company(row.get("title") or "", row.get("snippet") or "", link_n)
                loc = _guess_location(row.get("snippet") or "", q)
                if is_linkedin_post_url(link_n):
                    t2, c2, loc2 = split_linkedin_google_result(
                        row.get("title") or "", row.get("snippet") or "", link_n
                    )
                    if t2:
                        title = t2
                    if c2:
                        company = c2
                    if loc2:
                        loc = loc2
                snippet = row.get("snippet") or ""
                out.append(
                    Job(
                        source=f"google_browser:{label}",
                        company=company,
                        title=title,
                        location=loc,
                        link=link_n,
                        posted="recent",
                        score=score_title(title, cfg),
                        raw={
                            "text": snippet,
                            "description": snippet,
                            "google_query": q,
                            "search_url": url,
                        },
                    )
                )
            if pause > 0 and i + 1 < len(queries):
                time.sleep(pause)
    finally:
        context.close()
        pw.stop()

    print(f"Google browser: collected {len(out)} job link(s)", file=sys.stderr)
    return out
