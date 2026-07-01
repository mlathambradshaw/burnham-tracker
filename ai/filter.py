"""
ai/filter.py — Claude gatekeeper for the Burnham Tracker.

Cheap first pass: decide whether an article actually carries Andy Burnham's
(or a named inner-circle member's) own words on policy, worth sending to the
extraction step. Keeps the streaming + per-URL cache + concurrent-batch
architecture; only the prompt and output fields differ.
"""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Generator, Optional

import anthropic

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import INNER_CIRCLE

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4000
MIN_RELEVANCE_SCORE = 5
BATCH_SIZE = 40

_INNER_CIRCLE_NAMES = ", ".join(INNER_CIRCLE.keys())

_SYSTEM_PROMPT = f"""\
You are screening articles for an intelligence product that tracks the POLICY
POSITIONS of Andy Burnham — the incoming UK Prime Minister — and his inner
circle ({_INNER_CIRCLE_NAMES}).

This is a broad first-pass gate, NOT the final precision step — a later stage
extracts the exact positions. So keep anything that is a plausibly useful
SOURCE of his policy positions, and only bin what clearly is not. When unsure,
lean towards keeping (score 5).

An article is a useful source if it quotes him OR reports things he has said,
pledged, proposed, announced or set out — whether in his own words or
attributed/reported by the journalist ("Burnham has pledged…", "he told the
BBC…", "in his speech he set out…", "his plan is to…").

Score each item 0-10:
- 8-10: substantially sets out his (or an inner-circle member's) policy
  positions, plans or pledges — quoted or reported.
- 5-7: touches on at least one of his policy positions or statements, even
  briefly or as part of a wider piece.
- 1-4: about Burnham but carries NONE of his own positions — pure process or
  horse-race (who backs whom, polls, cabinet speculation), biography, or only
  OTHER people's views about him.
- 0: not really about Burnham.

Set "carries_his_words" true if it reports at least one position attributable
to Burnham himself (or a named inner-circle member).

Respond with ONLY a valid JSON array, no markdown. One object per item:
  {{"id": "<copy exactly>", "relevance_score": <0-10>,
    "carries_his_words": <true|false>}}
Include an entry for EVERY item provided.
"""

_USER_TEMPLATE = (
    "Screen these {count} items.\n\n{items}\n\n"
    "Return a JSON array with one object per item."
)


def _compact(article: dict) -> str:
    # Enough body for the screen to spot his quotes buried deeper in a piece.
    summary = (article.get("summary") or "")[:1500]
    own = " [from Burnham's own X account]" if article.get("is_own_words") else ""
    return (
        f"ID: {article['id']}\n"
        f"Source: {article.get('source', 'Unknown')}{own}\n"
        f"Title: {article.get('title', '')}\n"
        f"Summary: {summary}"
    )


def _parse_response(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", text))
    except json.JSONDecodeError:
        pass
    objects = []
    for m in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
        try:
            obj = json.loads(m.group())
            if "id" in obj:
                objects.append(obj)
        except json.JSONDecodeError:
            continue
    if objects:
        log.warning("Used fallback JSON parsing (%d items)", len(objects))
        return objects
    raise json.JSONDecodeError("Could not parse Claude response", text, 0)


def _score_batch(batch: list[dict], client, system_prompt: str) -> dict:
    items = [f"[{i}]\n{_compact(a)}" for i, a in enumerate(batch, 1)]
    user_prompt = _USER_TEMPLATE.format(count=len(batch), items="\n\n".join(items))
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        scored = _parse_response(msg.content[0].text)
        return {it["id"]: it for it in scored if it.get("id")}
    except (json.JSONDecodeError, IndexError) as exc:
        log.error("Failed to parse filter response: %s", exc)
        return {}
    except anthropic.APIError as exc:
        log.error("Claude API error in filter batch: %s", exc)
        return {}


def _enrich_batch(batch: list[dict], scored_map: dict,
                  set_score: Optional[Callable]) -> list[dict]:
    enriched = []
    for article in batch:
        scored = scored_map.get(article.get("id"))
        if not scored:
            continue
        try:
            score = int(scored.get("relevance_score", 0))
        except (TypeError, ValueError):
            score = 0
        carries = bool(scored.get("carries_his_words", False))
        # Permissive gate: score alone decides. Sonnet extraction is the
        # precision step that drops anything not genuinely his.
        keep = score >= MIN_RELEVANCE_SCORE
        record = {**article, "relevance_score": score, "carries_his_words": carries}
        if set_score and article.get("url"):
            # Cache the verdict either way so re-runs skip re-screening.
            set_score(article["url"], record if keep else
                      {**record, "_filtered_out": True})
        if keep:
            enriched.append(record)
    return enriched


def filter_articles_streaming(
    articles: list[dict],
    get_score: Optional[Callable] = None,
    set_score: Optional[Callable] = None,
) -> Generator[list[dict], None, None]:
    """
    Yield lists of articles that carry Burnham's (or inner-circle) own policy
    words, as each Claude batch completes. Previously screened URLs are served
    from cache and never re-sent to Claude.
    """
    if not articles:
        return

    cached_relevant: list[dict] = []
    uncached: list[dict] = []
    for article in articles:
        url = article.get("url", "")
        if get_score and url:
            cached = get_score(url)
            if cached is not None:
                if not cached.get("_filtered_out") and \
                        cached.get("relevance_score", 0) >= MIN_RELEVANCE_SCORE:
                    cached_relevant.append(cached)
                continue
        uncached.append(article)

    log.info("Filter: %d cached, %d new to screen",
             len(articles) - len(uncached), len(uncached))

    if cached_relevant:
        yield cached_relevant
    if not uncached:
        return

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    batches = [uncached[i:i + BATCH_SIZE] for i in range(0, len(uncached), BATCH_SIZE)]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_score_batch, b, client, _SYSTEM_PROMPT): b
                   for b in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                enriched = _enrich_batch(batch, future.result(), set_score)
                log.info("  Filter batch done (%d kept)", len(enriched))
                if enriched:
                    yield enriched
            except Exception as exc:
                log.error("Filter batch failed: %s", exc)


def filter_articles(
    articles: list[dict],
    get_score: Optional[Callable] = None,
    set_score: Optional[Callable] = None,
) -> list[dict]:
    """Collect all streaming batches into a single list (non-streaming use)."""
    out: list[dict] = []
    for batch in filter_articles_streaming(articles, get_score, set_score):
        out.extend(batch)
    log.info("filter_articles: %d → %d kept", len(articles), len(out))
    return out
