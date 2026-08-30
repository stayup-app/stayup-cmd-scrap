#!/usr/bin/env python3
"""
Stayup scrap — small web admin to curate the scrap fluxes.

Scrap fluxes are rows in the shared ``repository`` table with ``type = 'scrap'``
and a JSON ``config`` (``articles_selector``, ``content_selector``, ``exclude``,
``max_scraps``, ``retention_days``). They used to be added from the stayup-ui
admin, which forced that app to know one connector's config shape. This page
moves that job next to the scraper.

Auth is a single operator account read from the environment:
  SCRAP_ADMIN_EMAIL, SCRAP_ADMIN_PASSWORD   — the login
  SCRAP_ADMIN_SECRET                        — Flask session signing key

Run it with gunicorn (see docker-compose.yml) or ``flask --app admin run``.
"""

from __future__ import annotations

import hmac
import json
import os
from functools import wraps

from flask import Flask, flash, redirect, render_template, request, session, url_for

from scrape_pages import get_db_conn

# Clés de config qu'on lit sur le formulaire. Une clé absente ou vide n'est pas
# écrite : le scraper applique ses propres défauts (content_selector="body",
# max_scraps=5, retention_days=15).
INT_KEYS = ("max_scraps", "retention_days")


def _admin_credentials() -> tuple[str, str] | None:
    email = os.environ.get("SCRAP_ADMIN_EMAIL", "")
    password = os.environ.get("SCRAP_ADMIN_PASSWORD", "")
    if not email or not password:
        return None
    return email, password


def _parse_config(form) -> dict:
    """Build a scrap config dict from the submitted form, skipping empty fields."""
    config: dict = {}

    articles_selector = form.get("articles_selector", "").strip()
    if articles_selector:
        config["articles_selector"] = articles_selector

    content_selector = form.get("content_selector", "").strip()
    if content_selector:
        config["content_selector"] = content_selector

    exclude = [line.strip() for line in form.get("exclude", "").splitlines() if line.strip()]
    if exclude:
        config["exclude"] = exclude

    for key in INT_KEYS:
        raw = form.get(key, "").strip()
        if raw:
            try:
                config[key] = int(raw)
            except ValueError:
                pass

    return config


def _config_to_form(config: dict) -> dict:
    """Inverse of _parse_config: render a stored config back into form fields."""
    return {
        "articles_selector": config.get("articles_selector", ""),
        "content_selector": config.get("content_selector", ""),
        "exclude": "\n".join(config.get("exclude", [])),
        "max_scraps": config.get("max_scraps", ""),
        "retention_days": config.get("retention_days", ""),
    }


def _subscriber_count(cur, repository_id: int) -> int:
    """How many users follow this flux. 0 when the API's table isn't in this DB."""
    cur.execute("SELECT to_regclass('public.user_repository')")
    if cur.fetchone()[0] is None:
        return 0
    cur.execute(
        "SELECT COUNT(*) FROM user_repository WHERE repository_id = %s",
        (repository_id,),
    )
    return cur.fetchone()[0]


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SCRAP_ADMIN_SECRET") or os.urandom(32)

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    @app.get("/login")
    def login_form():
        if session.get("user"):
            return redirect(url_for("index"))
        return render_template("login.html")

    @app.post("/login")
    def login():
        creds = _admin_credentials()
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        ok = creds is not None and hmac.compare_digest(email, creds[0]) and hmac.compare_digest(password, creds[1])
        if not ok:
            flash(
                "Identifiants invalides."
                if creds
                else "Admin non configuré (SCRAP_ADMIN_EMAIL / SCRAP_ADMIN_PASSWORD)."
            )
            return render_template("login.html"), 401
        session["user"] = email
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login_form"))

    @app.get("/")
    @login_required
    def index():
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.id, r.url, r.config, r.created_at,
                           (SELECT COUNT(*) FROM connector_scrap cs WHERE cs.repository_id = r.id)
                    FROM repository r
                    WHERE r.type = 'scrap'
                    ORDER BY r.id
                    """)
                rows = cur.fetchall()
        finally:
            conn.close()
        fluxes = [
            {
                "id": row[0],
                "url": row[1],
                "config": row[2] if isinstance(row[2], dict) else json.loads(row[2] or "{}"),
                "created_at": row[3],
                "articles": row[4],
            }
            for row in rows
        ]
        return render_template("list.html", fluxes=fluxes)

    @app.get("/new")
    @login_required
    def new_form():
        return render_template("form.html", mode="new", values={"url": "", **_config_to_form({})})

    @app.post("/new")
    @login_required
    def create():
        url = request.form.get("url", "").strip()
        config = _parse_config(request.form)
        values = {"url": url, **_config_to_form(config)}
        if not url or "articles_selector" not in config:
            flash("L'URL et le sélecteur d'articles sont requis.")
            return render_template("form.html", mode="new", values=values), 400
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO repository (url, type, config) VALUES (%s, 'scrap', %s)",
                    (url, json.dumps(config)),
                )
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced to the operator, not swallowed
            conn.rollback()
            flash(f"Échec de l'ajout : {exc}")
            return render_template("form.html", mode="new", values=values), 400
        finally:
            conn.close()
        flash("Flux ajouté.")
        return redirect(url_for("index"))

    @app.get("/<int:repository_id>/edit")
    @login_required
    def edit_form(repository_id: int):
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT url, config FROM repository WHERE id = %s AND type = 'scrap'",
                    (repository_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            flash("Flux introuvable.")
            return redirect(url_for("index"))
        config = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
        values = {"url": row[0], **_config_to_form(config)}
        return render_template("form.html", mode="edit", repository_id=repository_id, values=values)

    @app.post("/<int:repository_id>/edit")
    @login_required
    def update(repository_id: int):
        url = request.form.get("url", "").strip()
        config = _parse_config(request.form)
        values = {"url": url, **_config_to_form(config)}
        if not url or "articles_selector" not in config:
            flash("L'URL et le sélecteur d'articles sont requis.")
            return render_template("form.html", mode="edit", repository_id=repository_id, values=values), 400
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE repository SET url = %s, config = %s WHERE id = %s AND type = 'scrap'",
                    (url, json.dumps(config), repository_id),
                )
                missing = cur.rowcount == 0
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            flash(f"Échec de la modification : {exc}")
            return render_template("form.html", mode="edit", repository_id=repository_id, values=values), 400
        finally:
            conn.close()
        flash("Flux introuvable." if missing else "Flux modifié.")
        return redirect(url_for("index"))

    @app.post("/<int:repository_id>/delete")
    @login_required
    def delete(repository_id: int):
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                followers = _subscriber_count(cur, repository_id)
                if followers:
                    conn.rollback()
                    flash(f"Suppression refusée : {followers} abonné(s) suivent ce flux.")
                    return redirect(url_for("index"))
                cur.execute("DELETE FROM connector_scrap WHERE repository_id = %s", (repository_id,))
                cur.execute("DELETE FROM log WHERE repository_id = %s", (repository_id,))
                cur.execute("DELETE FROM repository WHERE id = %s AND type = 'scrap'", (repository_id,))
                missing = cur.rowcount == 0
            conn.commit()
        finally:
            conn.close()
        flash("Flux introuvable." if missing else "Flux supprimé.")
        return redirect(url_for("index"))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
