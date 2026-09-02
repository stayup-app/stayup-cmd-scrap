# stayup-cmd-scrap

[![CI](https://github.com/stayup-app/stayup-cmd-scrap/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/stayup-app/stayup-cmd-scrap/actions/workflows/ci.yml)
[![Daily scrape](https://github.com/stayup-app/stayup-cmd-scrap/actions/workflows/daily.yml/badge.svg)](https://github.com/stayup-app/stayup-cmd-scrap/actions/workflows/daily.yml)

**Website:** https://stayup-ui.vercel.app

Scrapes blog articles from pages defined in the `profile` table and stores results in PostgreSQL.

On each run the script fetches all profiles, retrieves the article links on the listing page,
and scrapes each article until one is already in the database or the per-run limit is reached.

## How it works

1. Fetch the blog listing page and extract article URLs using `articles_selector`.
2. For each URL (newest first), stop if the article is already in `connector_scrap` (dedup by URL).
3. Scrape the article content using `content_selector` and save it to `connector_scrap`.
4. Stop after `max_scraps` articles (default: 50) to avoid runaway scraping.

Profiles are stored directly in the `profile` table as JSON configs:

```sql
INSERT INTO profile (config)
VALUES ('{
  "page": "https://blog.example.com",
  "articles_selector": "h2.post-title a",
  "content_selector": "article.post-content",
  "max_scraps": 20
}');
```

| Config key           | Required | Description                                                  |
|----------------------|----------|--------------------------------------------------------------|
| `page`               | yes      | URL of the blog listing page                                 |
| `articles_selector`  | yes      | CSS selector for article `<a>` links on the listing page     |
| `content_selector`   | no       | CSS selector for article body (default: `"body"`)            |
| `max_scraps`         | no       | Max articles scraped per run (default: `50`)                 |

Each scraped article is stored as a row in `connector_scrap` with:
- `content` — extracted text
- `params` — snapshot of the config + the article `url`

Errors are logged to the `log` table.

## Database schema

```
profile
  id          SERIAL PK
  config      JSONB UNIQUE   -- {"page": "...", "articles_selector": "...", ...}
  created_at  TIMESTAMPTZ

connector_scrap
  id          SERIAL PK
  provider_id → profile.id
  content     TEXT           -- scraped article text
  params      JSONB          -- {"url": "<article url>", ...config}
  executed_at TIMESTAMPTZ
  success     BOOLEAN

log
  id          SERIAL PK
  profile_id  → profile.id
  error       TEXT
  executed_at TIMESTAMPTZ
```

## Setup

### With Docker (recommended)

```bash
cp .env.example .env
# Fill in DB_NAME, DB_USER, DB_PASSWORD

docker compose up db -d

# Add a blog to scrape (see "Web admin" below for a UI instead of raw SQL)
docker compose exec db psql -U <DB_USER> -d <DB_NAME> -c \
  "INSERT INTO repository (url, type, config) VALUES ('https://blog.example.com', 'scrap', '{\"articles_selector\": \"h2 a\", \"content_selector\": \"article\"}');"

# Run the scraper
docker compose run --rm scrape_pages
```

### Without Docker

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:password@host:5432/dbname
python scrape_pages.py
```

## Web admin

A small Flask page to add, edit and delete scrap fluxes without touching SQL.
It reads and writes the same `repository` rows (`type = 'scrap'`) the scraper
consumes, so run it against the same database.

> Users can also request a scrap flux straight from the StayUp apps (just the
> page URL). The provider's display template ships a `form` block for that, but
> `scrap` is `flux_approval = manual`, so the request lands in the API's
> approval queue — an admin approves it and fills in the CSS selectors before
> the scraper picks it up.

```bash
# In .env, alongside the DB_* / DATABASE_URL settings:
SCRAP_ADMIN_EMAIL=you@example.com
SCRAP_ADMIN_PASSWORD=a-strong-password
SCRAP_ADMIN_SECRET=a-long-random-string   # signs the session cookie

docker compose up admin -d          # http://localhost:8000
# or, without Docker:
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:8000 admin:app
```

Auth is a single operator account from the environment — there is no sign-up.
Deleting a flux also removes its scraped rows and logs; it is refused while a
user is still subscribed to it (checked against the API's `user_repository`
table when that table lives in the same database).

## GitHub Actions

### CI (`ci.yml`)

Runs on every push and pull request to `main`:
- Lint with **ruff** and **black**
- Run unit + functional tests (scraper and web admin) against a temporary PostgreSQL service

### Daily cron (`daily.yml`)

Runs every day at 08:00 UTC (also triggerable manually from GitHub Actions).

Required secret:
- `DATABASE_URL` — connection string to your production database

To configure: **Settings → Secrets and variables → Actions → New repository secret**

## Development

```bash
# Lint + tests via Docker (recommended)
docker compose run --rm --entrypoint sh scrape_pages -c "ruff check . && black --check ."
docker compose run --rm test

# Pre-commit hook
cp scripts/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```
