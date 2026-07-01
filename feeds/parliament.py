"""
feeds/parliament.py — Fetch UK Parliament data.

Sources:
  1. Parliament Written Questions API  (questions-statements-api.parliament.uk)
  2. Parliament Written Statements API (questions-statements-api.parliament.uk)
  3. Committee publications — via Google News site:committees.parliament.uk
  4. UK Parliament Bills (recently updated, keyword-filtered)
"""

import hashlib
import logging
import re
import calendar
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

import feedparser
import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    SOURCE_COLORS, DEFAULT_COLOR, LOOKBACK_HOURS_BY_CATEGORY,
    PARLIAMENT_KEYWORD_FILTERS, PARLIAMENT_COMMITTEES,
)

LOOKBACK_HOURS = LOOKBACK_HOURS_BY_CATEGORY.get("other", 336)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# API base — swagger shows /api/writtenquestions/questions and /api/writtenstatements/statements
_WQ_BASE  = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
_WS_BASE  = "https://questions-statements-api.parliament.uk/api/writtenstatements/statements"
_BILLS_BASE = (
    "https://bills-api.parliament.uk/api/v1/Bills"
    "?CurrentHouse=All&IsDefeated=false"
    "&SortOrder=DateUpdatedDescending&Take=50"
)
_GNEWS = "https://news.google.com/rss/search?q={query}&hl=en-GB&gl=GB&ceid=GB:en"

REQUEST_TIMEOUT = 15  # seconds

_PARLIAMENT_COLOR = "#6c3483"
_COMMITTEE_COLOR  = "#1d70b8"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BradshawDashboard/2.0)"}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _TAG_RE.sub("", text).strip()


def _article_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _parse_iso_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    # Normalise fractional seconds to 6 digits
    if "." in date_str:
        base, frac = date_str.split(".", 1)
        tz_part = ""
        for sep in ("Z", "+", "-"):
            idx = frac.find(sep)
            if idx != -1:
                tz_part = frac[idx:]
                frac = frac[:idx]
                break
        date_str = f"{base}.{frac[:6]}{tz_part}"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    try:
        from dateutil import parser as dp
        return dp.parse(date_str).astimezone(timezone.utc)
    except Exception:
        return None


def _feedparser_to_dt(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                ts = calendar.timegm(raw)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass
    return datetime.now(tz=timezone.utc)


def _passes_keyword_filter(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in PARLIAMENT_KEYWORD_FILTERS)


# ---------------------------------------------------------------------------
# Source 1: Written Questions
# ---------------------------------------------------------------------------

def _fetch_written_questions(cutoff: datetime) -> list[dict]:
    articles: list[dict] = []
    date_from = cutoff.strftime("%Y-%m-%d")
    url = (
        f"{_WQ_BASE}?take=100&skip=0&expandMember=true"
        f"&dateFrom={date_from}"
    )
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])

        for item in results:
            val = item.get("value", {})
            item_id = val.get("id")
            if not item_id:
                continue

            uin        = val.get("uin", str(item_id))
            date_str   = (val.get("dateTabled") or "")[:10]
            entry_url  = (
                f"https://questions-statements.parliament.uk/"
                f"written-questions/detail/{date_str}/{uin}"
            )

            heading       = _strip_html(val.get("heading") or "")
            question_text = _strip_html(val.get("questionText") or "")
            title = heading if heading else question_text[:120]
            if not title:
                continue

            published_dt = _parse_iso_date(val.get("dateTabled")) or datetime.now(tz=timezone.utc)
            if published_dt < cutoff:
                continue

            # Build summary: question + answer snippet if available
            answer_text = _strip_html(val.get("answerText") or "")
            if answer_text:
                summary = f"Q: {question_text[:250]}\nA: {answer_text[:200]}"
            else:
                summary = question_text[:400]

            combined = f"{title} {summary}"
            if not _passes_keyword_filter(combined):
                continue

            articles.append({
                "id":             _article_id(entry_url),
                "title":          title,
                "url":            entry_url,
                "source":         "Parliament — Written Questions",
                "source_color":   _PARLIAMENT_COLOR,
                "published_iso":  published_dt.isoformat(),
                "summary":        summary,
                "tab":            "parliament",
                "parliament_type": "written_question",
            })

    except requests.RequestException as exc:
        log.warning("Failed to fetch written questions: %s", exc)
    except Exception as exc:
        log.warning("Unexpected error fetching written questions: %s", exc)

    log.info("Written questions: %d relevant", len(articles))
    return articles


# ---------------------------------------------------------------------------
# Source 2: Written Statements
# ---------------------------------------------------------------------------

def _fetch_written_statements(cutoff: datetime) -> list[dict]:
    articles: list[dict] = []
    date_from = cutoff.strftime("%Y-%m-%d")
    url = (
        f"{_WS_BASE}?take=100&skip=0&expandMember=true"
        f"&dateFrom={date_from}"
    )
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])

        for item in results:
            val = item.get("value", {})
            item_id = val.get("id")
            if not item_id:
                continue

            uin      = val.get("uin", str(item_id))
            date_str = (val.get("dateMade") or "")[:10]
            entry_url = (
                f"https://questions-statements.parliament.uk/"
                f"written-statements/detail/{date_str}/{uin}"
            )

            title   = _strip_html(val.get("title") or "Written Statement")
            text    = _strip_html(val.get("text") or "")
            summary = text[:400]
            if not title:
                continue

            published_dt = _parse_iso_date(val.get("dateMade")) or datetime.now(tz=timezone.utc)
            if published_dt < cutoff:
                continue

            combined = f"{title} {summary}"
            if not _passes_keyword_filter(combined):
                continue

            articles.append({
                "id":             _article_id(entry_url),
                "title":          title,
                "url":            entry_url,
                "source":         "Parliament — Written Statements",
                "source_color":   _PARLIAMENT_COLOR,
                "published_iso":  published_dt.isoformat(),
                "summary":        summary,
                "tab":            "parliament",
                "parliament_type": "written_statement",
            })

    except requests.RequestException as exc:
        log.warning("Failed to fetch written statements: %s", exc)
    except Exception as exc:
        log.warning("Unexpected error fetching written statements: %s", exc)

    log.info("Written statements: %d relevant", len(articles))
    return articles


# ---------------------------------------------------------------------------
# Source 3: Committee publications via Google News
# ---------------------------------------------------------------------------

def _committee_gnews_url(committee_name: str) -> str:
    """
    Build a Google News RSS URL that searches committees.parliament.uk
    for articles mentioning this committee by name.
    """
    query = urllib.parse.quote(
        f'site:committees.parliament.uk "{committee_name}"'
    )
    return _GNEWS.format(query=query)


def _fetch_one_committee(committee: dict, cutoff: datetime) -> list[dict]:
    name = committee["name"]
    url  = _committee_gnews_url(name)
    articles: list[dict] = []
    try:
        parsed = feedparser.parse(url, request_headers=_HEADERS)
        if parsed.get("bozo") and not parsed.get("entries"):
            log.debug("No entries from committee feed '%s'", name)
            return []

        for entry in parsed.entries:
            entry_url = getattr(entry, "link", "") or getattr(entry, "id", "")
            if not entry_url:
                continue

            title = _strip_html(getattr(entry, "title", "") or "")
            if not title:
                continue

            published_dt = _feedparser_to_dt(entry)
            if published_dt < cutoff:
                continue

            raw_summary = ""
            for attr in ("summary", "description"):
                val = getattr(entry, attr, None)
                if val:
                    raw_summary = val
                    break
            summary = _strip_html(raw_summary)[:400]

            articles.append({
                "id":             _article_id(entry_url),
                "title":          title,
                "url":            entry_url,
                "source":         name,
                "source_color":   SOURCE_COLORS.get(name, _COMMITTEE_COLOR),
                "published_iso":  published_dt.isoformat(),
                "summary":        summary,
                "tab":            "parliament",
                "parliament_type": "committee",
            })

    except Exception as exc:
        log.warning("Failed to fetch committee '%s': %s", name, exc)

    return articles


def _fetch_all_committees(cutoff: datetime) -> list[dict]:
    articles: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(_fetch_one_committee, c, cutoff): c["name"]
            for c in PARLIAMENT_COMMITTEES
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                articles.extend(result)
                log.info("Committee '%s': %d articles", futures[future], len(result))
            except Exception as exc:
                log.warning("Committee fetch error (%s): %s", futures[future], exc)
    return articles


# ---------------------------------------------------------------------------
# Source 4: Parliament Bills (recently updated)
# ---------------------------------------------------------------------------

def _fetch_parliament_bills(cutoff: datetime) -> list[dict]:
    articles: list[dict] = []
    try:
        resp = requests.get(_BILLS_BASE, timeout=REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])

        for bill in items:
            bill_id = bill.get("billId")
            title   = _strip_html(bill.get("shortTitle") or bill.get("longTitle") or "")
            if not title or not bill_id:
                continue

            url = f"https://bills.parliament.uk/bills/{bill_id}"

            last_update  = bill.get("lastUpdate", "")
            published_dt = _parse_iso_date(last_update) or datetime.now(tz=timezone.utc)
            if published_dt < cutoff:
                continue

            stage      = (bill.get("currentStage") or {}).get("description", "")
            originating = bill.get("originatingHouse", "")
            is_act     = bill.get("isAct", False)
            parts      = []
            if stage:
                parts.append(f"Stage: {stage}")
            if originating:
                parts.append(f"Originating: {originating}")
            if is_act:
                parts.append("Now an Act of Parliament")
            summary = " | ".join(parts)

            combined = f"{title} {summary}"
            if not _passes_keyword_filter(combined):
                continue

            articles.append({
                "id":             _article_id(url),
                "title":          f"Bill: {title}",
                "url":            url,
                "source":         "Parliament — Bills",
                "source_color":   _PARLIAMENT_COLOR,
                "published_iso":  published_dt.isoformat(),
                "summary":        summary,
                "tab":            "parliament",
                "parliament_type": "bill",
            })

    except requests.RequestException as exc:
        log.warning("Failed to fetch Parliament Bills: %s", exc)
    except Exception as exc:
        log.warning("Unexpected error fetching Parliament Bills: %s", exc)

    log.info("Parliament Bills: %d relevant", len(articles))
    return articles


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_parliament_updates() -> list[dict]:
    """
    Fetch all Parliament data sources in parallel where possible.
    Returns a combined list of article dicts sorted by published_iso descending.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_articles: list[dict] = []

    # Run the three API sources concurrently
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_questions  = pool.submit(_fetch_written_questions, cutoff)
        f_statements = pool.submit(_fetch_written_statements, cutoff)
        f_bills      = pool.submit(_fetch_parliament_bills, cutoff)

        for f in (f_questions, f_statements, f_bills):
            try:
                all_articles.extend(f.result())
            except Exception as exc:
                log.warning("Parliament API source failed: %s", exc)

    # Fetch committees (internally parallelised)
    log.info("Fetching %d committee feeds via Google News…", len(PARLIAMENT_COMMITTEES))
    all_articles.extend(_fetch_all_committees(cutoff))

    # Deduplicate by id
    seen: set[str] = set()
    unique: list[dict] = []
    for article in all_articles:
        if article["id"] not in seen:
            seen.add(article["id"])
            unique.append(article)

    unique.sort(key=lambda a: a["published_iso"], reverse=True)
    log.info("Total Parliament articles fetched: %d", len(unique))
    return unique
