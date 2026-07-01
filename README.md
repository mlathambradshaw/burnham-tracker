# Burnham Tracker

A read-only web dashboard tracking the policy positions of **Andy Burnham** —
the incoming Prime Minister — during the leadership handover and beyond. It
captures *his own words* (and his inner circle's), classifies each statement by
**policy area** and **solidity**, and shows what he's focusing on, what he's
actually committed to, and where he signals change.

See **[SPEC.md](SPEC.md)** for the full design rationale.

## What it shows
- **Firm commitments** — his clearest pledges and proposals, pulled out front.
- **Attention heatmap** — how much he's talking about each of 12 policy areas,
  by day, with a time slider to watch focus shift.
- **Newest positions** — a live feed of captured statements, filterable.
- **Per-area timelines** — click any area to see each statement plotted by date
  and solidity (topic → emerging → firm), with the exact quote and source.
- **Flags** — ⚡ *Departure* (a break from the current government line) and
  ⟲ *Shift* (a position that softens or reverses).

## How it works
`fetch` (Burnham's X via RSSHub, a Google-News "Andy Burnham" search, LabourList,
PoliticsHome, Parliament) → keyword pre-filter → **Claude filter** (does this
carry his words on policy?) → **Claude extract** (individual positions) → store
in Redis. Both Claude steps use `claude-haiku-4-5`. A background thread refreshes
twice a day; the **Refresh** button runs it on demand and streams progress.

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env        # fill in ANTHROPIC_API_KEY (REDIS_URL optional locally)
python app.py               # http://localhost:8080
```
Without `REDIS_URL` it uses an in-memory store (fine for local dev; data is lost
on restart). To preview the UI with synthetic data and no API calls:
```bash
SEED_DEMO=1 ANTHROPIC_API_KEY=x SECRET_KEY=x python app.py
```

## Deploy (Railway)
Same setup as the sibling Bradshaw dashboard:
1. Push to GitHub, create a Railway service from the repo.
2. Set variables: `ANTHROPIC_API_KEY`, `REDIS_URL` (Upstash), `SECRET_KEY`.
3. Deploy. `Procfile` runs gunicorn with **1 worker / 4 threads** (single worker
   so the background pipeline and in-process guard aren't duplicated).

Optional: `REFRESH_INTERVAL_HOURS` (default 12).

## Environment variables
| Var | Required | Purpose |
|-----|----------|---------|
| `ANTHROPIC_API_KEY` | yes | Claude filter + extraction |
| `REDIS_URL` | prod | Position store + caches (Upstash `rediss://…`) |
| `SECRET_KEY` | yes | Flask session key |
| `REFRESH_INTERVAL_HOURS` | no | Background refresh cadence (default 12) |
| `SEED_DEMO` | no | `1` = load demo data, skip live pipeline (local preview only) |

## Notes
- **Twitter/X** is read via public RSSHub instances (tried in order, graceful
  fallback). If they're all down, the **+ Add statement** button lets you paste a
  tweet/quote/speech excerpt and run it through extraction manually.
- No database, no SMTP, no contacts — this is a read-only intelligence product.
