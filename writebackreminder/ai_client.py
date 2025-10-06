"""Helpers for generating AI follow-up recommendations."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence, Tuple

from openai import OpenAI
from pydantic import BaseModel, Field

from .config import get_config


DEFAULT_MODEL = "gpt-4o-2024-08-06"
HistoryItem = Tuple[datetime, str, str]


class FollowupRecommendation(BaseModel):
    """Structured response produced by the language model."""

    person: str
    proposed_response: str
    urgency: int = Field(ge=1, le=10)
    rationale: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def load_api_key() -> Optional[str]:
    return get_config().openai_api_key


def generate_followup(
    email: str,
    person: str,
    history: Iterable[HistoryItem],
    *,
    model: Optional[str] = None,
    current_time: Optional[datetime] = None,
) -> FollowupRecommendation:
    """Call the OpenAI Responses API to craft a follow-up suggestion."""

    config = get_config()
    api_key = config.openai_api_key
    if not api_key:
        raise RuntimeError("Missing OpenAI API key in config (openai_api_key).")

    now = current_time or datetime.now(timezone.utc)
    model_name = model or config.followup_model or DEFAULT_MODEL
    client = OpenAI(api_key=api_key)

    history_lines = _format_history(history)
    if history_lines:
        history_text = "\n".join(history_lines)
    else:
        history_text = "No previous conversation summaries are available."

    response = client.responses.parse(
        model=model_name,
        input=[
            {
                "role": "system",
                "content": (
                    "You craft concise follow-up messages and assess urgency. Consider the time difference, too early responses might not be a good idea, while a friendly follow up after no response after a while can make sense. Do not consider the timezone at all, this is left to the user."
                    " Return JSON matching the provided schema."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Current UTC time: {now.isoformat()}\n"
                    f"User email: {email}\n"
                    f"Target person: {person}\n"
                    "Conversation history (most recent last):\n"
                    f"{history_text}\n\n"
                    "Produce a short proposed response to the person,"
                    " an urgency score (1-10, where 10 is most urgent),"
                    " and a brief rationale."
                ),
            },
        ],
        text_format=FollowupRecommendation,
    )

    recommendation = response.output_parsed
    recommendation.generated_at = now
    return recommendation


def _format_history(history: Iterable[HistoryItem]) -> Sequence[str]:
    lines = []
    for timestamp, entry_type, summary in history:
        if not summary:
            continue
        if isinstance(timestamp, datetime):
            ts = timestamp
        else:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        label = "NOTE" if entry_type == "note" else "CONVERSATION"
        lines.append(f"{ts.isoformat()} [{label}] {summary.strip()}")
    return lines
