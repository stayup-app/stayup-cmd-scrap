"""Tests for the web admin. stayup-api itself is mocked (unittest.mock.patch
on `requests.request`) — its actual behavior (auth, /ui/repositories CRUD,
provider scoping) is covered by stayup-api's own test suite."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from admin import create_app

ADMIN_EMAIL = "ops@example.com"
ADMIN_PASSWORD = "s3cret-pass"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def mock_response(json_body=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.content = b"{}" if json_body is not None else b""
    response.json.return_value = json_body

    def raise_for_status():
        if status >= 400:
            error = requests.HTTPError(f"{status} error")
            error.response = response
            raise error

    response.raise_for_status.side_effect = raise_for_status
    return response


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SCRAP_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("SCRAP_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("SCRAP_ADMIN_SECRET", "test-secret")
    monkeypatch.setenv("STAYUP_API_KEY", "stayup_conn_test-key")
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def auth_client(client):
    client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    return client


# ---------------------------------------------------------------------------
# Auth (this app's own Flask session — no stayup-api call involved)
# ---------------------------------------------------------------------------


class TestAuth:
    def test_index_redirects_to_login_when_anonymous(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_login_rejects_a_wrong_password(self, client):
        resp = client.post("/login", data={"email": ADMIN_EMAIL, "password": "nope"})
        assert resp.status_code == 401
        assert b"Identifiants invalides" in resp.data

    def test_login_establishes_the_session(self, client):
        resp = client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert resp.status_code == 302
        # An authenticated visitor to /login is bounced to the list.
        assert client.get("/login").status_code == 302

    def test_logout_clears_the_session(self, auth_client):
        auth_client.post("/logout")
        assert auth_client.get("/").status_code == 302

    def test_login_reports_a_missing_configuration(self, monkeypatch):
        monkeypatch.delenv("SCRAP_ADMIN_EMAIL", raising=False)
        monkeypatch.delenv("SCRAP_ADMIN_PASSWORD", raising=False)
        app = create_app()
        app.config.update(TESTING=True)
        resp = app.test_client().post("/login", data={"email": "a@b.c", "password": "x"})
        assert resp.status_code == 401
        assert b"Admin non configur" in resp.data

    def test_delete_requires_authentication(self, client):
        resp = client.post("/1/delete")
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    @patch("admin.requests.request")
    def test_shows_a_flux_from_the_api(self, mock_request, auth_client):
        mock_request.return_value = mock_response(
            {
                "repositories": [
                    {
                        "id": 1,
                        "url": "https://seeded.example.com",
                        "type": "scrap",
                        "config": {},
                        "subscriber_count": "0",
                    }
                ]
            }
        )
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert b"https://seeded.example.com" in resp.data
        headers = mock_request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer stayup_conn_test-key"

    @patch("admin.requests.request")
    def test_filters_out_other_provider_types(self, mock_request, auth_client):
        # Défense en profondeur côté client : la clé est déjà scopée par
        # l'API elle-même (elle ne renvoie que le provider "scrap"), mais le
        # filtre reste correct si jamais un autre type apparaissait ici.
        mock_request.return_value = mock_response(
            {
                "repositories": [
                    {"id": 1, "url": "https://rss.example.com", "type": "rss", "config": {}, "subscriber_count": "0"},
                ]
            }
        )
        resp = auth_client.get("/")
        assert b"https://rss.example.com" not in resp.data

    @patch("admin.requests.request")
    def test_empty_state(self, mock_request, auth_client):
        mock_request.return_value = mock_response({"repositories": []})
        resp = auth_client.get("/")
        assert b"Aucun flux" in resp.data


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    @patch("admin.requests.request")
    def test_posts_the_url_and_config_to_the_api(self, mock_request, auth_client):
        mock_request.return_value = mock_response({"id": 7, "url": "https://new.example.com"}, status=201)

        resp = auth_client.post(
            "/new",
            data={
                "url": "https://new.example.com",
                "articles_selector": "h2.title a",
                "content_selector": "article.body",
                "exclude": "div.ads\n.sidebar",
                "max_scraps": "9",
                "retention_days": "30",
            },
        )
        # Redirige vers la liste sans la suivre : la suivre déclencherait un
        # second appel (GET la liste), hors sujet de ce test.
        assert resp.status_code == 302
        method, url = mock_request.call_args[0]
        assert method == "POST"
        assert url.endswith("/ui/repositories")
        body = mock_request.call_args.kwargs["json"]
        assert body == {
            "url": "https://new.example.com",
            "type": "scrap",
            "config": {
                "articles_selector": "h2.title a",
                "content_selector": "article.body",
                "exclude": ["div.ads", ".sidebar"],
                "max_scraps": 9,
                "retention_days": 30,
            },
        }

    def test_rejects_a_missing_articles_selector(self, auth_client):
        resp = auth_client.post("/new", data={"url": "https://x.example.com"})
        assert resp.status_code == 400
        assert b"sont requis" in resp.data

    @patch("admin.requests.request")
    def test_surfaces_an_api_error(self, mock_request, auth_client):
        mock_request.return_value = mock_response(
            {"error": "This URL is already registered under another provider"}, status=409
        )

        resp = auth_client.post(
            "/new",
            data={"url": "https://dup.example.com", "articles_selector": "a"},
        )
        assert resp.status_code == 400
        assert b"already registered" in resp.data


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


class TestEdit:
    @patch("admin.requests.request")
    def test_updates_url_and_config(self, mock_request, auth_client):
        # `update()` ne fait qu'un seul appel — PATCH directement, pas de
        # lecture préalable — donc pas de `follow_redirects` ici : le suivre
        # déclencherait un second appel (GET la liste), hors sujet.
        mock_request.return_value = mock_response({"success": True})

        resp = auth_client.post(
            "/3/edit",
            data={"url": "https://moved.example.com", "articles_selector": "a.new", "content_selector": "main"},
        )
        assert resp.status_code == 302
        method, url = mock_request.call_args[0]
        assert method == "PATCH"
        assert url.endswith("/ui/repositories/3")
        body = mock_request.call_args.kwargs["json"]
        assert body == {
            "url": "https://moved.example.com",
            "config": {"articles_selector": "a.new", "content_selector": "main"},
        }

    @patch("admin.requests.request")
    def test_edit_form_prefills_the_stored_values(self, mock_request, auth_client):
        mock_request.return_value = mock_response(
            {
                "repositories": [
                    {
                        "id": 3,
                        "url": "https://old.example.com",
                        "type": "scrap",
                        "config": {"articles_selector": "a.keep", "exclude": ["nav"]},
                    }
                ]
            }
        )
        resp = auth_client.get("/3/edit")
        assert b"a.keep" in resp.data
        assert b"nav" in resp.data

    @patch("admin.requests.request")
    def test_unknown_flux_redirects_with_a_notice(self, mock_request, auth_client):
        mock_request.return_value = mock_response({"repositories": []})
        resp = auth_client.get("/999/edit", follow_redirects=True)
        assert b"Flux introuvable" in resp.data


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    @patch("admin.requests.request")
    def test_removes_the_flux_via_the_api(self, mock_request, auth_client):
        mock_request.side_effect = [
            mock_response(
                {
                    "repositories": [
                        {
                            "id": 4,
                            "url": "https://x.example.com",
                            "type": "scrap",
                            "config": {},
                            "subscriber_count": "0",
                        }
                    ]
                }
            ),
            mock_response({"success": True}),
        ]

        # Pas de `follow_redirects` : le suivre déclencherait un 3e appel (GET
        # la liste, pour la page vers laquelle on redirige), hors sujet.
        auth_client.post("/4/delete")

        method, url = mock_request.call_args_list[1][0]
        assert method == "DELETE"
        assert url.endswith("/ui/repositories/4")

    @patch("admin.requests.request")
    def test_is_blocked_when_a_user_follows_the_flux(self, mock_request, auth_client):
        mock_request.return_value = mock_response(
            {
                "repositories": [
                    {"id": 4, "url": "https://x.example.com", "type": "scrap", "config": {}, "subscriber_count": "2"}
                ]
            }
        )

        # `follow_redirects` ici : le message flash ne s'affiche que sur la
        # page suivante, qui refait donc son propre appel liste (2 appels
        # liste au total, jamais de DELETE).
        resp = auth_client.post("/4/delete", follow_redirects=True)
        assert b"Suppression refus\xc3\xa9e" in resp.data
        assert mock_request.call_count == 2
        for call in mock_request.call_args_list:
            assert call.args[0] == "GET"
