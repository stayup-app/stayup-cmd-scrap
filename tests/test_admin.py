"""
Functional tests for the web admin — require a running PostgreSQL instance.

Connection is configured via environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import json
import os

import psycopg2
import pytest

from admin import create_app
from scrape_pages import init_db

ADMIN_EMAIL = "ops@example.com"
ADMIN_PASSWORD = "s3cret-pass"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_conn():
    try:
        return psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 5432)),
            dbname=os.environ.get("DB_NAME", "stayup"),
            user=os.environ.get("DB_USER", "stayup"),
            password=os.environ.get("DB_PASSWORD", "stayup"),
        )
    except psycopg2.OperationalError as e:
        pytest.skip(f"PostgreSQL unavailable: {e}")


@pytest.fixture(scope="session")
def _schema():
    """Create the tables once. Only DB-backed tests depend on this, so the
    auth-only tests still run when no PostgreSQL is around."""
    conn = make_conn()
    init_db(conn)
    conn.close()


@pytest.fixture
def db_conn(_schema):
    conn = make_conn()
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS user_repository")
        cur.execute("TRUNCATE connector_scrap, log, repository RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SCRAP_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("SCRAP_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("SCRAP_ADMIN_SECRET", "test-secret")
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def auth_client(client):
    client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def insert_flux(conn, url="https://blog.example.com", config=None, type="scrap"):
    config = config or {"articles_selector": "h2 a", "content_selector": "article"}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repository (url, type, config) VALUES (%s, %s, %s) RETURNING id",
            (url, type, json.dumps(config)),
        )
        flux_id = cur.fetchone()[0]
    conn.commit()
    return flux_id


def read_flux(conn, flux_id):
    with conn.cursor() as cur:
        cur.execute("SELECT url, config FROM repository WHERE id = %s", (flux_id,))
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Auth
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

    def test_authenticated_visitor_sees_the_list(self, auth_client, db_conn):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert b"Flux scrap" in resp.data

    def test_logout_clears_the_session(self, auth_client):
        auth_client.post("/logout")
        resp = auth_client.get("/")
        assert resp.status_code == 302

    def test_login_reports_a_missing_configuration(self, monkeypatch):
        monkeypatch.delenv("SCRAP_ADMIN_EMAIL", raising=False)
        monkeypatch.delenv("SCRAP_ADMIN_PASSWORD", raising=False)
        app = create_app()
        app.config.update(TESTING=True)
        resp = app.test_client().post("/login", data={"email": "a@b.c", "password": "x"})
        assert resp.status_code == 401
        assert b"Admin non configur" in resp.data


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestList:
    def test_shows_a_seeded_flux(self, auth_client, db_conn):
        insert_flux(db_conn, url="https://seeded.example.com")
        resp = auth_client.get("/")
        assert b"https://seeded.example.com" in resp.data

    def test_empty_state(self, auth_client, db_conn):
        resp = auth_client.get("/")
        assert b"Aucun flux" in resp.data


class TestCreate:
    def test_inserts_a_scrap_repository(self, auth_client, db_conn):
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
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT url, type, config FROM repository")
            url, type_, config = cur.fetchone()
        assert (url, type_) == ("https://new.example.com", "scrap")
        assert config == {
            "articles_selector": "h2.title a",
            "content_selector": "article.body",
            "exclude": ["div.ads", ".sidebar"],
            "max_scraps": 9,
            "retention_days": 30,
        }

    def test_omits_empty_optional_fields(self, auth_client, db_conn):
        auth_client.post(
            "/new",
            data={"url": "https://min.example.com", "articles_selector": "a.post"},
            follow_redirects=True,
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT config FROM repository")
            config = cur.fetchone()[0]
        assert config == {"articles_selector": "a.post"}

    def test_rejects_a_missing_articles_selector(self, auth_client, db_conn):
        resp = auth_client.post("/new", data={"url": "https://x.example.com"})
        assert resp.status_code == 400
        assert b"sont requis" in resp.data
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repository")
            assert cur.fetchone()[0] == 0

    def test_surfaces_a_duplicate_url(self, auth_client, db_conn):
        insert_flux(db_conn, url="https://dup.example.com")
        resp = auth_client.post(
            "/new",
            data={"url": "https://dup.example.com", "articles_selector": "a"},
        )
        assert resp.status_code == 400
        assert "Échec de".encode() in resp.data


class TestEdit:
    def test_updates_url_and_config(self, auth_client, db_conn):
        flux_id = insert_flux(db_conn)
        auth_client.post(
            f"/{flux_id}/edit",
            data={
                "url": "https://moved.example.com",
                "articles_selector": "a.new",
                "content_selector": "main",
            },
            follow_redirects=True,
        )
        url, config = read_flux(db_conn, flux_id)
        assert url == "https://moved.example.com"
        assert config == {"articles_selector": "a.new", "content_selector": "main"}

    def test_edit_form_prefills_the_stored_values(self, auth_client, db_conn):
        flux_id = insert_flux(db_conn, config={"articles_selector": "a.keep", "exclude": ["nav"]})
        resp = auth_client.get(f"/{flux_id}/edit")
        assert b"a.keep" in resp.data
        assert b"nav" in resp.data

    def test_unknown_flux_redirects_with_a_notice(self, auth_client, db_conn):
        resp = auth_client.get("/999/edit", follow_redirects=True)
        assert b"Flux introuvable" in resp.data


class TestDelete:
    def test_removes_the_flux_its_content_and_logs(self, auth_client, db_conn):
        flux_id = insert_flux(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connector_scrap (repository_id, content, params, executed_at, success) "
                "VALUES (%s, 'x', '{}', NOW(), TRUE)",
                (flux_id,),
            )
            cur.execute(
                "INSERT INTO log (repository_id, error, executed_at) VALUES (%s, 'boom', NOW())",
                (flux_id,),
            )
        db_conn.commit()

        auth_client.post(f"/{flux_id}/delete", follow_redirects=True)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repository WHERE id = %s", (flux_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE repository_id = %s", (flux_id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM log WHERE repository_id = %s", (flux_id,))
            assert cur.fetchone()[0] == 0

    def test_is_blocked_when_a_user_follows_the_flux(self, auth_client, db_conn):
        flux_id = insert_flux(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("CREATE TABLE user_repository (id SERIAL PRIMARY KEY, user_id TEXT, repository_id INTEGER)")
            cur.execute(
                "INSERT INTO user_repository (user_id, repository_id) VALUES ('u1', %s)",
                (flux_id,),
            )
        db_conn.commit()

        resp = auth_client.post(f"/{flux_id}/delete", follow_redirects=True)
        assert "Suppression refusée".encode() in resp.data
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repository WHERE id = %s", (flux_id,))
            assert cur.fetchone()[0] == 1

    def test_delete_requires_authentication(self, client, db_conn):
        flux_id = insert_flux(db_conn)
        resp = client.post(f"/{flux_id}/delete")
        assert resp.status_code == 302
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repository WHERE id = %s", (flux_id,))
            assert cur.fetchone()[0] == 1
