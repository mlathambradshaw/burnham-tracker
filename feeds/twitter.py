"""
feeds/twitter.py — Fetch Andy Burnham's X / Twitter posts via an RSSHub bridge.

The free Twitter/X API does not allow reading at scale, so we use RSSHub
(https://docs.rsshub.app) which exposes a public account's tweets as RSS.
Several public instances are tried in order; if none respond the fetcher
returns an empty list rather than breaking the pipeline. Tweets are his own
words, so they bypass the news keyword pre-filter and go straight to scoring.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone, timedelta

import feedparser

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    BURNHAM_HANDLE, RSSHUB_INSTANCES, SOURCE_COLORS, DEFAULT_COLOR,
    LOOKBACK_HOURS_BY_CATEGORY, MAX_ARTICLES_PER_FEED,
)

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BurnhamTracker/1.0)"}
_SOURCE_NAME = "Andy Burnham (X)"


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _TAG_RE.sub("", text).strip()


def _article_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _published_dt(entry) -> datetime:
    import calendar
    for attr in ("published_parsed", "updated_parsed"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                return datetime.fromtimestamp(calendar.timegm(raw), tz=timezone.utc)
            except Exception:
                pass
    return datetime.now(tz=timezone.utc)


def _parse_instance(instance: str, cutoff: datetime) -> list[dict]:
    """Try one RSSHub instance. Return article dicts, or [] on failure."""
    url = f"{instance.rstrip('/')}/twitter/user/{BURNHAM_HANDLE}"
    try:
        parsed = feedparser.parse(url, request_headers=_HEADERS)
        if not parsed.entries:
            log.debug("RSSHub instance %s returned no entries", instance)
            return []

        posts: list[dict] = []
        for entry in parsed.entries:
            published_dt = _published_dt(entry)
            if published_dt < cutoff:
                continue

            tweet_url = getattr(entry, "link", "") or getattr(entry, "id", "")
            if not tweet_url:
                continue

            text = _strip_html(
                getattr(entry, "title", "") or getattr(entry, "summary", "")
            )
            if not text:
                continue

            posts.append({
                "id":             _article_id(tweet_url),
                "title":          text[:120],
                "url":            tweet_url,
                "source":         _SOURCE_NAME,
                "source_color":   SOURCE_COLORS.get(_SOURCE_NAME, DEFAULT_COLOR),
                "published_iso":  published_dt.isoformat(),
                "summary":        text,
                "tab":            "news",
                "parliament_type": None,
                "is_own_words":   True,   # straight from his account
            })

        log.info("RSSHub %s → %d tweets", instance, len(posts))
        return posts[:MAX_ARTICLES_PER_FEED]

    except Exception as exc:
        log.warning("RSSHub instance %s failed: %s", instance, exc)
        return []


def fetch_twitter_posts() -> list[dict]:
    """
    Fetch @AndyBurnhamGM tweets via the first working RSSHub instance.
    Returns [] if every instance fails (manual-paste fallback can fill the gap).
    """
    hours = LOOKBACK_HOURS_BY_CATEGORY.get("social", 720)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

    for instance in RSSHUB_INSTANCES:
        posts = _parse_instance(instance, cutoff)
        if posts:
            return posts

    log.warning(
        "No RSSHub instance returned tweets for @%s — X source empty this run",
        BURNHAM_HANDLE,
    )
    return []
