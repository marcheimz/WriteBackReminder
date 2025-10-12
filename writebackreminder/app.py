"""FastAPI application factory for the WriteBackReminder web UI."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.templating import Jinja2Templates
from starlette.routing import NoMatchFound
from jinja2 import pass_context

from .ai_client import DEFAULT_MODEL as FOLLOWUP_DEFAULT_MODEL, generate_followup, load_api_key
from .config import get_config
from .datastore import ConversationStore, RecommendationEntry

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


def _load_google_credentials(credentials_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return Google OAuth client credentials from environment variables only."""
    env_client_id = os.getenv("GOOGLE_CLIENT_ID")
    env_client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if env_client_id and env_client_secret:
        return env_client_id, env_client_secret
    return None, None


def create_app() -> FastAPI:
    base_dir = Path(__file__).resolve().parent
    template_dir = base_dir.parent / "templates"

    app_config = get_config()

    app = FastAPI()

    # Respect X-Forwarded-Proto/For from Fly's proxy so generated URLs use https
    app.add_middleware(ProxyHeadersMiddleware)

    # Sessions for login state (use distinct cookie name to avoid clashes)
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_config.secret_key,
        session_cookie="wbr_session",
    )

    # Templates
    templates = Jinja2Templates(directory=str(template_dir))

    @pass_context
    def _jinja_url_for(context, name: str, **params) -> str:
        request: Request = context["request"]
        # Prefer path-only URLs to avoid scheme/host issues behind proxies
        try:
            base = request.app.url_path_for(name)
        except NoMatchFound:
            # Fall back to root if route is missing
            base = "/"
        if params:
            qs = urlencode(params, doseq=True)
            sep = "&" if ("?" in base) else "?"
            return f"{base}{sep}{qs}"
        return base

    # Override default to also handle query parameters
    templates.env.globals["url_for"] = _jinja_url_for

    # Logger
    logger = logging.getLogger("writebackreminder")

    # Storage and config state
    user_data_dir = app_config.user_data_dir
    recommendation_data_dir = app_config.recommendations_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)
    recommendation_data_dir.mkdir(parents=True, exist_ok=True)

    app.state.APP_CONFIG = app_config
    app.state.USER_DATA_DIR = str(user_data_dir)
    app.state.RECOMMENDATION_DATA_DIR = str(recommendation_data_dir)
    app.state.conversation_store = ConversationStore(user_data_dir, recommendation_data_dir)

    followup_hours = app_config.followup_refresh_hours
    followup_model = app_config.followup_model or FOLLOWUP_DEFAULT_MODEL
    app.state.FOLLOWUP_REFRESH_HOURS = followup_hours
    app.state.FOLLOWUP_REFRESH_INTERVAL_SECONDS = int(followup_hours * 3600) if followup_hours > 0 else 0
    app.state.FOLLOWUP_MODEL = followup_model
    app.state._followup_refresh_lock = asyncio.Lock()
    app.state._followup_last_run: Dict[str, float] = {}
    app.state._followup_in_progress: Dict[str, bool] = {}

    google_client_id, google_client_secret = _load_google_credentials(app_config.google_credentials_path)
    app.state.GOOGLE_CLIENT_ID = google_client_id
    app.state.GOOGLE_CLIENT_SECRET = google_client_secret
    app.state.GOOGLE_CREDENTIALS_PATH = str(app_config.google_credentials_path)

    google_login_enabled = bool(google_client_id and google_client_secret)
    app.state.GOOGLE_LOGIN_ENABLED = google_login_enabled

    async def maybe_refresh_followups(force: bool = False, user_filter: Optional[str] = None) -> None:
        """Refresh stored follow-up recommendations if the interval has elapsed."""

        if not load_api_key():
            return

        interval = app.state.FOLLOWUP_REFRESH_INTERVAL_SECONDS
        key = user_filter or "__global__"

        if not force and interval:
            now = time.monotonic()
            last = app.state._followup_last_run.get(key, 0.0)
            if (now - last) < interval:
                return

        async with app.state._followup_refresh_lock:
            if not force and interval:
                now = time.monotonic()
                last = app.state._followup_last_run.get(key, 0.0)
                if (now - last) < interval:
                    return

            app.state._followup_in_progress[key] = True
            completed = False
            try:
                await _refresh_followups(user_filter=user_filter, force=force)
                completed = True
            finally:
                app.state._followup_in_progress.pop(key, None)
                if completed:
                    app.state._followup_last_run[key] = time.monotonic()

    async def _refresh_followups(user_filter: Optional[str] = None, *, force: bool = False) -> None:
        """Iterate over stored conversations and update follow-up recommendations."""

        model = app.state.FOLLOWUP_MODEL
        refresh_hours = app.state.FOLLOWUP_REFRESH_HOURS
        max_age = timedelta(hours=refresh_hours) if refresh_hours > 0 else None

        store = app.state.conversation_store
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
                    logger.warning(
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
    async def landing(request: Request):
        session = request.session
        if session.get("active_user_email"):
            return RedirectResponse(request.url_for("conversations"))
        return templates.TemplateResponse("login.html", {"request": request, **login_context()})

    @app.get("/login/google")
    async def login_google(request: Request):
        if not google_login_enabled:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, **login_context(errors=("Google login is not configured for this deployment.",))},
                status_code=503,
            )

        state = secrets.token_urlsafe(16)
        request.session["oauth_state"] = state

        redirect_uri = str(request.url_for("auth_google"))
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
        return RedirectResponse(auth_url)

    @app.get("/auth/google")
    async def auth_google(request: Request):
        if not google_login_enabled:
            return RedirectResponse(request.url_for("landing"))

        state = request.query_params.get("state")
        code = request.query_params.get("code")
        saved_state = request.session.pop("oauth_state", None)

        if not code or not state or state != saved_state:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, **login_context(errors=("Invalid Google sign-in response. Please try again.",))},
                status_code=400,
            )

        redirect_uri = str(request.url_for("auth_google"))

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
                return templates.TemplateResponse(
                    "login.html",
                    {"request": request, **login_context(errors=("Failed to contact Google for sign-in.",))},
                    status_code=400,
                )

            if token_response.status_code != 200:
                return templates.TemplateResponse(
                    "login.html",
                    {"request": request, **login_context(errors=("Google sign-in was rejected. Please try again.",))},
                    status_code=400,
                )

            token_payload = token_response.json()
            access_token = token_payload.get("access_token")
            if not access_token:
                return templates.TemplateResponse(
                    "login.html",
                    {"request": request, **login_context(errors=("Google sign-in did not return an access token.",))},
                    status_code=400,
                )

            try:
                userinfo_response = await client.get(
                    GOOGLE_USERINFO_ENDPOINT,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.HTTPError:
                return templates.TemplateResponse(
                    "login.html",
                    {"request": request, **login_context(errors=("Failed to fetch Google account details.",))},
                    status_code=400,
                )

        if userinfo_response.status_code != 200:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, **login_context(errors=("Unable to read Google account details.",))},
                status_code=400,
            )

        user_info = userinfo_response.json()
        email = (user_info.get("email") or "").lower()
        if not email:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, **login_context(errors=("Google account did not return an email address.",))},
                status_code=400,
            )

        request.session["active_user_email"] = email
        request.session["active_user_name"] = (
            (user_info.get("name") or user_info.get("given_name") or "").strip() or email
        )
        return RedirectResponse(request.url_for("conversations"))

    @app.post("/logout")
    async def logout(request: Request):
        request.session.pop("active_user_email", None)
        request.session.pop("active_user_name", None)
        request.session.pop("oauth_state", None)
        return RedirectResponse(request.url_for("landing"), status_code=303)

    @app.get("/conversations")
    async def conversations(request: Request):
        user_email = request.session.get("active_user_email")
        if not user_email:
            return RedirectResponse(request.url_for("landing"))

        await maybe_refresh_followups(user_filter=user_email)

        selected_person = request.query_params.get("person") or None
        known_people = app.state.conversation_store.people_for_user(user_email)
        conversation_entries = (
            app.state.conversation_store.conversations(user_email, selected_person)
            if selected_person
            else ()
        )

        recommendation = (
            app.state.conversation_store.recommendation_for(user_email, selected_person)
            if selected_person
            else None
        )

        refresh_in_progress = bool(app.state._followup_in_progress.get(user_email))

        context: Dict[str, object] = {
            "request": request,
            "active_user_email": user_email,
            "active_user_name": request.session.get("active_user_name"),
            "selected_person": selected_person,
            "known_people": known_people,
            "conversations": conversation_entries,
            "recommendation": recommendation,
            "refresh_in_progress": refresh_in_progress,
            "errors": (),
        }
        return templates.TemplateResponse("index.html", context)

    @app.post("/recommendations/refresh")
    async def refresh_recommendations(request: Request):
        user_email = request.session.get("active_user_email")
        if not user_email:
            return RedirectResponse(request.url_for("landing"), status_code=303)

        await maybe_refresh_followups(force=True, user_filter=user_email)

        referer = request.headers.get("referer")
        base = str(request.base_url)
        if referer and base and referer.startswith(base):
            return RedirectResponse(referer, status_code=303)
        return RedirectResponse(request.url_for("recommendations_page"), status_code=303)

    @app.get("/recommendations")
    async def recommendations_page(request: Request):
        user_email = request.session.get("active_user_email")
        if not user_email:
            return RedirectResponse(request.url_for("landing"))

        await maybe_refresh_followups(user_filter=user_email)

        recommendations = app.state.conversation_store.recommendations_for_user(user_email)
        sorted_recs = sorted(
            recommendations.items(),
            key=lambda item: item[1].urgency,
            reverse=True,
        )

        context = {
            "request": request,
            "active_user_email": user_email,
            "active_user_name": request.session.get("active_user_name"),
            "recommendations": sorted_recs,
            "refresh_in_progress": bool(app.state._followup_in_progress.get(user_email)),
        }
        return templates.TemplateResponse("recommendations.html", context)

    @app.post("/log")
    async def log_conversation(request: Request):
        user_email = request.session.get("active_user_email")
        if not user_email:
            return RedirectResponse(request.url_for("landing"))

        form = await request.form()
        person = (form.get("person") or "").strip()
        entry_type = (form.get("entry_type") or "conversation").strip().lower()
        summary = (form.get("summary") or "").strip()
        errors = []

        if entry_type not in {"conversation", "note"}:
            errors.append("Please choose a valid entry type.")

        if not person:
            errors.append("Please specify who you spoke with.")

        if not summary:
            errors.append("Please add a short summary of the conversation.")

        if errors:
            known_people = app.state.conversation_store.people_for_user(user_email)
            conversation_entries = (
                app.state.conversation_store.conversations(user_email, person)
                if person
                else ()
            )
            context = {
                "request": request,
                "active_user_email": user_email,
                "active_user_name": request.session.get("active_user_name"),
                "selected_person": person or None,
                "known_people": known_people,
                "conversations": conversation_entries,
                "recommendation": app.state.conversation_store.recommendation_for(user_email, person)
                if person
                else None,
                "refresh_in_progress": bool(app.state._followup_in_progress.get(user_email)),
                "new_entry_type": entry_type,
                "draft_summary": summary,
                "errors": tuple(errors),
            }
            return templates.TemplateResponse("index.html", context, status_code=400)

        app.state.conversation_store.add_entry(user_email, person, summary, entry_type)
        dest = str(request.url_for("conversations")) + (f"?person={person}" if person else "")
        return RedirectResponse(dest, status_code=303)

    @app.get("/conversations/edit")
    async def edit_entry(request: Request):
        user_email = request.session.get("active_user_email")
        if not user_email:
            return RedirectResponse(request.url_for("landing"))

        person = request.query_params.get("person") or ""
        entry_id = request.query_params.get("entry") or ""

        entry = (
            app.state.conversation_store.get_entry(user_email, person, entry_id)
            if person and entry_id
            else None
        )

        errors: Tuple[str, ...] = ()
        if not entry:
            errors = ("Conversation entry not found.",)

        context = {
            "request": request,
            "active_user_email": user_email,
            "active_user_name": request.session.get("active_user_name"),
            "person": person,
            "entry": entry,
            # Ensure template fallback works: undefined would be treated as not none
            "draft_summary": None,
            "provided_entry_type": None,
            "errors": errors,
        }
        status = 404 if errors else 200
        return templates.TemplateResponse("edit_entry.html", context, status_code=status)

    @app.post("/conversations/edit")
    async def update_entry(request: Request):
        user_email = request.session.get("active_user_email")
        if not user_email:
            return RedirectResponse(request.url_for("landing"))

        form = await request.form()
        person = (form.get("person") or "").strip()
        entry_id = (form.get("entry_id") or "").strip()
        summary = (form.get("summary") or "").strip()
        entry_type = (form.get("entry_type") or "").strip().lower() or None

        errors = []
        entry = None
        if not person or not entry_id:
            errors.append("Missing conversation reference.")
        else:
            entry = app.state.conversation_store.get_entry(user_email, person, entry_id)
            if not entry:
                errors.append("Conversation entry not found.")

        if entry_type and entry_type not in {"conversation", "note"}:
            errors.append("Please choose a valid entry type.")

        if not summary:
            errors.append("Please provide an updated summary.")

        if errors:
            context = {
                "request": request,
                "active_user_email": user_email,
                "active_user_name": request.session.get("active_user_name"),
                "person": person,
                "entry": entry,
                "provided_entry_type": entry_type or (entry.entry_type if entry else None),
                "draft_summary": summary,
                "errors": tuple(errors),
            }
            status = 404 if any("not found" in error.lower() for error in errors) else 400
            return templates.TemplateResponse("edit_entry.html", context, status_code=status)

        updated = app.state.conversation_store.update_entry(user_email, person, entry_id, summary, entry_type)
        if not updated:
            context = {
                "request": request,
                "active_user_email": user_email,
                "active_user_name": request.session.get("active_user_name"),
                "person": person,
                "entry": entry,
                "provided_entry_type": entry_type or (entry.entry_type if entry else None),
                "draft_summary": summary,
                "errors": ("Unable to update this conversation entry.",),
            }
            return templates.TemplateResponse("edit_entry.html", context, status_code=400)

        dest = str(request.url_for("conversations")) + (f"?person={person}" if person else "")
        return RedirectResponse(dest, status_code=303)

    @app.post("/conversations/delete")
    async def delete_entry(request: Request):
        user_email = request.session.get("active_user_email")
        if not user_email:
            return RedirectResponse(request.url_for("landing"))

        form = await request.form()
        person = (form.get("person") or "").strip()
        entry_id = (form.get("entry_id") or "").strip()

        if person and entry_id:
            removed = app.state.conversation_store.delete_entry(user_email, person, entry_id)
            if removed:
                dest = str(request.url_for("conversations")) + (f"?person={person}" if person else "")
                return RedirectResponse(dest, status_code=303)

        context = {
            "request": request,
            "active_user_email": user_email,
            "active_user_name": request.session.get("active_user_name"),
            "person": person,
            "entry": app.state.conversation_store.get_entry(user_email, person, entry_id)
            if (person and entry_id)
            else None,
            "errors": ("Unable to delete the requested conversation entry.",),
        }
        return templates.TemplateResponse("edit_entry.html", context, status_code=400)

    return app
