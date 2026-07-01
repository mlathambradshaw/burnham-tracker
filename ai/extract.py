"""
ai/extract.py — Claude extraction of individual policy positions from articles
that have passed the Burnham filter.

For each article we make one Haiku call that pulls out the distinct things
Burnham (or a named inner-circle member) actually *said* — his own words only —
classifies each into a policy area, rates how solid the position is, and flags
where it signals a break from the current government's direction.

A separate pass (detect_flip_flops) looks across the accumulated positions in
each area for statements that reverse or soften an earlier one.
"""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Generator

import anthropic

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    POLICY_AREAS, POLICY_AREA_DESCRIPTIONS,
    SOLIDITY_LEVELS, SOLIDITY_DESCRIPTIONS, INNER_CIRCLE,
)

log = logging.getLogger(__name__)

# Sonnet for extraction — much stronger at the "whose words are these?"
# attribution call than Haiku. The cheap filter step stays on Haiku.
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4000
MAX_WORKERS = 3


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _areas_block() -> str:
    return "\n".join(
        f"- {area}: {POLICY_AREA_DESCRIPTIONS.get(area, '')}" for area in POLICY_AREAS
    )


def _solidity_block() -> str:
    return "\n".join(f"- {lvl}: {SOLIDITY_DESCRIPTIONS[lvl]}" for lvl in SOLIDITY_LEVELS)


def _inner_circle_block() -> str:
    return "\n".join(f"- {name} ({role})" for name, role in INNER_CIRCLE.items())


_SYSTEM_PROMPT = """\
You are a political analyst tracking what Andy Burnham — the incoming UK Prime \
Minister (running effectively unopposed for the Labour leadership) — and his \
inner circle are saying, so clients can anticipate the direction of his \
government.

YOUR JOB
From the article, extract the distinct POLICY POSITIONS taken by Andy Burnham \
himself (or by a named inner-circle member). For each, write a short, neutral, \
SPECIFIC statement of what he would actually do — the policy, not the rhetoric.

WHAT COUNTS AS A POSITION
A position is a concrete policy stance or commitment — something specific he \
would do in government. Distil it into a plain statement, e.g.:
- "Open a 'No.10 North' government hub in Manchester."
- "Replace council tax and stamp duty with a tax on land value."
- "Put the entire £39bn affordable-homes budget into social rent."

A position is NOT a slogan, vision line or rhetorical flourish. Lines like \
"a rewired Britain", "end trickle-down economics", "turn the tide", "make \
politics work again" are NOT positions in themselves — they are themes. If he \
says one of these WITHOUT any specific policy attached, either capture the \
underlying theme as solidity "topic", or skip it. Do not dress up rhetoric as \
a firm commitment.

Keep the supporting words he actually said as "quote" (evidence), but the \
"position" is the distilled policy point — that is the important field.

STRICT RULES — WHOSE WORDS COUNT
Extract a statement ONLY when the words clearly originate from Burnham himself \
(or a named inner-circle member). That means one of:
  (a) a direct quotation from him, or
  (b) a clearly attributed report of something HE said — "Burnham said…", \
"he pledged…", "he told the BBC…", "Burnham's team confirmed he would…".

DO NOT extract — these are NOT his words, even if they mention him:
- A journalist's, columnist's or analyst's description, speculation or summary \
of his position ("Burnham's plan would…", "he is expected to…", "his proposal \
is popular with voters").
- Anyone else talking about him or to him — rivals, ministers, union leaders, \
commentators, "critics say", unnamed sources ("Reeves urged Burnham to…", \
"unions warn Burnham…").
- An inner-circle member describing or praising Burnham's position — that is \
the adviser's characterisation, not Burnham's own statement.
- A quotation attributed to someone OTHER than Burnham, even when they are \
vouching for him or describing his views in the third person — e.g. an aide \
saying "Andy has been really explicit, he backs the fiscal rules" or "Andy has \
always believed in X". Those are the speaker's words about him, NOT his own. \
Do not credit them to Burnham.
- Vague fragments with no real content ("something he is committed to", "the \
plans", "his approach"). Only extract a position that states actual substance.
A useful test: could you put the words in Burnham's own mouth, in the first \
person, as something he said? "He has pledged to scrap X" → yes (he said it). \
"Andy backs the fiscal rules" said by an aide → no (the aide said it).
If you cannot tell whether the words originate from Burnham himself, DO NOT \
extract. When in doubt, leave it out.

- Inner circle — extract their OWN statements, attributed to the person, never \
to Burnham:
{inner_circle}
- If the article is ABOUT Burnham but contains nothing he actually said, return \
an empty positions array. A thin article yielding zero positions is the correct \
outcome — do not pad.
- One position per distinct idea. Do not merge two different policies; do not \
split one policy into many near-identical fragments. Prefer a clean direct \
quote over a fragment.

FOR EACH POSITION, CLASSIFY:

policy_area — exactly one, from this list (use the name verbatim):
{areas}

solidity — how concrete the position is:
{solidity}

what_will_change — true if this signals a BREAK from the *current* government's \
policy or direction (i.e. something he would do differently from the outgoing \
Starmer government), otherwise false.

statement_date — when he actually MADE this statement, as YYYY-MM-DD. Use the \
article's publication date (given in the user message) as your reference point \
to resolve relative phrases ("yesterday", "last week", "in his victory speech \
on 19 June"). Only return a date different from the article's publication date \
when the text clearly indicates when he said it; otherwise return the article's \
publication date exactly.

is_direct_quote — true ONLY if "quote" is his verbatim words (as they appear in \
quotation marks in the text); false for an attributed paraphrase.

OUTPUT
Respond with ONLY valid JSON, no markdown, no commentary:
{{
  "positions": [
    {{
      "position": "short neutral statement of the specific policy he would enact",
      "quote": "the supporting words he actually said (evidence), or ''",
      "policy_area": "one area name from the list",
      "solidity": "topic | emerging | firm",
      "attributed_to": "Andy Burnham" or the inner-circle member's name,
      "context": "one sentence of neutral context",
      "what_will_change": true or false,
      "statement_date": "YYYY-MM-DD"
    }}
  ]
}}

Return {{"positions": []}} if there is nothing worth recording. A slogan with \
no policy behind it is not worth recording.
"""


def _build_system_prompt() -> str:
    return _SYSTEM_PROMPT.format(
        inner_circle=_inner_circle_block(),
        areas=_areas_block(),
        solidity=_solidity_block(),
    )


def _user_prompt(article: dict) -> str:
    own = " (this is a post from Burnham's own X account — his own words)" \
        if article.get("is_own_words") else ""
    return (
        f"Source: {article.get('source', 'Unknown')}{own}\n"
        f"Article publication date: {article.get('published_iso', '')[:10]} "
        "(use this to resolve statement_date)\n"
        f"Title: {article.get('title', '')}\n"
        f"Text: {(article.get('summary') or '')[:6000]}\n\n"
        "Extract the policy positions as specified."
    )


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # salvage the first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", match.group()))
        except json.JSONDecodeError:
            pass
    return {"positions": []}


_VALID_AREAS = set(POLICY_AREAS)
_VALID_SOLIDITY = set(SOLIDITY_LEVELS)


def _clean_positions(raw: dict) -> list[dict]:
    out = []
    for p in raw.get("positions", []):
        if not isinstance(p, dict):
            continue
        position = (p.get("position") or "").strip()
        area = (p.get("policy_area") or "").strip()
        if not position or area not in _VALID_AREAS:
            continue
        solidity = (p.get("solidity") or "").strip().lower()
        if solidity not in _VALID_SOLIDITY:
            solidity = "topic"
        sd = (p.get("statement_date") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", sd):
            sd = ""
        out.append({
            "position":         position,
            "quote":            (p.get("quote") or "").strip(),
            "policy_area":      area,
            "solidity":         solidity,
            "attributed_to":    (p.get("attributed_to") or "Andy Burnham").strip(),
            "context":          (p.get("context") or "").strip(),
            "what_will_change": bool(p.get("what_will_change", False)),
            "statement_date":   sd,
        })
    return out


# ---------------------------------------------------------------------------
# Per-article extraction
# ---------------------------------------------------------------------------

def extract_positions(article: dict, client=None, system_prompt: str = None) -> list[dict]:
    """Extract positions from a single article (one Claude call)."""
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    if system_prompt is None:
        system_prompt = _build_system_prompt()
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": _user_prompt(article)}],
        )
        return _clean_positions(_parse_json(msg.content[0].text))
    except (anthropic.APIError, IndexError) as exc:
        log.error("Extraction failed for %s: %s", article.get("url", "?"), exc)
        return []


# Backwards-compatible internal alias used by the streaming path.
_extract_one = extract_positions


def extract_positions_streaming(
    articles: list[dict],
) -> Generator[tuple[dict, list[dict]], None, None]:
    """
    Extract positions from each article concurrently, yielding (article,
    positions) as each Claude call completes. positions may be an empty list.
    """
    if not articles:
        return

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    system_prompt = _build_system_prompt()

    log.info("Extracting positions from %d articles (%d concurrent)…",
             len(articles), MAX_WORKERS)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_extract_one, a, client, system_prompt): a
            for a in articles
        }
        for future in as_completed(futures):
            article = futures[future]
            try:
                positions = future.result()
            except Exception as exc:
                log.error("Extraction worker error: %s", exc)
                positions = []
            yield article, positions


# ---------------------------------------------------------------------------
# Flip-flop detection (cross-position, per area)
# ---------------------------------------------------------------------------

_FLIP_SYSTEM = """\
You compare statements made by Andy Burnham over time within a single policy \
area and identify FLIP-FLOPS: where a later statement clearly reverses, \
contradicts or materially softens an earlier one by the same person.

Be conservative. A position simply becoming more detailed or firmer over time \
is NOT a flip-flop. Only flag genuine reversals or contradictions.

You are given a numbered list of statements with dates. Respond with ONLY JSON:
{"flips": [{"id": <number of the later statement>, "contradicts": <number of \
the earlier statement>, "note": "one sentence on the reversal"}]}
Return {"flips": []} if there are none.
"""


def detect_flip_flops(positions_by_area: dict[str, list[dict]]) -> dict[str, dict]:
    """
    Given {area: [position, ...]} (each position must carry 'id', 'date',
    'quote', 'attributed_to'), return {position_id: {"contradicts": id,
    "note": str}} for positions judged to reverse an earlier one.

    One Claude call per area that has >= 2 Burnham statements. Best-effort:
    any failure for an area simply yields no flags for it.
    """
    flags: dict[str, dict] = {}
    areas = {
        area: [p for p in items if (p.get("attributed_to") == "Andy Burnham")]
        for area, items in positions_by_area.items()
    }
    areas = {a: items for a, items in areas.items() if len(items) >= 2}
    if not areas:
        return flags

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def _one(area: str, items: list[dict]) -> None:
        ordered = sorted(items, key=lambda p: p.get("date", ""))
        listing = "\n".join(
            f"{i}. [{p.get('date', '')[:10]}] {p.get('position') or p.get('quote', '')[:200]}"
            for i, p in enumerate(ordered)
        )
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=1500,
                system=_FLIP_SYSTEM,
                messages=[{"role": "user",
                           "content": f"Policy area: {area}\n\n{listing}"}],
            )
            data = _parse_json(msg.content[0].text)
            for flip in data.get("flips", []):
                try:
                    later = ordered[int(flip["id"])]
                    earlier = ordered[int(flip["contradicts"])]
                except (KeyError, ValueError, IndexError, TypeError):
                    continue
                flags[later["id"]] = {
                    "contradicts": earlier["id"],
                    "note": (flip.get("note") or "").strip(),
                }
        except Exception as exc:
            log.warning("Flip-flop check failed for area %r: %s", area, exc)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(lambda kv: _one(*kv), areas.items()))

    log.info("Flip-flop detection flagged %d positions", len(flags))
    return flags
