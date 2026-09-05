#!/usr/bin/env python3
"""
Stayup scrap — small web admin to curate the scrap fluxes.

Scrap fluxes are sources of type 'scrap' on a stayup-api instance, with a JSON
config (``articles_selector``, ``content_selector``, ``exclude``, ``max_scraps``,
``retention_days``). This page never touches a database directly — it calls
stayup-api's general admin endpoints (``/ui/repositories``), the same ones the
stayup-ui admin panel uses, authenticated with the connector's own API key
(scoped to provider "scrap" — it can only see/manage repositories of that
type, never another provider's).

Two separate credentials are involved:
  SCRAP_ADMIN_EMAIL, SCRAP_ADMIN_PASSWORD   — gates who may open this page
                                               (this app's own Flask session)
  SCRAP_ADMIN_SECRET                        — Flask session signing key
  STAYUP_API_URL, STAYUP_API_KEY            — the stayup-api instance to curate
                                               and the connector key this page
                                               authenticates with (the same key
                                               scrape_pages.py uses)

Run it with gunicorn (see docker-compose.yml) or ``flask --app admin run``.
"""

from __future__ import annotations

import hmac
import os
from functools import wraps

import requests
from flask import Flask, flash, redirect, render_template, request, session, url_for

# Clés de config qu'on lit sur le formulaire. Une clé absente ou vide n'est pas
# écrite : le scraper applique ses propres défauts (content_selector="body",
# max_scraps=5, retention_days=15).
INT_KEYS = ("max_scraps", "retention_days")

API_URL = os.environ.get("STAYUP_API_URL", "http://localhost:3000").rstrip("/")


def _admin_credentials() -> tuple[str, str] | None:
    email = os.environ.get("SCRAP_ADMIN_EMAIL", "")
    password = os.environ.get("SCRAP_ADMIN_PASSWORD", "")
    if not email or not password:
        return None
    return email, password


def api_request(method: str, path: str, **kwargs) -> dict | None:
    """Call one of stayup-api's /ui/repositories/* endpoints, scoped to this
    connector's own provider (scrap) by its API key. Raises
    requests.HTTPError on a non-2xx response — callers read
    exc.response.json()["error"] for a message to show the operator."""
    api_key = os.environ.get("STAYUP_API_KEY", "")
    if not api_key:
        raise RuntimeError("STAYUP_API_KEY is not set.")
    url = f"{API_URL}/ui/repositories{path}"
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.request(method, url, headers=headers, timeout=10, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def _api_error_message(exc: requests.HTTPError) -> str:
    """Extract stayup-api's {"error": "..."} body, falling back to str(exc)."""
    try:
        return exc.response.json().get("error", str(exc)) if exc.response is not None else str(exc)
    except ValueError:
        return str(exc)


def list_scrap_fluxes() -> list[dict]:
    """All sources of type 'scrap', as this page's `fluxes` shape."""
    result = api_request("GET", "")
    return [
        {
            "id": r["id"],
            "url": r["url"],
            "config": r.get("config") or {},
            "subscriber_count": int(r.get("subscriber_count") or 0),
        }
        for r in result["repositories"]
        if r["type"] == "scrap"
    ]


def get_scrap_flux(repository_id: int) -> dict | None:
    for flux in list_scrap_fluxes():
        if flux["id"] == repository_id:
            return flux
    return None


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
        fluxes = list_scrap_fluxes()
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
        try:
            api_request("POST", "", json={"url": url, "type": "scrap", "config": config})
        except requests.HTTPError as exc:
            flash(f"Échec de l'ajout : {_api_error_message(exc)}")
            return render_template("form.html", mode="new", values=values), 400
        flash("Flux ajouté.")
        return redirect(url_for("index"))

    @app.get("/<int:repository_id>/edit")
    @login_required
    def edit_form(repository_id: int):
        flux = get_scrap_flux(repository_id)
        if flux is None:
            flash("Flux introuvable.")
            return redirect(url_for("index"))
        values = {"url": flux["url"], **_config_to_form(flux["config"])}
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
        try:
            api_request(
                "PATCH",
                f"/{repository_id}",
                json={"url": url, "config": config},
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 400
            if status == 404:
                flash("Flux introuvable.")
                return redirect(url_for("index"))
            flash(f"Échec de la modification : {_api_error_message(exc)}")
            return render_template("form.html", mode="edit", repository_id=repository_id, values=values), 400
        flash("Flux modifié.")
        return redirect(url_for("index"))

    @app.post("/<int:repository_id>/delete")
    @login_required
    def delete(repository_id: int):
        flux = get_scrap_flux(repository_id)
        if flux is None:
            flash("Flux introuvable.")
            return redirect(url_for("index"))
        if flux["subscriber_count"]:
            flash(f"Suppression refusée : {flux['subscriber_count']} abonné(s) suivent ce flux.")
            return redirect(url_for("index"))
        try:
            api_request("DELETE", f"/{repository_id}")
        except requests.HTTPError as exc:
            flash(f"Échec de la suppression : {_api_error_message(exc)}")
            return redirect(url_for("index"))
        flash("Flux supprimé.")
        return redirect(url_for("index"))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
