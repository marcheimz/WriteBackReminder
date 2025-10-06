"""Quart application factory for the WriteBackReminder web UI."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlencode

import httpx
from quart import Quart, redirect, render_template, request, session, url_for

from .ai_client import DEFAULT_MODEL as FOLLOWUP_DEFAULT_MODEL, generate_followup, load_api_key
from .config import get_config
from .datastore import ConversationStore, RecommendationEntry

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


def _load_google_credentials(credentials_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return Google OAuth client credentials loaded from JSON if available."""

    if not credentials_path.is_file():
        return None, None

    try:
        payload = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None

    client_id = payload.get("client_id")
    client_secret = payload.get("client_secret")
    if not client_id or not client_secret:
        return None, None

    return str(client_id), str(client_secret)


def create_app() -> Quart:
    base_dir = Path(__file__).resolve().parent
    template_dir = base_dir.parent / "templates"

    app_config = get_config()

    app = Quart(__name__, template_folder=str(template_dir))
    app.config["APP_CONFIG"] = app_config
    app.config["SECRET_KEY"] = app_config.secret_key

    user_data_dir = app_config.user_data_dir
    recommendation_data_dir = app_config.recommendations_dir
    app_config.google_credentials_path.parent.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    recommendation_data_dir.mkdir(parents=True, exist_ok=True)

    app.config["USER_DATA_DIR"] = str(user_data_dir)
    app.config["RECOMMENDATION_DATA_DIR"] = str(recommendation_data_dir)
    app.conversation_store = ConversationStore(user_data_dir, recommendation_data_dir)

    followup_hours = app_config.followup_refresh_hours
    followup_model = app_config.followup_model or FOLLOWUP_DEFAULT_MODEL
    app.config["FOLLOWUP_REFRESH_HOURS"] = followup_hours
    app.config["FOLLOWUP_REFRESH_INTERVAL_SECONDS"] = (
        int(followup_hours * 3600) if followup_hours > 0 else 0
    )
    app.config["FOLLOWUP_MODEL"] = followup_model
    app._followup_refresh_lock = asyncio.Lock()
    app._followup_last_run: Dict[str, float] = {}
    app._followup_in_progress: Dict[str, bool] = {}

    google_client_id, google_client_secret = _load_google_credentials(app_config.google_credentials_path)
    app.config["GOOGLE_CLIENT_ID"] = google_client_id
    app.config["GOOGLE_CLIENT_SECRET"] = google_client_secret
    app.config["GOOGLE_CREDENTIALS_PATH"] = str(app_config.google_credentials_path)

    google_login_enabled = bool(google_client_id and google_client_secret)
    app.config["GOOGLE_LOGIN_ENABLED"] = google_login_enabled

    async def maybe_refresh_followups(force: bool = False, user_filter: Optional[str] = None) -> None:
        """Refresh stored follow-up recommendations if the interval has elapsed."""

        if not load_api_key():
            return

        interval = app.config["FOLLOWUP_REFRESH_INTERVAL_SECONDS"]
        key = user_filter or "__global__"

        if not force and interval:
            now = time.monotonic()
            last = app._followup_last_run.get(key, 0.0)
            if (now - last) < interval:
                return

        async with app._followup_refresh_lock:
            if not force and interval:
                now = time.monotonic()
                last = app._followup_last_run.get(key, 0.0)
                if (now - last) < interval:
                    return

            app._followup_in_progress[key] = True
            completed = False
            try:
                await _refresh_followups(user_filter=user_filter, force=force)
                completed = True
            finally:
                app._followup_in_progress.pop(key, None)
                if completed:
                    app._followup_last_run[key] = time.monotonic()

    async def _refresh_followups(user_filter: Optional[str] = None, *, force: bool = False) -> None:
        """Iterate over stored conversations and update follow-up recommendations."""

        model = app.config["FOLLOWUP_MODEL"]
        refresh_hours = app.config["FOLLOWUP_REFRESH_HOURS"]
        max_age = timedelta(hours=refresh_hours) if refresh_hours > 0 else None

        store = app.conversation_store
        now = datetime.now(timezone.utc)

        users = (store.users() if user_filter is None else (user_filter,))
        for user in users:
            people = store.people_for_user(user)
            for person in people:
                entries = store.conversations(user, person)
                if not entries:
                    continue

                existing = store.recommendation_for(user, person)
                if (not force) and existing and max_age and (now - existing.generated_at) < max_age:
                    continue

                history = [(entry.timestamp, entry.entry_type, entry.summary) for entry in entries]
                try:
                    recommendation = await asyncio.to_thread(
                        generate_followup,
                        user,
                        person,
                        history,
                        model=model,
                        current_time=datetime.now(timezone.utc),
                    )
                except Exception as exc:  # noqa: BLE001
                    app.logger.warning(
                        "Failed to generate follow-up for user=%s person=%s: %s",
                        user,
                        person,
                        exc,
                    )
                    continue

                store.set_recommendation(
                    user,
                    person,
                    RecommendationEntry(
                        proposed_response=recommendation.proposed_response,
                        urgency=recommendation.urgency,
                        rationale=recommendation.rationale,
                        generated_at=recommendation.generated_at,
                    ),
                )

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

        await maybe_refresh_followups(user_filter=user_email)

        selected_person = request.args.get("person") or None
        known_people = app.conversation_store.people_for_user(user_email)
        conversation_entries = (
            app.conversation_store.conversations(user_email, selected_person)
            if selected_person
            else ()
        )

        recommendation = (
            app.conversation_store.recommendation_for(user_email, selected_person)
            if selected_person
            else None
        )

        refresh_in_progress = bool(app._followup_in_progress.get(user_email))

        context: Dict[str, Optional[str]] = {
            "active_user_email": user_email,
            "active_user_name": session.get("active_user_name"),
            "selected_person": selected_person,
            "known_people": known_people,
            "conversations": conversation_entries,
            "recommendation": recommendation,
            "refresh_in_progress": refresh_in_progress,
            "errors": (),
        }
        return await render_template("index.html", **context)

    @app.post("/recommendations/refresh")
    async def refresh_recommendations() -> str:
        user_email = session.get("active_user_email")
        if not user_email:
            return redirect(url_for("landing"))

        await maybe_refresh_followups(force=True, user_filter=user_email)

        referer = request.headers.get("Referer")
        if referer and referer.startswith(request.host_url):
            return redirect(referer)
        return redirect(url_for("recommendations_page"))

    @app.get("/recommendations")
    async def recommendations_page() -> str:
        user_email = session.get("active_user_email")
        if not user_email:
            return redirect(url_for("landing"))

        await maybe_refresh_followups(user_filter=user_email)

        recommendations = app.conversation_store.recommendations_for_user(user_email)
        sorted_recs = sorted(
            recommendations.items(),
            key=lambda item: item[1].urgency,
            reverse=True,
        )

        context = {
            "active_user_email": user_email,
            "active_user_name": session.get("active_user_name"),
            "recommendations": sorted_recs,
            "refresh_in_progress": bool(app._followup_in_progress.get(user_email)),
        }
        return await render_template("recommendations.html", **context)

    @app.post("/log")
    async def log_conversation() -> str:
        user_email = session.get("active_user_email")
        if not user_email:
            return redirect(url_for("landing"))

        form = await request.form
        person = (form.get("person") or "").strip()
        entry_type = (form.get("entry_type") or "conversation").strip().lower()
        entry_type = (form.get("entry_type") or "").strip().lower() or None
        summary = (form.get("summary") or "").strip()
        errors = []

        if entry_type not in {"conversation", "note"}:
            errors.append("Please choose a valid entry type.")

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
                "recommendation": app.conversation_store.recommendation_for(user_email, person)
                if person
                else None,
                "refresh_in_progress": bool(app._followup_in_progress.get(user_email)),
                "new_entry_type": entry_type,
                "draft_summary": summary,
                "errors": tuple(errors),
            }
            return await render_template("index.html", **context), 400

        app.conversation_store.add_entry(user_email, person, summary, entry_type)
        return redirect(url_for("conversations", person=person))

    @app.get("/conversations/edit")
    async def edit_entry() -> str:
        user_email = session.get("active_user_email")
        if not user_email:
            return redirect(url_for("landing"))

        person = request.args.get("person") or ""
        entry_id = request.args.get("entry") or ""

        entry = (
            app.conversation_store.get_entry(user_email, person, entry_id)
            if person and entry_id
            else None
        )

        errors: Tuple[str, ...] = ()
        if not entry:
            errors = ("Conversation entry not found.",)

        context = {
            "active_user_email": user_email,
            "active_user_name": session.get("active_user_name"),
            "person": person,
            "entry": entry,
            "errors": errors,
        }
        status = 404 if errors else 200
        return await render_template("edit_entry.html", **context), status

    @app.post("/conversations/edit")
    async def update_entry() -> str:
        user_email = session.get("active_user_email")
        if not user_email:
            return redirect(url_for("landing"))

        form = await request.form
        person = (form.get("person") or "").strip()
        entry_id = (form.get("entry_id") or "").strip()
        summary = (form.get("summary") or "").strip()

        errors = []
        entry = None
        if not person or not entry_id:
            errors.append("Missing conversation reference.")
        else:
            entry = app.conversation_store.get_entry(user_email, person, entry_id)
            if not entry:
                errors.append("Conversation entry not found.")

        if entry_type and entry_type not in {"conversation", "note"}:
            errors.append("Please choose a valid entry type.")

        if not summary:
            errors.append("Please provide an updated summary.")

        if errors:
            context = {
                "active_user_email": user_email,
                "active_user_name": session.get("active_user_name"),
                "person": person,
                "entry": entry,
                "provided_entry_type": entry_type or (entry.entry_type if entry else None),
                "draft_summary": summary,
                "errors": tuple(errors),
            }
            status = 404 if any("not found" in error.lower() for error in errors) else 400
            return await render_template("edit_entry.html", **context), status

        updated = app.conversation_store.update_entry(user_email, person, entry_id, summary, entry_type)
        if not updated:
            context = {
                "active_user_email": user_email,
                "active_user_name": session.get("active_user_name"),
                "person": person,
                "entry": entry,
                "provided_entry_type": entry_type or (entry.entry_type if entry else None),
                "draft_summary": summary,
                "errors": ("Unable to update this conversation entry.",),
            }
            return await render_template("edit_entry.html", **context), 400

        return redirect(url_for("conversations", person=person))

    @app.post("/conversations/delete")
    async def delete_entry() -> str:
        user_email = session.get("active_user_email")
        if not user_email:
            return redirect(url_for("landing"))

        form = await request.form
        person = (form.get("person") or "").strip()
        entry_id = (form.get("entry_id") or "").strip()

        if person and entry_id:
            removed = app.conversation_store.delete_entry(user_email, person, entry_id)
            if removed:
                return redirect(url_for("conversations", person=person))

        context = {
            "active_user_email": user_email,
            "active_user_name": session.get("active_user_name"),
            "person": person,
            "entry": app.conversation_store.get_entry(user_email, person, entry_id)
            if (person and entry_id)
            else None,
            "errors": ("Unable to delete the requested conversation entry.",),
        }
        return await render_template("edit_entry.html", **context), 400

    return app
