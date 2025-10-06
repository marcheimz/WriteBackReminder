"""Generate follow-up suggestions for stored conversations using the OpenAI API."""
from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from writebackreminder.ai_client import DEFAULT_MODEL, FollowupRecommendation, generate_followup, load_api_key
from writebackreminder.config import get_config


def _user_file(email: str) -> Path:
    config = get_config()
    token = base64.urlsafe_b64encode(email.encode("utf-8")).decode("ascii").rstrip("=")
    return config.user_data_dir / f"{token}.json"


def _load_history(email: str, person: str) -> List[Tuple[datetime, str]]:
    path = _user_file(email)
    if not path.is_file():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    conversations = payload.get("conversations", {})
    entries = conversations.get(person, []) if isinstance(conversations, dict) else []
    history: List[Tuple[datetime, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        summary = entry.get("summary")
        timestamp_raw = entry.get("timestamp")
        if not isinstance(summary, str) or not isinstance(timestamp_raw, str):
            continue
        try:
            timestamp = datetime.fromisoformat(timestamp_raw)
        except ValueError:
            continue
        history.append((timestamp, summary))
    return history


def request_recommendation(email: str, person: str, model: str) -> FollowupRecommendation:
    history = _load_history(email, person)
    return generate_followup(email, person, history, model=model)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="Google account email recorded in the app")
    parser.add_argument("person", help="Conversation partner to generate a follow-up for")
    parser.add_argument(
        "--model",
        default=None,
        help="OpenAI model to use (defaults to the value in secrets/config.json)",
    )
    args = parser.parse_args()

    if not load_api_key():
        raise SystemExit("No OpenAI API key configured in secrets/config.json.")

    model = args.model or get_config().followup_model or DEFAULT_MODEL
    recommendation = request_recommendation(args.email, args.person, model)
    print(recommendation.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
