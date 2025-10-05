"""Simple persistent conversation store for the WriteBackReminder app."""
from __future__ import annotations

import base64
import json
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List


@dataclass
class ConversationEntry:
    """Single conversation summary captured from the UI."""

    summary: str
    timestamp: datetime


class ConversationStore:
    """Tracks conversations per (user, person) tuple with on-disk persistence."""

    def __init__(self, root_dir: Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: DefaultDict[str, DefaultDict[str, List[ConversationEntry]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._user_files: Dict[str, Path] = {}
        self._load_existing()

    def add_entry(self, user: str, person: str, summary: str) -> ConversationEntry:
        """Append a new conversation summary for the given pair and persist the change."""
        entry = ConversationEntry(summary=summary.strip(), timestamp=datetime.now(timezone.utc))
        with self._lock:
            bucket = self._data[user]
            bucket[person].append(entry)
            self._persist_user(user)
        return entry

    def people_for_user(self, user: str) -> List[str]:
        """Return known conversation partners for the user, sorted alphabetically."""
        with self._lock:
            return sorted(self._data.get(user, {}).keys())

    def conversations(self, user: str, person: str) -> Iterable[ConversationEntry]:
        """Iterate over the stored conversation entries for the pair."""
        with self._lock:
            return tuple(self._data.get(user, {}).get(person, ()))

    def all_data(self) -> Dict[str, Dict[str, List[ConversationEntry]]]:
        """Return a shallow copy of the raw data for read-only scenarios."""
        with self._lock:
            return {user: dict(persons) for user, persons in self._data.items()}

    def _load_existing(self) -> None:
        """Populate in-memory state from any persisted files."""
        for path in sorted(self._root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            user = payload.get("user")
            if not isinstance(user, str) or not user:
                continue

            conversations = payload.get("conversations", {})
            if not isinstance(conversations, dict):
                continue

            user_bucket = self._data[user]
            for person, entries in conversations.items():
                if not isinstance(person, str) or not isinstance(entries, list):
                    continue

                person_bucket = user_bucket[person]
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
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    person_bucket.append(ConversationEntry(summary=summary, timestamp=timestamp))

            self._user_files[user] = path

    def _persist_user(self, user: str) -> None:
        """Write a user's conversations to disk."""
        data = self._data.get(user, {})
        serializable = {
            "user": user,
            "conversations": {
                person: [
                    {"summary": entry.summary, "timestamp": entry.timestamp.isoformat()}
                    for entry in entries
                ]
                for person, entries in data.items()
            },
        }

        path = self._path_for_user(user)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _path_for_user(self, user: str) -> Path:
        existing = self._user_files.get(user)
        if existing:
            return existing

        token = base64.urlsafe_b64encode(user.encode("utf-8")).decode("ascii").rstrip("=")
        path = self._root / f"{token}.json"
        self._user_files[user] = path
        return path
