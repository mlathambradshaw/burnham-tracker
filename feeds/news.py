"""
feeds/news.py — Fetch and parse news/think-tank RSS feeds.
"""

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

import feedparser

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import NEWS_FEEDS, SOURCE_COLORS, DEFAULT_COLOR, LOOKBACK_HOURS_BY_CATEGORY, MAX_ARTICLES_PER_FEED

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    if not text:
        return ""
    return _TAG_RE.sub("", text).strip()


def _article_id(url: str) -> str:
    """Stable MD5-based identifier derived from the article URL."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _parse_published(entry) -> datetime:
    """
    Return a timezone-aware datetime for the feed entry.
    Tries published_parsed then updated_parsed; falls back to utcnow.
    """
    for attr in ("published_parsed", "updated_parsed"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                # feedparser returns time.struct_time in UTC
                import calendar
                ts = calendar.timegm(raw)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass
    return datetime.now(tz=timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _entry_url(entry) -> str:
    """Best-effort URL extraction from a feed entry."""
    return getattr(entry, "link", "") or getattr(entry, "id", "") or ""


def _entry_summary(entry) -> str:
    """Extract and clean a short summary from a feed entry."""
    raw = ""
    for attr in ("summary", "description", "content"):
        val = getattr(entry, attr, None)
        if val:
            if isinstance(val, list):
                # Atom content is a list of dicts
                val = " ".join(
                    v.get("value", "") for v in val if isinstance(v, dict)
                )
            raw = str(val)
            break
    cleaned = _strip_html(raw)
    return cleaned[:400]  # increased from 300 for better Claude context


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BradshawDashboard/2.0)"}
_FETCH_TIMEOUT = 12   # seconds per feed
_MAX_WORKERS   = 10   # parallel fetches


def _cutoff_for(feed_cfg: dict) -> datetime:
    category = feed_cfg.get("category", "other")
    hours = LOOKBACK_HOURS_BY_CATEGORY.get(category, 168)
    return datetime.now(tz=timezone.utc) - timedelta(hours=hours)


def _fetch_one_feed(source_name: str, feed_cfg: dict, cutoff: datetime) -> list[dict]:
    """Fetch a single RSS feed and return a list of article dicts."""
    url = feed_cfg["url"]
    try:
        parsed = feedparser.parse(
            url,
            request_headers=_HEADERS,
            handlers=[],
            response_headers={"content-location": url},
        )
        if parsed.get("bozo") and not parsed.get("entries"):
            log.warning("Feed bozo for %s: %s", source_name, parsed.get("bozo_exception", ""))
            return []

        feed_articles: list[dict] = []
        for entry in parsed.entries:
            published_dt = _parse_published(entry)
            if published_dt < cutoff:
                continue

            article_url = _entry_url(entry)
            if not article_url:
                continue

            title = _strip_html(getattr(entry, "title", "") or "")
            if not title:
                continue

            feed_articles.append({
                "id":            _article_id(article_url),
                "title":         title,
                "url":           article_url,
                "source":        source_name,
                "source_color":  SOURCE_COLORS.get(source_name, DEFAULT_COLOR),
                "published_iso": _to_iso(published_dt),
                "summary":       _entry_summary(entry),
                "tab":           "news",
                "parliament_type": None,
            })

        trimmed = feed_articles[:MAX_ARTICLES_PER_FEED]
        log.debug("  %s → %d articles", source_name, len(trimmed))
        return trimmed

    except Exception as exc:
        log.warning("Failed to fetch '%s' (%s): %s", source_name, url, exc)
        return []


def fetch_all_news_feeds() -> list[dict]:
    """
    Fetch all configured news RSS feeds in parallel.

    Returns a list of article dicts sorted by published_iso descending.
    Each dict contains: id, title, url, source, source_color,
    published_iso, summary, tab, parliament_type.
    """
    all_articles: list[dict] = []

    log.info("Fetching %d news feeds in parallel (max %d workers)…",
             len(NEWS_FEEDS), _MAX_WORKERS)

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one_feed, name, cfg, _cutoff_for(cfg)): name
            for name, cfg in NEWS_FEEDS.items()
        }
        for future in as_completed(futures):
            try:
                all_articles.extend(future.result())
            except Exception as exc:
                log.warning("Unexpected error for feed %s: %s", futures[future], exc)

    # Deduplicate by article id (Politico sub-feeds can overlap with main feed)
    seen: set[str] = set()
    deduped: list[dict] = []
    for article in all_articles:
        if article["id"] not in seen:
            seen.add(article["id"])
            deduped.append(article)

    deduped.sort(key=lambda a: a["published_iso"], reverse=True)
    log.info("Total news articles after dedup: %d", len(deduped))
    return deduped
