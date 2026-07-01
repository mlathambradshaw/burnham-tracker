# Burnham Tracker — Build Spec

A read-only web dashboard tracking what **Andy Burnham and his inner circle are
saying** as he moves toward becoming Prime Minister (Labour leadership
coronation, PM by ~17 July 2026). External-facing intelligence product for
clients, media and researchers. **Not** an outreach tool — no contacts, no email.

## The product in one line
Capture Burnham's *own words*, classify each into a **policy area** and a
**solidity** level, and show how his positions are forming over the campaign
window — what he's focusing on, what he's actually committed to, and where he
signals change.

## Core concepts
- **Policy area** — a major area of national government (PM scope, light Burnham
  specificity). An area has *volume* (how much he talks about it). Areas do NOT
  have solidity.
- **Position** — a specific stance within an area (e.g. "free personal care for
  over-65s"). A *position* has **solidity** and can firm up over time:
  `topic` (just raised) → `emerging` (a developing position) → `firm`
  (a clear proposal/commitment).
- **Attribution** — primary stream is Burnham's own words. A named inner circle
  is captured secondarily, always badged as the adviser, never as Burnham.

## Policy areas (12)
Economy/Tax/Spending · Health & Social Care · Housing & Homelessness ·
Devolution & Local Government · Transport & Infrastructure · Energy & Net Zero ·
Work/Pay/Welfare · Business/Industrial Strategy/Trade · Crime/Policing/Justice ·
Immigration & Asylum · Education & Skills · Foreign Affairs & Defence.

## Sources (his words only)
- `@AndyBurnhamGM` on X (via RSSHub bridge, graceful fallback).
- Google News search for "Andy Burnham" — used to lift his *direct quotes* from
  coverage; journalist framing discarded at extraction.
- LabourList / PoliticsHome (keyword pre-filtered for Burnham).
- Parliament: written questions/statements mentioning him now he's an MP.
- Inner circle (badged, secondary): James Purnell, Kevin Lee, Kate Green,
  Caroline Simpson, Grace Pritchard.

## Pipeline
fetch → keyword pre-filter (mentions Burnham / from his X) → Claude **filter**
(is this Burnham/inner-circle saying something substantive? his words only) →
Claude **extract** (individual positions: quote, area, solidity, attribution,
is_direct_quote, what_will_change, context) → store + mark processed.

Models: `claude-haiku-4-5` for both filter and extract. Concurrency via
ThreadPoolExecutor. Processed-URL cache makes extraction idempotent (positions
counted once).

## Storage (Redis only, in-memory fallback)
- `burnham:processed:{urlhash}` = 1, TTL 30d — article handled.
- `burnham:positions` = capped JSON list (most recent ~1000) — **single source
  of truth**. Heatmap, timelines, firm-commitments, newest feed and solidity
  breakdown are all derived from this on read.
- `burnham:meta` = {last_run_iso, ...}.

Each position record:
`{id, quote, policy_area, solidity, attributed_to, is_direct_quote, context,
  source, url, date, captured_iso, what_will_change, flip_flop}`

## Dashboard (templates/burnham.html, Chart.js + custom)
1. **Firm commitments (flagship)** — the most solid positions pulled out and
   showcased separately, each with quote + provenance link.
2. **Attention heatmap** — policy areas × time, intensity = volume, with a time
   slider/scrubber to watch focus shift. Never encodes solidity.
3. **Newest positions feed** — latest captured statements, alongside the heatmap.
4. **Per-area breakout** — timeline: x = time, y = solidity, dots = statements
   threaded into evolving positions; hover/click = exact quote + date + source.
5. **Flags woven through:** "what will change" (break from current govt line) and
   "flip-flop" (a position softens/reverses). Provenance link on every position.

## Kept from bradshaw_dashboard
SSE streaming, background twice-daily refresh, pipeline guard, single Gunicorn
worker, plain `redis.from_url`, Railway deploy. Env: ANTHROPIC_API_KEY,
REDIS_URL, SECRET_KEY.
