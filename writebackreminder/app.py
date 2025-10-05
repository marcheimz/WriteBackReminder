"""Quart application factory for the WriteBackReminder web UI."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlencode

import httpx
from quart import Quart, redirect, render_template, request, session, url_for

from .datastore import ConversationStore

DEFAULT_GOOGLE_CREDENTIALS_FILE = "google_oauth.json"
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


def _load_google_credentials(base_dir: Path) -> Tuple[Optional[str], Optional[str], Optional[Path]]:
    """Return Google OAuth client credentials loaded from JSON if available."""
    configured_path = os.getenv("GOOGLE_CREDENTIALS_FILE")
    if configured_path:
        candidate = Path(configured_path).expanduser()
    else:
        candidate = base_dir.parent / DEFAULT_GOOGLE_CREDENTIALS_FILE

    if not candidate.is_file():
        return None, None, None

    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None, candidate

    client_id = payload.get("client_id")
    client_secret = payload.get("client_secret")
    if not client_id or not client_secret:
        return None, None, candidate

    return str(client_id), str(client_secret), candidate


def create_app() -> Quart:
    base_dir = Path(__file__).resolve().parent
    template_dir = base_dir.parent / "templates"

    app = Quart(__name__, template_folder=str(template_dir))
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev")

    user_data_dir = base_dir.parent / "userdata"
    app.config["USER_DATA_DIR"] = str(user_data_dir)
    app.conversation_store = ConversationStore(user_data_dir)

    google_client_id, google_client_secret, credentials_path = _load_google_credentials(base_dir)
    app.config["GOOGLE_CLIENT_ID"] = google_client_id
    app.config["GOOGLE_CLIENT_SECRET"] = google_client_secret
    app.config["GOOGLE_CREDENTIALS_PATH"] = str(credentials_path) if credentials_path else None

    google_login_enabled = bool(google_client_id and google_client_secret)
    app.config["GOOGLE_LOGIN_ENABLED"] = google_login_enabled

    def login_context(errors: Iterable[str] = ()) -> Dict[str, object]:
        return {
            "google_login_enabled": google_login_enabled,
            "errors": tuple(errors),
        }

    @app.get("/")
    async def landing() -> str:
        if session.get("active_user_email"):
            return redirect(url_for("conversations"))
        return await render_template("login.html", **login_context())

    @app.get("/login/google")
    async def login_google() -> str:
        if not google_login_enabled:
            return await render_template(
                "login.html",
                **login_context(errors=("Google login is not configured for this deployment.",)),
            ), 503

        state = secrets.token_urlsafe(16)
        session["oauth_state"] = state

        redirect_uri = url_for("auth_google", _external=True)
        params = {
            "client_id": google_client_id,
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": redirect_uri,
            "state": state,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        }
        auth_url = f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"
        return redirect(auth_url)

    @app.get("/auth/google")
    async def auth_google() -> str:
        if not google_login_enabled:
            return redirect(url_for("landing"))

        state = request.args.get("state")
        code = request.args.get("code")
        saved_state = session.pop("oauth_state", None)

        if not code or not state or state != saved_state:
            return await render_template(
                "login.html",
                **login_context(errors=("Invalid Google sign-in response. Please try again.",)),
            ), 400

        redirect_uri = url_for("auth_google", _external=True)

        async with httpx.AsyncClient(timeout=10) as client:
            try:
                token_response = await client.post(
                    GOOGLE_TOKEN_ENDPOINT,
                    data={
                        "code": code,
                        "client_id": google_client_id,
                        "client_secret": google_client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError:
                return await render_template(
                    "login.html",
                    **login_context(errors=("Failed to contact Google for sign-in.",)),
                ), 400

            if token_response.status_code != 200:
                return await render_template(
                    "login.html",
                    **login_context(errors=("Google sign-in was rejected. Please try again.",)),
                ), 400

            token_payload = token_response.json()
            access_token = token_payload.get("access_token")
            if not access_token:
                return await render_template(
                    "login.html",
                    **login_context(errors=("Google sign-in did not return an access token.",)),
                ), 400

            try:
                userinfo_response = await client.get(
                    GOOGLE_USERINFO_ENDPOINT,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.HTTPError:
                return await render_template(
                    "login.html",
                    **login_context(errors=("Failed to fetch Google account details.",)),
                ), 400

        if userinfo_response.status_code != 200:
            return await render_template(
                "login.html",
                **login_context(errors=("Unable to read Google account details.",)),
            ), 400

        user_info = userinfo_response.json()
        email = (user_info.get("email") or "").lower()
        if not email:
            return await render_template(
                "login.html",
                **login_context(errors=("Google account did not return an email address.",)),
            ), 400

        session["active_user_email"] = email
        session["active_user_name"] = (
            (user_info.get("name") or user_info.get("given_name") or "").strip() or email
        )
        return redirect(url_for("conversations"))

    @app.post("/logout")
    async def logout() -> str:
        session.pop("active_user_email", None)
        session.pop("active_user_name", None)
        session.pop("oauth_state", None)
        return redirect(url_for("landing"))

    @app.get("/conversations")
    async def conversations() -> str:
        user_email = session.get("active_user_email")
        if not user_email:
            return redirect(url_for("landing"))

        selected_person = request.args.get("person") or None
        known_people = app.conversation_store.people_for_user(user_email)
        conversation_entries = (
            app.conversation_store.conversations(user_email, selected_person)
            if selected_person
            else ()
        )

        context: Dict[str, Optional[str]] = {
            "active_user_email": user_email,
            "active_user_name": session.get("active_user_name"),
            "selected_person": selected_person,
            "known_people": known_people,
            "conversations": conversation_entries,
            "errors": (),
        }
        return await render_template("index.html", **context)

    @app.post("/log")
    async def log_conversation() -> str:
        user_email = session.get("active_user_email")
        if not user_email:
            return redirect(url_for("landing"))

        form = await request.form
        person = (form.get("person") or "").strip()
        summary = (form.get("summary") or "").strip()
        errors = []

        if not person:
            errors.append("Please specify who you spoke with.")

        if not summary:
            errors.append("Please add a short summary of the conversation.")

        if errors:
            known_people = app.conversation_store.people_for_user(user_email)
            conversation_entries = (
                app.conversation_store.conversations(user_email, person)
                if person
                else ()
            )
            context = {
                "active_user_email": user_email,
                "active_user_name": session.get("active_user_name"),
                "selected_person": person or None,
                "known_people": known_people,
                "conversations": conversation_entries,
                "errors": tuple(errors),
            }
            return await render_template("index.html", **context), 400

        app.conversation_store.add_entry(user_email, person, summary)
        return redirect(url_for("conversations", person=person))

    return app
