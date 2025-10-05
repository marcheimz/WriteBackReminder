"""Simple in-memory conversation store for the WriteBackReminder app."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import DefaultDict, Dict, Iterable, List


@dataclass
class ConversationEntry:
    """Single conversation summary captured from the UI."""

    summary: str
    timestamp: datetime


class ConversationStore:
    """Tracks conversations per (user, person) tuple in-memory."""

    def __init__(self) -> None:
        self._data: DefaultDict[str, DefaultDict[str, List[ConversationEntry]]] = defaultdict(
            lambda: defaultdict(list)
        )

    def add_entry(self, user: str, person: str, summary: str) -> ConversationEntry:
        """Append a new conversation summary for the given pair."""
        entry = ConversationEntry(summary=summary.strip(), timestamp=datetime.now(timezone.utc))
        self._data[user][person].append(entry)
        return entry

    def people_for_user(self, user: str) -> List[str]:
        """Return known conversation partners for the user, sorted alphabetically."""
        return sorted(self._data.get(user, {}).keys())

    def conversations(self, user: str, person: str) -> Iterable[ConversationEntry]:
        """Iterate over the stored conversation entries for the pair."""
        return tuple(self._data.get(user, {}).get(person, ()))

    def all_data(self) -> Dict[str, Dict[str, List[ConversationEntry]]]:
        """Return a shallow copy of the raw data for read-only scenarios."""
        return {user: dict(persons) for user, persons in self._data.items()}
