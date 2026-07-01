# Deploying the Burnham Tracker

The app is a standard Flask service (gunicorn, `Procfile`, `runtime.txt`). It runs
anywhere that hosts Python web apps. These steps use **Railway** for hosting and
**Upstash** for Redis, the same stack as the Bradshaw Advisory dashboard.

## What you need
- A **GitHub** repo with this code.
- An **Upstash Redis** database (free tier is plenty — data is ~100KB).
- A **Railway** project connected to the repo.
- Three environment variables: `ANTHROPIC_API_KEY`, `REDIS_URL`, `SECRET_KEY`.

## 1. Put the code on GitHub
From `burnham_tracker/`:
```bash
git init
git add .
git commit -m "Burnham Tracker"
git branch -M main
git remote add origin git@github.com:<org>/burnham-tracker.git
git push -u origin main
```
`.env` and `.local_store.json` are git-ignored, so no secrets or local data are pushed.

## 2. Create the Redis database (Upstash)
1. Sign in at upstash.com, create a Redis database (any region near the app).
2. Copy its connection string — it looks like `rediss://default:<password>@<host>:<port>`.
   That value is your `REDIS_URL`. (The `rediss://` scheme means TLS; the app handles it.)

## 3. Deploy on Railway
1. New Project → Deploy from GitHub repo → pick the repo.
2. Railway detects the `Procfile` and builds automatically.
3. In the service's **Variables**, add:
   - `ANTHROPIC_API_KEY` — your Anthropic key
   - `REDIS_URL` — from Upstash
   - `SECRET_KEY` — any long random string
   - *(optional)* `REFRESH_INTERVAL_HOURS` — how often it refreshes (default 12)
4. Deploy. On boot it runs one scan, then refreshes on that interval. You can also
   hit **Refresh** in the UI at any time.

## 4. Point a domain at it
1. Railway gives a `*.up.railway.app` URL to check it works.
2. For a branded address (e.g. `burnham.bradshawadvisory.com`): Railway → Settings →
   Networking → Custom Domain, then add the CNAME it shows to your DNS.

## Notes
- **One worker on purpose.** The `Procfile` runs a single gunicorn worker with
  threads. The background refresh and the de-duplication guard rely on shared
  in-process state, so do not scale to multiple workers.
- **Cost.** Hosting is a few dollars a month; Upstash free tier covers the data;
  the only usage cost is Anthropic (Haiku to filter, Sonnet to extract) on each
  refresh — small, since only a handful of articles reach extraction per run.
- **No Redis?** The app falls back to an in-memory store (data resets on restart).
  Fine for local use, not for production — always set `REDIS_URL` when deployed.
- **Access.** The site is public by default. If you want it gated before launch,
  see "Access control" below.

## Access control (optional)
If the tracker should not be openly public yet, the simplest option is HTTP basic
auth in front of every route. Ask and this can be added with a single env var
(`SITE_PASSWORD`) so the whole site sits behind one shared password.
