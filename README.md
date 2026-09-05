# stayup-cmd-scrap

[![CI](https://github.com/stayup-app/stayup-cmd-scrap/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/stayup-app/stayup-cmd-scrap/actions/workflows/ci.yml)
[![Daily scrape](https://github.com/stayup-app/stayup-cmd-scrap/actions/workflows/daily.yml/badge.svg)](https://github.com/stayup-app/stayup-cmd-scrap/actions/workflows/daily.yml)

**Website:** https://stayup-ui.vercel.app

Scrapes blog articles from pages curated via `admin.py` (see below) and stores
results via [stayup-api](https://github.com/stayup-app/stayup-api) — neither
this script nor `admin.py` touches a database directly, they only call
`stayup-api`'s HTTP endpoints.

On each run the script fetches every tracked source, retrieves the article
links on its listing page, and scrapes each article until one is already
known or the per-run limit (`max_scraps`) is reached.

## How it works

1. Fetch the blog listing page and extract article URLs using `articles_selector`.
2. For each URL (newest first), stop at the first one already known (dedup by URL — the
   article's URL is used as the dedup `version`, same mechanism as every other connector).
3. Scrape the article content using `content_selector` and store it.
4. Stop after `max_scraps` articles per run (default: 5).

Each source's config:

| Config key           | Required | Description                                                  |
|-----------------------|----------|--------------------------------------------------------------|
| `articles_selector`   | yes      | CSS selector for article `<a>` links on the listing page     |
| `content_selector`    | no       | CSS selector for article body (default: `"body"`)            |
| `exclude`             | no       | CSS selectors removed from the body before extracting text   |
| `max_scraps`          | no       | Max articles scraped per run (default: `5`)                  |
| `retention_days`      | no       | How long a scraped article is kept (default: `15`)           |

Each scraped article is stored with:
- `content` — extracted text
- `params` — snapshot of the config + the article `url` (`$row.params.url` in the display template)

## Requirements

- Python 3.13, or [Docker](https://www.docker.com/)
- A `stayup-api` instance (the public one, or your own — see [self-hosting-and-providers.md](https://github.com/stayup-app/stayup-api/blob/main/docs/self-hosting-and-providers.md))
- An API key for the `scrap` provider, created from that instance's admin panel (Connector keys → New key, provider `scrap`). The key is shown once — copy it right away. It is used by both the scraper and `admin.py` (see below) — no separate admin account needed.

## Setup

```bash
git clone https://github.com/stayup-app/stayup-cmd-scrap.git
cd stayup-cmd-scrap
cp .env.example .env
```

Fill in `STAYUP_API_URL` / `STAYUP_API_KEY` (used by both the scraper and the
web admin — see below), and the `SCRAP_ADMIN_*` trio if you're running the
web admin.

```bash
docker compose run --rm scrape_pages
```

Without Docker:
```bash
pip install -r requirements.txt
STAYUP_API_URL=... STAYUP_API_KEY=... python scrape_pages.py
```

> **Note:** the provider registers itself automatically on every run — nothing
> to create by hand beyond the key. Sources themselves are curated via
> `admin.py`, not this script.

## Web admin

A small Flask page to add, edit and delete scrap fluxes without touching the
API by hand. It calls `stayup-api`'s general admin endpoints
(`/ui/repositories`) — the same ones the stayup-ui admin panel uses —
authenticated with the same `STAYUP_API_KEY` as the scraper: a connector key
scoped to `scrap` can manage repositories of that type there too, so no
separate admin account is needed.

> Users can also request a scrap flux straight from the StayUp apps (just the
> page URL). The provider's display template ships a `form` block for that, but
> `scrap` is `flux_approval = manual`, so the request lands in the API's
> approval queue — an admin approves it and fills in the CSS selectors here
> before the scraper picks it up.

```bash
# In .env:
SCRAP_ADMIN_EMAIL=you@example.com
SCRAP_ADMIN_PASSWORD=a-strong-password
SCRAP_ADMIN_SECRET=a-long-random-string        # signs the session cookie

docker compose up admin -d          # http://localhost:8000
# or, without Docker:
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:8000 admin:app
```

Auth to this page is a single operator account from the environment — there is
no sign-up. It is a separate credential from `STAYUP_API_KEY`: the former
gates who may open this page, the latter is what the page uses to talk to
`stayup-api` on the operator's behalf.

Deleting a flux is refused while a user is still subscribed to it (checked via
`stayup-api`'s subscriber count for that source).

## GitHub Actions

### CI (`ci.yml`)

Runs on every push and pull request to `main`: lint with **ruff** and
**black**, then the test suite — `stayup-api` and network calls are mocked,
so no database or live service is needed.

### Daily cron (`daily.yml`)

Runs every day at 00:00 UTC (also triggerable manually from GitHub Actions).

Required secrets — `STAYUP_API_URL` and `STAYUP_API_KEY` — configured in
**Settings → Secrets and variables → Actions → New repository secret**.

## Development

```bash
# Lint + tests via Docker (recommended)
docker compose run --rm --entrypoint sh scrape_pages -c "ruff check . && black --check ."
docker compose run --rm test

# Pre-commit hook
cp scripts/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```
