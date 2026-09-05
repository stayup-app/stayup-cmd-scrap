"""Unit tests — no external dependencies. stayup-api itself is mocked
(unittest.mock.patch on `requests.request`); its actual behavior is covered
by stayup-api's own test suite. Network access to scraped pages is mocked too."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from scrape_pages import (
    DISPLAY_TEMPLATE,
    cleanup_old_entries,
    get_article_links,
    get_scraped_urls,
    get_sources,
    process_repository,
    register_provider,
    save_entry,
    save_error,
    scrape_page,
)

# ---------------------------------------------------------------------------
# api_request helpers
# ---------------------------------------------------------------------------


def mock_response(json_body=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.content = b"{}" if json_body is not None else b""
    response.json.return_value = json_body
    response.raise_for_status.return_value = None
    return response


@patch("scrape_pages.API_KEY", "test-key")
class TestRegisterProvider:
    @patch("scrape_pages.requests.request")
    def test_posts_display_name_sort_order_and_template(self, mock_request):
        mock_request.return_value = mock_response()
        register_provider()
        method, url = mock_request.call_args[0]
        assert method == "POST"
        assert url.endswith("/connector-api/scrap/register")
        body = mock_request.call_args.kwargs["json"]
        assert body["displayName"] == "Scrap"
        assert body["sortOrder"] == 40
        assert body["template"] == DISPLAY_TEMPLATE


class TestApiRequestWithoutKey:
    @patch("scrape_pages.API_KEY", None)
    def test_raises_when_no_api_key_is_configured(self):
        with pytest.raises(RuntimeError, match="STAYUP_API_KEY"):
            register_provider()


@patch("scrape_pages.API_KEY", "test-key")
class TestGetSources:
    @patch("scrape_pages.requests.request")
    def test_returns_id_url_config_tuples(self, mock_request):
        mock_request.return_value = mock_response(
            {"sources": [{"id": 1, "url": "https://example.com", "config": {"articles_selector": "a.post"}}]}
        )
        result = get_sources()
        assert result == [(1, "https://example.com", {"articles_selector": "a.post"})]


@patch("scrape_pages.API_KEY", "test-key")
class TestGetScrapedUrls:
    @patch("scrape_pages.requests.request")
    def test_returns_set_of_urls(self, mock_request):
        mock_request.return_value = mock_response(
            {"versions": ["https://example.com/post-1", "https://example.com/post-2"]}
        )
        assert get_scraped_urls(1) == {"https://example.com/post-1", "https://example.com/post-2"}
        url = mock_request.call_args[0][1]
        assert url.endswith("/connector-api/scrap/sources/1/versions")

    @patch("scrape_pages.requests.request")
    def test_returns_empty_set_when_nothing_scraped(self, mock_request):
        mock_request.return_value = mock_response({"versions": []})
        assert get_scraped_urls(1) == set()


@patch("scrape_pages.API_KEY", "test-key")
class TestSaveEntry:
    @patch("scrape_pages.requests.request")
    def test_posts_the_url_as_version_and_params(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        executed_at = datetime.now(tz=timezone.utc)
        params = {"url": "https://example.com/post-1", "articles_selector": "a.post"}
        save_entry(1, "https://example.com/post-1", "content", params, executed_at)

        item = mock_request.call_args.kwargs["json"]["items"][0]
        assert item["repositoryId"] == 1
        assert item["version"] == "https://example.com/post-1"
        assert item["content"] == "content"
        assert item["params"] == params
        assert item["success"] is True


@patch("scrape_pages.API_KEY", "test-key")
class TestSaveError:
    @patch("scrape_pages.requests.request")
    def test_posts_the_error(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        executed_at = datetime.now(tz=timezone.utc)
        save_error(5, "something went wrong", executed_at)
        body = mock_request.call_args.kwargs["json"]
        assert body == {"repositoryId": 5, "error": "something went wrong", "executedAt": executed_at.isoformat()}

    @patch("scrape_pages.requests.request")
    def test_accepts_none_repository_id(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        save_error(None, "error", datetime.now(tz=timezone.utc))
        assert mock_request.call_args.kwargs["json"]["repositoryId"] is None


@patch("scrape_pages.API_KEY", "test-key")
class TestCleanupOldEntries:
    @patch("scrape_pages.requests.request")
    def test_sends_retention_days_as_a_query_param(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        cleanup_old_entries(7, 30)
        method, url = mock_request.call_args[0]
        assert method == "DELETE"
        assert url.endswith("/connector-api/scrap/sources/7/old-items")
        assert mock_request.call_args.kwargs["params"] == {"retentionDays": 30}


class TestDisplayTemplate:
    def test_round_trips_through_json_unchanged(self):
        assert json.loads(json.dumps(DISPLAY_TEMPLATE)) == DISPLAY_TEMPLATE

    def test_ships_a_self_describing_icon(self):
        # Le connecteur fournit son icône (tracé SVG teintable), pas une clé du
        # jeu intégré des apps : un nouveau connecteur s'affiche sans toucher au code.
        icon = DISPLAY_TEMPLATE["display"]["icon"]
        assert isinstance(icon, dict)
        assert icon["paths"]
        assert all(p[:1] in ("M", "m") for p in icon["paths"])
        assert icon["viewBox"] == "0 0 24 24"

    def test_text_detail_reads_url_from_params(self):
        assert DISPLAY_TEMPLATE["detail"]["mode"] == "text"
        assert DISPLAY_TEMPLATE["detail"]["openUrl"] == "$row.params.url"
        assert DISPLAY_TEMPLATE["item"]["parseContentAsJson"] is False


# ---------------------------------------------------------------------------
# get_article_links
# ---------------------------------------------------------------------------


class TestGetArticleLinks:
    @patch("scrape_pages.requests.get")
    def test_returns_absolute_urls(self, mock_get):
        mock_get.return_value.text = (
            "<html><body>"
            '<a class="post" href="https://example.com/post-1">Post 1</a>'
            '<a class="post" href="https://example.com/post-2">Post 2</a>'
            "</body></html>"
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = get_article_links("https://example.com", "a.post")
        assert result == ["https://example.com/post-1", "https://example.com/post-2"]

    @patch("scrape_pages.requests.get")
    def test_resolves_relative_hrefs(self, mock_get):
        mock_get.return_value.text = "<html><body>" '<a class="post" href="/blog/post-1">Post 1</a>' "</body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        result = get_article_links("https://example.com", "a.post")
        assert result == ["https://example.com/blog/post-1"]

    @patch("scrape_pages.requests.get")
    def test_skips_elements_without_href(self, mock_get):
        mock_get.return_value.text = (
            "<html><body>" '<a class="post">No href</a>' '<a class="post" href="/post-1">With href</a>' "</body></html>"
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = get_article_links("https://example.com", "a.post")
        assert result == ["https://example.com/post-1"]

    @patch("scrape_pages.requests.get")
    def test_returns_empty_list_when_no_match(self, mock_get):
        mock_get.return_value.text = "<html><body><p>No links here</p></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        result = get_article_links("https://example.com", "a.post")
        assert result == []

    @patch("scrape_pages.requests.get")
    def test_raises_on_http_error(self, mock_get):
        mock_get.return_value.raise_for_status.side_effect = Exception("404 Not Found")
        with pytest.raises(Exception, match="404"):
            get_article_links("https://example.com", "a.post")


# ---------------------------------------------------------------------------
# scrape_page
# ---------------------------------------------------------------------------


class TestScrapePage:
    @patch("scrape_pages.requests.get")
    def test_returns_text_of_matched_element(self, mock_get):
        mock_get.return_value.text = "<html><body><main><p>Hello world</p></main></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        result = scrape_page("https://example.com", "main")
        assert result == "Hello world"

    @patch("scrape_pages.requests.get")
    def test_returns_none_when_no_match(self, mock_get):
        mock_get.return_value.text = "<html><body><div>content</div></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        result = scrape_page("https://example.com", "article")
        assert result is None

    @patch("scrape_pages.requests.get")
    def test_raises_on_http_error(self, mock_get):
        mock_get.return_value.raise_for_status.side_effect = Exception("404 Not Found")
        with pytest.raises(Exception, match="404"):
            scrape_page("https://example.com", "main")

    @patch("scrape_pages.requests.get")
    def test_exclude_selectors_removed_from_content(self, mock_get):
        mock_get.return_value.text = (
            "<html><body><main>"
            "<p>Real content</p>"
            '<div class="ads">Ad banner</div>'
            '<span class="sidebar">Sidebar</span>'
            "</main></body></html>"
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = scrape_page("https://example.com", "main", ["div.ads", ".sidebar"])
        assert "Real content" in result
        assert "Ad banner" not in result
        assert "Sidebar" not in result

    @patch("scrape_pages.requests.get")
    def test_exclude_selectors_only_within_content_element(self, mock_get):
        mock_get.return_value.text = (
            "<html><body>" '<div class="ads">Outside ads</div>' "<main><p>Content</p></main>" "</body></html>"
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = scrape_page("https://example.com", "main", ["div.ads"])
        assert result == "Content"

    @patch("scrape_pages.requests.get")
    def test_empty_exclude_list_behaves_like_no_exclusions(self, mock_get):
        mock_get.return_value.text = "<html><body><main><p>Hello</p></main></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        assert scrape_page("https://example.com", "main", []) == "Hello"

    @patch("scrape_pages.requests.get")
    def test_none_exclude_behaves_like_no_exclusions(self, mock_get):
        mock_get.return_value.text = "<html><body><main><p>Hello</p></main></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        assert scrape_page("https://example.com", "main", None) == "Hello"


# ---------------------------------------------------------------------------
# process_repository — end to end, stayup-api and network mocked
# ---------------------------------------------------------------------------


@patch("scrape_pages.API_KEY", "test-key")
class TestProcessRepository:
    _url = "https://blog.example.com"

    def _make_config(self):
        return {
            "articles_selector": "a.post",
            "content_selector": "article",
        }

    @patch("scrape_pages.save_entry")
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_scraped_urls")
    @patch("scrape_pages.get_article_links")
    def test_saves_only_latest_when_no_existing_scraps(self, mock_links, mock_scraped, mock_scrape, mock_save):
        mock_links.return_value = [
            "https://blog.example.com/post-3",
            "https://blog.example.com/post-2",
            "https://blog.example.com/post-1",
        ]
        mock_scraped.return_value = set()
        mock_scrape.return_value = "Article content"

        process_repository(1, self._url, datetime.now(tz=timezone.utc), self._make_config())

        mock_scrape.assert_called_once_with("https://blog.example.com/post-3", "article", [])
        mock_save.assert_called_once()

    @patch("scrape_pages.save_entry")
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_scraped_urls")
    @patch("scrape_pages.get_article_links")
    def test_does_nothing_when_latest_already_scraped(self, mock_links, mock_scraped, mock_scrape, mock_save):
        mock_links.return_value = ["https://blog.example.com/post-1", "https://blog.example.com/post-2"]
        mock_scraped.return_value = {"https://blog.example.com/post-1"}

        process_repository(1, self._url, datetime.now(tz=timezone.utc), self._make_config())

        mock_scrape.assert_not_called()
        mock_save.assert_not_called()

    @patch("scrape_pages.save_entry")
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_scraped_urls")
    @patch("scrape_pages.get_article_links")
    def test_stops_at_max_scraps(self, mock_links, mock_scraped, mock_scrape, mock_save):
        mock_links.return_value = [f"https://blog.example.com/post-{i}" for i in range(10)]
        mock_scraped.return_value = {"https://blog.example.com/already-known"}
        mock_scrape.return_value = "Content"

        process_repository(1, self._url, datetime.now(tz=timezone.utc), self._make_config())

        assert mock_scrape.call_count == 5

    @patch("scrape_pages.save_entry")
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_scraped_urls")
    @patch("scrape_pages.get_article_links")
    def test_stops_when_known_article_found(self, mock_links, mock_scraped, mock_scrape, mock_save):
        mock_links.return_value = [
            "https://blog.example.com/post-3",
            "https://blog.example.com/post-2",
            "https://blog.example.com/post-1",
        ]
        # post-3 is new, post-2 is already known — should stop before post-1
        mock_scraped.return_value = {"https://blog.example.com/post-2"}
        mock_scrape.return_value = "Content"

        process_repository(1, self._url, datetime.now(tz=timezone.utc), self._make_config())

        mock_scrape.assert_called_once_with("https://blog.example.com/post-3", "article", [])

    @patch("scrape_pages.save_error")
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_scraped_urls")
    @patch("scrape_pages.get_article_links")
    def test_logs_error_when_content_selector_not_found(self, mock_links, mock_scraped, mock_scrape, mock_save_error):
        mock_links.return_value = ["https://blog.example.com/post-1"]
        mock_scraped.return_value = set()
        mock_scrape.return_value = None  # Selector found nothing

        process_repository(1, self._url, datetime.now(tz=timezone.utc), self._make_config())

        mock_save_error.assert_called_once()

    @patch("scrape_pages.save_error")
    @patch("scrape_pages.get_article_links")
    def test_logs_error_on_listing_page_failure(self, mock_links, mock_save_error):
        mock_links.side_effect = Exception("connection timeout")

        process_repository(1, self._url, datetime.now(tz=timezone.utc), self._make_config())

        mock_save_error.assert_called_once()
        assert "connection timeout" in mock_save_error.call_args[0][1]

    @patch("scrape_pages.save_error")
    @patch("scrape_pages.save_entry")
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_scraped_urls")
    @patch("scrape_pages.get_article_links")
    def test_continues_after_per_article_error(self, mock_links, mock_scraped, mock_scrape, mock_save, mock_save_error):
        mock_links.return_value = [
            "https://blog.example.com/post-1",
            "https://blog.example.com/post-2",
        ]
        mock_scraped.return_value = {"https://blog.example.com/already-known"}
        mock_scrape.side_effect = [Exception("timeout"), "Content of post 2"]

        process_repository(1, self._url, datetime.now(tz=timezone.utc), self._make_config())

        # Second article was still scraped despite first failing
        assert mock_scrape.call_count == 2
        mock_save.assert_called_once()
        mock_save_error.assert_called_once()

    @patch("scrape_pages.get_article_links")
    def test_does_nothing_when_listing_page_returns_no_links(self, mock_links):
        mock_links.return_value = []
        # Ne doit rien appeler côté API — aucun mock nécessaire au-delà de get_article_links.
        process_repository(1, self._url, datetime.now(tz=timezone.utc), self._make_config())

    @patch("scrape_pages.save_entry")
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_scraped_urls")
    @patch("scrape_pages.get_article_links")
    def test_passes_exclude_selectors_from_config(self, mock_links, mock_scraped, mock_scrape, _save):
        mock_links.return_value = ["https://blog.example.com/post-1"]
        mock_scraped.return_value = set()
        mock_scrape.return_value = "Content"

        config = {**self._make_config(), "exclude": ["div.ads", ".comments"]}
        process_repository(1, self._url, datetime.now(tz=timezone.utc), config)

        mock_scrape.assert_called_once_with("https://blog.example.com/post-1", "article", ["div.ads", ".comments"])
