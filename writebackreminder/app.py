"""Quart application factory for the WriteBackReminder web UI."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from quart import Quart, redirect, render_template, request, session, url_for

from .datastore import ConversationStore

AVAILABLE_USERS = (
    "alice",
    "bob",
    "carol",
)


def create_app() -> Quart:
    base_dir = Path(__file__).resolve().parent
    template_dir = base_dir.parent / "templates"

    app = Quart(__name__, template_folder=str(template_dir))
    app.config["SECRET_KEY"] = "dev"
    app.conversation_store = ConversationStore()

    @app.get("/")
    async def landing() -> str:
        active_user = session.get("active_user")
        if active_user:
            return redirect(url_for("conversations"))

        context = {
            "available_users": AVAILABLE_USERS,
            "selected_user": None,
            "errors": (),
        }
        return await render_template("login.html", **context)

    @app.post("/login")
    async def login() -> str:
        form = await request.form
        user = (form.get("user") or "").strip()
        errors = []

        if not user:
            errors.append("Please choose a user to continue.")
        elif user not in AVAILABLE_USERS:
            errors.append("Unknown user selected.")

        if errors:
            context = {
                "available_users": AVAILABLE_USERS,
                "selected_user": user or None,
                "errors": tuple(errors),
            }
            return await render_template("login.html", **context), 400

        session["active_user"] = user
        return redirect(url_for("conversations"))

    @app.post("/logout")
    async def logout() -> str:
        session.pop("active_user", None)
        return redirect(url_for("landing"))

    @app.get("/conversations")
    async def conversations() -> str:
        user = session.get("active_user")
        if not user:
            return redirect(url_for("landing"))

        selected_person = request.args.get("person") or None
        known_people = app.conversation_store.people_for_user(user)
        conversation_entries = (
            app.conversation_store.conversations(user, selected_person)
            if selected_person
            else ()
        )

        context: Dict[str, Optional[str]] = {
            "active_user": user,
            "selected_person": selected_person,
            "known_people": known_people,
            "conversations": conversation_entries,
            "errors": (),
        }
        return await render_template("index.html", **context)

    @app.post("/log")
    async def log_conversation() -> str:
        user = session.get("active_user")
        if not user:
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
            known_people = app.conversation_store.people_for_user(user)
            conversation_entries = (
                app.conversation_store.conversations(user, person)
                if person
                else ()
            )
            context = {
                "active_user": user,
                "selected_person": person or None,
                "known_people": known_people,
                "conversations": conversation_entries,
                "errors": tuple(errors),
            }
            return await render_template("index.html", **context), 400

        app.conversation_store.add_entry(user, person, summary)
        return redirect(url_for("conversations", person=person))

    return app
