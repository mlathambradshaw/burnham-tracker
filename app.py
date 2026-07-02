"""
app.py — Flask web application for the Burnham Tracker.

Read-only intelligence dashboard tracking what Andy Burnham (and his inner
circle) are saying as he moves toward No. 10. See SPEC.md.

Pipeline:  fetch → keyword pre-filter → Claude filter → Claude extract →
store positions (Redis) → flip-flop pass.
"""

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from ai.extract import detect_flip_flops, extract_positions_streaming
from ai.filter import filter_articles_streaming
from feeds.article_text import enrich_with_fulltext
from feeds.news import fetch_all_news_feeds
from feeds.parliament import fetch_parliament_updates
from feeds.twitter import fetch_twitter_posts
from config import (
    BURNHAM_NAME_KEYWORDS, DEFAULT_COLOR, POLICY_AREAS, SOLIDITY_LEVELS,
    SOURCE_COLORS, TIMELINE_START_DATE,
)

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "burnham-dev-key-change-in-prod")

# ---------------------------------------------------------------------------
# Redis setup  (falls back to in-memory store if REDIS_URL is not set)
# ---------------------------------------------------------------------------

try:
    import redis as redis_lib
    _redis_url = os.environ.get("REDIS_URL", "")
    if _redis_url:
        _redis = redis_lib.from_url(
            _redis_url, decode_responses=True,
            socket_connect_timeout=5, socket_timeout=5,
        )
        _redis.ping()
        log.info("Redis connected ✓")
    else:
        _redis = None
        log.info("No REDIS_URL — using in-memory store")
except Exception as exc:
    _redis = None
    log.warning("Redis unavailable (%s) — using in-memory store", exc)

_local: dict = {}   # in-memory fallback keyed by redis key

# Optional local file persistence (used only when there is no Redis) so a
# locally-run server keeps its data across restarts and starts instantly.
_LOCAL_STORE_FILE = os.environ.get("LOCAL_STORE_FILE", "")
if not _redis and _LOCAL_STORE_FILE and os.path.exists(_LOCAL_STORE_FILE):
    try:
        with open(_LOCAL_STORE_FILE) as _f:
            _local.update(json.load(_f))
        log.info("Loaded local store from %s (%d keys)", _LOCAL_STORE_FILE, len(_local))
    except Exception as _exc:
        log.warning("Could not load local store: %s", _exc)


def _persist_local() -> None:
    if _redis or not _LOCAL_STORE_FILE:
        return
    try:
        with open(_LOCAL_STORE_FILE, "w") as f:
            json.dump(_local, f)
    except Exception as exc:
        log.debug("Local store write failed: %s", exc)

# Key names / tuning
_POSITIONS_KEY = "burnham:positions"
_META_KEY      = "burnham:meta"
_PROCESSED_TTL = 30 * 24 * 3600
_FILTER_TTL    = 30 * 24 * 3600
_MAX_POSITIONS = 1000


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Low-level Redis helpers (with in-memory fallback)
# ---------------------------------------------------------------------------

def _get(key: str) -> Optional[str]:
    try:
        return _redis.get(key) if _redis else _local.get(key)
    except Exception:
        return None


def _set(key: str, value: str, ttl: Optional[int] = None) -> None:
    try:
        if _redis:
            _redis.setex(key, ttl, value) if ttl else _redis.set(key, value)
        else:
            _local[key] = value
            _persist_local()
    except Exception:
        pass


# ── Filter verdict cache (consumed by ai.filter) ──────────────────────────

def _filter_key(url: str) -> str:
    return "burnham:filter:" + hashlib.md5(url.encode()).hexdigest()[:16]


def get_score(url: str) -> Optional[dict]:
    if not url:
        return None
    raw = _get(_filter_key(url))
    return json.loads(raw) if raw else None


def set_score(url: str, record: dict) -> None:
    if url:
        _set(_filter_key(url), json.dumps(record), _FILTER_TTL)


# ── Processed-article guard (makes extraction idempotent) ──────────────────

def _processed_key(url: str) -> str:
    return "burnham:processed:" + hashlib.md5(url.encode()).hexdigest()[:16]


def _is_processed(url: str) -> bool:
    return bool(url) and _get(_processed_key(url)) is not None


def _mark_processed(url: str) -> None:
    if url:
        _set(_processed_key(url), "1", _PROCESSED_TTL)


# ── Position store (single source of truth) ────────────────────────────────

def _load_positions() -> list[dict]:
    raw = _get(_POSITIONS_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _save_positions(positions: list[dict]) -> None:
    # Newest first, capped.
    positions = sorted(positions, key=lambda p: p.get("date", ""), reverse=True)
    _set(_POSITIONS_KEY, json.dumps(positions[:_MAX_POSITIONS]))


def _add_positions(new_records: list[dict]) -> None:
    if not new_records:
        return
    existing = _load_positions()
    by_id = {p["id"]: p for p in existing}
    for rec in new_records:
        by_id[rec["id"]] = rec
    _save_positions(list(by_id.values()))


def _make_record(article: dict, p: dict) -> dict:
    pid = hashlib.md5(
        f"{article.get('url', '')}|{p['position'][:80]}".encode()
    ).hexdigest()[:16]
    # Prefer the date he actually made the statement; fall back to the article's
    # publication date. Never accept a statement date after the article ran.
    art_date = article.get("published_iso") or _now_iso()
    sd = (p.get("statement_date") or "").strip()
    # Accept only a statement date within the campaign window and not after the
    # article ran; otherwise (e.g. a historical reference) use the article date.
    in_range = sd and TIMELINE_START_DATE <= sd <= art_date[:10]
    date = f"{sd}T12:00:00+00:00" if in_range else art_date
    return {
        "id":               pid,
        "position":         p["position"],
        "quote":            p.get("quote", ""),
        "policy_area":      p["policy_area"],
        "solidity":         p["solidity"],
        "attributed_to":    p.get("attributed_to", "Andy Burnham"),
        "is_direct_quote":  p.get("is_direct_quote", False),
        "context":          p.get("context", ""),
        "what_will_change": p.get("what_will_change", False),
        "source":           article.get("source", ""),
        "source_color":     article.get("source_color")
                            or SOURCE_COLORS.get(article.get("source", ""), DEFAULT_COLOR),
        "url":              article.get("url", ""),
        "date":             date,
        "captured_iso":     _now_iso(),
        "flip_flop":        None,
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_pipeline_running = {"on": False}


def _mentions_burnham(article: dict) -> bool:
    if article.get("is_own_words"):
        return True
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    return any(kw in text for kw in BURNHAM_NAME_KEYWORDS)


def _fetch_raw() -> list[dict]:
    """Fetch every source, then keep only items plausibly carrying his words."""
    raw: list[dict] = []
    for fetch in (fetch_all_news_feeds, fetch_twitter_posts, fetch_parliament_updates):
        try:
            raw.extend(fetch())
        except Exception as exc:
            log.warning("Source %s failed: %s", fetch.__name__, exc)
    # Dedup by id and pre-filter.
    seen, deduped = set(), []
    for a in raw:
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        if _mentions_burnham(a):
            deduped.append(a)
    log.info("Fetched %d items mentioning Burnham", len(deduped))
    # Pull article bodies so the AI sees his actual quotes, not just headlines.
    enrich_with_fulltext(deduped)
    return deduped


def _recompute_flip_flops() -> None:
    """Run the cross-position flip-flop pass over the whole store."""
    positions = _load_positions()
    if not positions:
        return
    by_area: dict[str, list[dict]] = {}
    for p in positions:
        by_area.setdefault(p["policy_area"], []).append(p)
    try:
        flags = detect_flip_flops(by_area)
    except Exception as exc:
        log.warning("Flip-flop pass failed: %s", exc)
        return
    for p in positions:
        p["flip_flop"] = flags.get(p["id"])
    _save_positions(positions)


def _run_pipeline_streaming(emit=None) -> int:
    """
    Run the full pipeline. If emit is given (callable taking a dict) it is
    called with progress events for SSE. Returns the count of new positions.
    """
    def _emit(ev):
        if emit:
            emit(ev)

    raw = _fetch_raw()
    _emit({"type": "status", "message": f"Screening {len(raw)} items…"})

    # 1. Filter (gatekeeper) — collect relevant articles.
    relevant: list[dict] = []
    for batch in filter_articles_streaming(raw, get_score, set_score):
        relevant.extend(batch)
    _emit({"type": "status",
           "message": f"{len(relevant)} carry his words — extracting positions…"})

    # 2. Extract positions for articles not already processed.
    new_articles = [a for a in relevant if not _is_processed(a.get("url", ""))]
    new_count = 0
    for article, positions in extract_positions_streaming(new_articles):
        _mark_processed(article.get("url", ""))
        if not positions:
            continue
        records = [_make_record(article, p) for p in positions]
        _add_positions(records)
        new_count += len(records)
        _emit({"type": "positions", "items": records})

    # 3. Flip-flop pass across the full store.
    if new_count:
        _emit({"type": "status", "message": "Checking for position shifts…"})
        _recompute_flip_flops()

    _set(_META_KEY, json.dumps({"last_run": _now_iso(),
                                "total": len(_load_positions())}))
    _emit({"type": "done", "new": new_count})
    log.info("Pipeline complete — %d new positions", new_count)
    return new_count


def _run_pipeline() -> None:
    """Background-thread entry point with the running-guard."""
    if _pipeline_running["on"]:
        log.info("Pipeline already running — skipping")
        return
    _pipeline_running["on"] = True
    try:
        _run_pipeline_streaming()
    except Exception as exc:
        log.error("Pipeline error: %s", exc, exc_info=True)
    finally:
        _pipeline_running["on"] = False


# ---------------------------------------------------------------------------
# Aggregation for the dashboard
# ---------------------------------------------------------------------------

def _build_dashboard_data() -> dict:
    positions = _load_positions()

    # Group by area (newest first within each area).
    by_area: dict[str, list[dict]] = {a: [] for a in POLICY_AREAS}
    for p in positions:
        by_area.setdefault(p["policy_area"], []).append(p)
    for a in by_area:
        by_area[a].sort(key=lambda x: x.get("date", ""), reverse=True)

    # Heatmap: counts per area per day, from the Makerfield result to today.
    start = datetime.fromisoformat(TIMELINE_START_DATE).date()
    today = datetime.now(tz=timezone.utc).date()
    ndays = max(1, (today - start).days + 1)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(ndays)]
    date_index = {d: i for i, d in enumerate(dates)}
    matrix = [[0] * len(dates) for _ in POLICY_AREAS]
    area_index = {a: i for i, a in enumerate(POLICY_AREAS)}
    for p in positions:
        d = (p.get("date") or "")[:10]
        ai_ = area_index.get(p["policy_area"])
        di = date_index.get(d)
        if ai_ is not None and di is not None:
            matrix[ai_][di] += 1

    meta_raw = _get(_META_KEY)
    meta = json.loads(meta_raw) if meta_raw else {}

    return {
        "policy_areas":    POLICY_AREAS,
        "solidity_levels": SOLIDITY_LEVELS,
        "by_area":         by_area,
        "heatmap":         {"dates": dates, "matrix": matrix},
        "meta": {
            "last_run":    meta.get("last_run"),
            "total":       len(positions),
            "window_days": ndays,
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("burnham.html")


@app.route("/api/burnham-data")
def api_burnham_data():
    try:
        return jsonify(_build_dashboard_data())
    except Exception as exc:
        log.error("Error building dashboard data: %s", exc, exc_info=True)
        return jsonify({"error": "Failed to build dashboard data."}), 500


# ---------------------------------------------------------------------------
# Background periodic refresh
# ---------------------------------------------------------------------------

_REFRESH_HOURS = float(os.environ.get("REFRESH_INTERVAL_HOURS", "12"))


def _seed_demo() -> None:
    """Populate the store with synthetic positions for local preview only.
    Enabled with SEED_DEMO=1; never runs in production. No-op if data exists."""
    if _load_positions():
        return
    today = datetime.now(tz=timezone.utc).date()

    def d(days_ago):
        return (today - timedelta(days=days_ago)).isoformat() + "T10:00:00+00:00"

    demo = [
        ("https://x.com/AndyBurnhamGM/1", "Andy Burnham (X)", d(1),
         "I will end the scandal of people selling their homes to pay for care — a National Care Service is the answer.",
         "Health & Social Care", "firm", True, True),
        ("https://news/health2", "Google News — Burnham", d(6),
         "Social care can't keep being the bit we leave until last.",
         "Health & Social Care", "topic", False, True),
        ("https://news/health3", "Google News — Burnham", d(3),
         "We should be moving towards free personal care, funded fairly.",
         "Health & Social Care", "emerging", False, True),
        ("https://x.com/AndyBurnhamGM/4", "Andy Burnham (X)", d(2),
         "Real devolution means handing fiscal powers to every English region, not just a few city-regions.",
         "Devolution & Local Government", "firm", True, True),
        ("https://news/devo5", "Google News — Burnham", d(9),
         "Westminster hoards too much power. That has to change.",
         "Devolution & Local Government", "emerging", False, False),
        ("https://news/transport6", "PoliticsHome", d(4),
         "Bus franchising worked in Greater Manchester and it should be the national model.",
         "Transport & Infrastructure", "firm", True, True),
        ("https://news/econ7", "LabourList", d(5),
         "Growth has to be felt in every postcode, not just the South East.",
         "Economy, Tax & Spending", "topic", False, False),
        ("https://news/housing8", "Google News — Burnham", d(7),
         "We need a council housebuilding programme at a scale we haven't seen for decades.",
         "Housing & Homelessness", "emerging", True, False),
        ("https://news/justice9", "Google News — Burnham", d(8),
         "A Hillsborough Law with a legal duty of candour will be a priority for my government.",
         "Crime, Policing & Justice", "firm", True, True),
        ("https://news/work10", "LabourList", d(11),
         "Workers' rights need strengthening, but we'll do it in partnership with business.",
         "Work, Pay & Welfare", "emerging", False, False),
        ("https://news/energy11", "Google News — Burnham", d(13),
         "Net zero is a jobs opportunity for the North.",
         "Energy & Net Zero", "topic", False, False),
    ]
    records = []
    for url, src, date_iso, quote, area, sol, direct, change in demo:
        records.append(_make_record(
            {"url": url, "source": src, "published_iso": date_iso,
             "source_color": SOURCE_COLORS.get(src, DEFAULT_COLOR)},
            {"quote": quote, "policy_area": area, "solidity": sol,
             "attributed_to": "Andy Burnham", "is_direct_quote": direct,
             "context": "", "what_will_change": change},
        ))
    # Mark the late "topic" social-care remark as a softening of the firm pledge.
    earlier = records[0]["id"]
    records[1]["flip_flop"] = {"contradicts": earlier,
                               "note": "Softer framing than the earlier firm pledge."}
    _add_positions(records)
    _set(_META_KEY, json.dumps({"last_run": _now_iso(), "total": len(records)}))
    log.info("Seeded %d demo positions", len(records))


def _background_loop() -> None:
    time.sleep(5)
    _run_pipeline()
    interval = _REFRESH_HOURS * 3600
    log.info("Background refresh every %.0f hours", _REFRESH_HOURS)
    while True:
        time.sleep(interval)
        _run_pipeline()


if os.environ.get("SEED_DEMO") == "1":
    _seed_demo()
elif os.environ.get("WERKZEUG_RUN_MAIN") != "true" \
        and os.environ.get("RUN_STARTUP_SCAN", "1") == "1":
    threading.Thread(target=_background_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Starting Burnham Tracker on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
