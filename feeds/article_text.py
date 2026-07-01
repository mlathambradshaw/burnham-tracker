"""
feeds/article_text.py — Fetch and extract the main body text of an article.

RSS summaries are usually just a headline, so the body is where Burnham's
quotes actually live. For each article that passes the keyword pre-filter we
fetch the page and pull the readable text, which then becomes the article's
'summary' for the AI filter/extraction steps. Failures (paywalls, timeouts,
blocks) degrade gracefully to the original RSS summary.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import PAYWALLED_DOMAINS

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}
_TIMEOUT = 12
_MAX_WORKERS = 8
_MIN_BODY = 400      # below this we don't trust the extraction
_MAX_BODY = 6000     # cap sent to the model

_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.I | re.S)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

try:
    from bs4 import BeautifulSoup
    _HAVE_BS4 = True
except Exception:
    _HAVE_BS4 = False


def _clean(text: str) -> str:
    text = _TAG_RE.sub("", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&#39;", "'").replace("&quot;", '"')
                .replace("&rsquo;", "’").replace("&lsquo;", "‘")
                .replace("&ldquo;", "“").replace("&rdquo;", "”"))
    return _WS_RE.sub(" ", text).strip()


def _is_paywalled(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in PAYWALLED_DOMAINS)


def _extract_body(html: str) -> str:
    """Pull readable paragraph text from an article page."""
    if _HAVE_BS4:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript", "aside", "figure"]):
                tag.decompose()
            root = soup.find("article") or soup.find("main") or soup
            paras = [p.get_text(" ", strip=True) for p in root.find_all("p")]
            body = " ".join(p for p in paras if len(p) > 40)
            return _WS_RE.sub(" ", body).strip()[:_MAX_BODY]
        except Exception:
            pass
    # Regex fallback
    html = _SCRIPT_RE.sub(" ", html)
    paras = [_clean(m.group(1)) for m in _P_RE.finditer(html)]
    body = " ".join(p for p in paras if len(p) > 40)
    return body[:_MAX_BODY]


def fetch_text(url: str) -> str:
    """Return the article body text, or '' if unavailable/too short."""
    if not url or _is_paywalled(url):
        return ""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
            return ""
        body = _extract_body(r.text)
        return body if len(body) >= _MIN_BODY else ""
    except Exception as exc:
        log.debug("Article fetch failed for %s: %s", url, exc)
        return ""


def enrich_with_fulltext(articles: list[dict], cap: int = 80) -> list[dict]:
    """
    Fetch body text for news articles (in place) so the AI sees real content.
    Tweets/own-words items and parliament items are left as-is. When a body is
    retrieved it replaces 'summary'; the original is kept as 'rss_summary'.
    """
    targets = [
        a for a in articles
        if not a.get("is_own_words")
        and a.get("tab") == "news"
        and a.get("url")
    ][:cap]
    if not targets:
        return articles

    log.info("Fetching full text for %d articles (%d concurrent)…",
             len(targets), _MAX_WORKERS)
    fetched = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_text, a["url"]): a for a in targets}
        for fut in as_completed(futures):
            a = futures[fut]
            try:
                body = fut.result()
            except Exception:
                body = ""
            if body:
                a["rss_summary"] = a.get("summary", "")
                a["summary"] = body
                a["fulltext"] = True
                fetched += 1
    log.info("Full text retrieved for %d/%d articles", fetched, len(targets))
    return articles
