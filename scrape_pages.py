#!/usr/bin/env python3
"""
Stayup — scrapes blog articles defined in the tracked sources and stores
results via stayup-api.

For each tracked source, the script fetches the listing page and extracts
article URLs:
  - If no articles exist yet for this source: saves only the latest article.
  - Otherwise: saves new articles (newest first) until a known article is found,
    up to config["max_scraps"] (default 5) articles per run.

A cleanup step removes entries older than config["retention_days"] (default 15) days.

Talks to stayup-api over HTTP (STAYUP_API_URL + STAYUP_API_KEY) — it never
touches a database directly. See stayup-api/docs/self-hosting-and-providers.md.
Sources themselves (URL + selectors) are curated separately, via admin.py —
see its own docstring.

Tracked source config:
  url     — listing page URL to scrape
  config  — scraping options:
    {
      "articles_selector":  "h2.post-title a",       # CSS selector for article links
      "content_selector":   "article.post-content",  # CSS selector for article body (optional, default: "body")
      "exclude":            ["div.ads", ".sidebar"], # CSS selectors to remove before extracting text (optional)
    }
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PROVIDER_TYPE = "scrap"

# Nom affiché du provider dans les apps (fallback : nom de table capitalisé).
DISPLAY_NAME = "Scrap"

# Où ce connecteur se classe parmi les autres dans la barre latérale.
SORT_ORDER = 40

DEFAULT_MAX_SCRAPS = 5
DEFAULT_RETENTION_DAYS = 15

# Instance stayup-api à laquelle parler, et la clé qui authentifie ce
# connecteur pour le provider 'scrap' — obtenue depuis l'admin de cette
# instance (voir stayup-api/docs/self-hosting-and-providers.md).
API_URL = os.environ.get("STAYUP_API_URL", "http://localhost:3000").rstrip("/")
API_KEY = os.environ.get("STAYUP_API_KEY")

# Manifeste d'affichage : comment les 3 apps (ui / desktop / mobile) rendent les
# lignes de ce connecteur, sans une ligne de code côté app. stayup-api le relaie
# tel quel depuis provider_registry.template, sans jamais l'interpréter.
# Schéma : voir stayup-api/docs/self-hosting-and-providers.md.
#
# Une entrée = un article scrapé. `content` est du texte brut ; `params`
# porte l'URL de l'article — d'où les accès `$row.params.url`.
DISPLAY_TEMPLATE = {
    "version": 1,
    "display": {
        "name": DISPLAY_NAME,
        # Icône auto-descriptive (tracé SVG teintable). Un globe (page web).
        "icon": {
            "paths": [
                "M12 2a10 10 0 1 0 0 20 10 10 0 1 0 0-20z",
                "M12 2a15 15 0 0 0 0 20 15 15 0 0 0 0-20",
                "M2 12h20",
            ],
            "viewBox": "0 0 24 24",
            "stroke": True,
        },
        "accent": "#9dc7e0",
        "sortOrder": SORT_ORDER,
        "feedLabel": {"path": "$source.url", "format": "domain"},
    },
    "item": {
        "parseContentAsJson": False,
        "fields": {
            "title": ["content", {"path": "$row.params.url", "format": "hostname"}],
            "subtitle": {"path": "$row.params.url", "format": "hostname"},
            "summary": "content",
            "url": "$row.params.url",
            "timestamp": "$row.executed_at",
        },
    },
    "list": {
        "layout": "row",
        "primary": "title",
        "secondary": "subtitle",
        "meta": "timestamp",
    },
    "detail": {
        "mode": "text",
        "title": {"path": "$row.params.url", "format": "hostname"},
        "body": "content",
        "openUrl": "$row.params.url",
        "openLabel": "Visit website",
    },
    # Champ « ajouter un flux » : l'URL complète de la page/section à suivre, comme
    # pour rss. Le provider est en mode `manual` : l'ajout part en file
    # d'approbation, où un admin renseigne les sélecteurs CSS (articles_selector…).
    "form": {
        "label": "Page URL to scrape",
        "placeholder": "https://blog.example.com/",
        "pattern": r"^https?://.+",
        "transform": {"trim": True},
    },
}


# ---------------------------------------------------------------------------
# stayup-api client
# ---------------------------------------------------------------------------


def api_request(method: str, path: str, **kwargs) -> dict | None:
    """Call one of stayup-api's /connector-api/scrap/* endpoints.

    Raises RuntimeError if STAYUP_API_KEY isn't set, or requests.HTTPError on
    a non-2xx response (via raise_for_status).
    """
    if not API_KEY:
        raise RuntimeError("STAYUP_API_KEY is not set.")
    url = f"{API_URL}/connector-api/{PROVIDER_TYPE}{path}"
    headers = {"Authorization": f"Bearer {API_KEY}"}
    response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def register_provider() -> None:
    """Auto-déclaration au démarrage — nom affiché et manifeste d'affichage."""
    api_request(
        "POST",
        "/register",
        json={
            "displayName": DISPLAY_NAME,
            "sortOrder": SORT_ORDER,
            "template": DISPLAY_TEMPLATE,
        },
    )


def get_sources() -> list[tuple[int, str, dict]]:
    """Return all tracked sources as (id, url, config) tuples."""
    result = api_request("GET", "/sources")
    return [(s["id"], s["url"], s.get("config") or {}) for s in result["sources"]]


def get_scraped_urls(repository_id: int) -> set[str]:
    """Return the set of article URLs already scraped for this source.

    Article URLs are stored as `version` (see save_entry) so this reuses the
    same "all known versions" endpoint as stayup-cmd-changelog, rather than
    needing the API to search inside `params`.
    """
    result = api_request("GET", f"/sources/{repository_id}/versions")
    return set(result["versions"])


def save_entry(repository_id: int, article_url: str, content: str, params: dict, executed_at: datetime) -> None:
    """Persist a scrape result. `article_url` doubles as the dedup version."""
    api_request(
        "POST",
        "/items",
        json={
            "items": [
                {
                    "repositoryId": repository_id,
                    "version": article_url,
                    "content": content,
                    "params": params,
                    "executedAt": executed_at.isoformat(),
                    "success": True,
                }
            ]
        },
    )


def save_error(repository_id: int | None, error: str, executed_at: datetime) -> None:
    """Persist a scrape error."""
    api_request(
        "POST",
        "/errors",
        json={"repositoryId": repository_id, "error": error, "executedAt": executed_at.isoformat()},
    )


def cleanup_old_entries(repository_id: int, retention_days: int) -> None:
    """Delete stored entries for a source older than retention_days days."""
    api_request(
        "DELETE",
        f"/sources/{repository_id}/old-items",
        params={"retentionDays": retention_days},
    )


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def get_article_links(page_url: str, articles_selector: str) -> list[str]:
    """Fetch a listing page and return absolute URLs of all elements matching articles_selector.

    Elements must have an href attribute (typically <a> tags).
    Relative hrefs are resolved against page_url.
    """
    resp = requests.get(page_url, timeout=30, headers={"User-Agent": "stayup-scrap/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    links = []
    for element in soup.select(articles_selector):
        href = element.get("href")
        if href:
            links.append(urljoin(page_url, href))
    return links


def scrape_page(page_url: str, css_path: str, exclude_selectors: list[str] | None = None) -> str | None:
    """Fetch a page and return the text content of the element matching css_path.

    Excluded elements (matched by exclude_selectors) are removed before text extraction.
    Returns None if no element matches css_path.
    """
    resp = requests.get(page_url, timeout=30, headers={"User-Agent": "stayup-scrap/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    element = soup.select_one(css_path)
    if element is None:
        return None
    for selector in exclude_selectors or []:
        for excluded in element.select(selector):
            excluded.decompose()
    return element.get_text(separator="\n", strip=True)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def process_repository(repository_id: int, repository_url: str, executed_at: datetime, config: dict) -> None:
    """Scrape blog articles for one source and persist new results.

    - If no articles exist yet for this source: saves only the latest article.
    - Otherwise: iterates articles newest-first, saves new ones, stops at the first
      already-known article or after max_scraps articles.
    Any exception during listing-page fetch is caught, logged, and printed to stderr.
    Errors on individual articles are logged but do not stop the run.
    """
    try:
        articles_selector = config["articles_selector"]
        content_selector = config.get("content_selector", "body")
        exclude_selectors = config.get("exclude", [])
        max_scraps = config.get("max_scraps", DEFAULT_MAX_SCRAPS)

        article_urls = get_article_links(repository_url, articles_selector)
        if not article_urls:
            return

        scraped_urls = get_scraped_urls(repository_id)

        if not scraped_urls:
            # First time: save only the latest article
            url = article_urls[0]
            try:
                content = scrape_page(url, content_selector, exclude_selectors)
                if content is None:
                    message = f"No element found at selector '{content_selector}' on {url}"
                    save_error(repository_id, message, executed_at)
                else:
                    save_entry(repository_id, url, content, {"url": url, **config}, executed_at)
            except Exception as e:
                save_error(repository_id, f"Error scraping {url}: {e}", executed_at)
                print(f"[{url}] Error: {e}", file=sys.stderr)
            return

        # Articles exist: save new ones until we hit a known one
        scraped_count = 0
        for url in article_urls:
            if scraped_count >= max_scraps:
                break

            if url in scraped_urls:
                break

            try:
                content = scrape_page(url, content_selector, exclude_selectors)
                if content is None:
                    message = f"No element found at selector '{content_selector}' on {url}"
                    save_error(repository_id, message, executed_at)
                    continue

                save_entry(repository_id, url, content, {"url": url, **config}, executed_at)
                scraped_count += 1

            except Exception as e:
                save_error(repository_id, f"Error scraping {url}: {e}", executed_at)
                print(f"[{url}] Error: {e}", file=sys.stderr)

    except Exception as e:
        save_error(repository_id, str(e), executed_at)
        print(f"[{repository_url}] Error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    register_provider()

    sources = get_sources()
    if not sources:
        print("No sources tracked. Use admin.py to add one.")
        return

    executed_at = datetime.now(tz=timezone.utc)

    for repository_id, repository_url, config in sources:
        process_repository(repository_id, repository_url, executed_at, config)
        cleanup_old_entries(repository_id, config.get("retention_days", DEFAULT_RETENTION_DAYS))


if __name__ == "__main__":
    main()
