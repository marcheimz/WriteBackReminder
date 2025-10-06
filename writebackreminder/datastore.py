"""Simple persistent conversation store for the WriteBackReminder app."""
from __future__ import annotations

import base64
import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional

from uuid import uuid4


@dataclass
class ConversationEntry:
    """Single conversation or note captured from the UI."""

    id: str
    entry_type: str
    summary: str
    timestamp: datetime


@dataclass
class RecommendationEntry:
    """AI-generated follow-up suggestion for a conversation partner."""

    proposed_response: str
    urgency: int
    rationale: str
    generated_at: datetime


@dataclass
class _UserData:
    conversations: DefaultDict[str, List[ConversationEntry]] = field(
        default_factory=lambda: defaultdict(list)
    )
    recommendations: Dict[str, RecommendationEntry] = field(default_factory=dict)


class ConversationStore:
    """Tracks conversations and AI recommendations with on-disk persistence."""

    def __init__(self, root_dir: Path, recommendations_dir: Optional[Path] = None) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._recommendation_root = (
            Path(recommendations_dir) if recommendations_dir else (self._root / "recommendations")
        )
        self._recommendation_root.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._data: Dict[str, _UserData] = {}
        self._user_files: Dict[str, Path] = {}
        self._recommendation_files: Dict[str, Path] = {}
        self._load_existing()
        self._load_existing_recommendations()

    def add_entry(self, user: str, person: str, summary: str, entry_type: str) -> ConversationEntry:
        """Append a new conversation summary or note for the given pair and persist it."""
        entry = ConversationEntry(
            id=uuid4().hex,
            entry_type=entry_type,
            summary=summary.strip(),
            timestamp=datetime.now(timezone.utc),
        )
        with self._lock:
            user_data = self._ensure_user(user)
            user_data.conversations[person].append(entry)
            user_data.recommendations.pop(person, None)
            self._persist_user(user)
            self._persist_recommendations(user)
        return entry

    def people_for_user(self, user: str) -> List[str]:
        """Return known conversation partners for the user, sorted alphabetically."""
        with self._lock:
            user_data = self._data.get(user)
            if not user_data:
                return []
            return sorted(user_data.conversations.keys())

    def conversations(self, user: str, person: str) -> Iterable[ConversationEntry]:
        """Iterate over the stored conversation entries for the pair."""
        with self._lock:
            user_data = self._data.get(user)
            if not user_data:
                return ()
            return tuple(user_data.conversations.get(person, ()))

    def all_data(self) -> Dict[str, Dict[str, List[ConversationEntry]]]:
        """Return a shallow copy of the raw data for read-only scenarios."""
        with self._lock:
            result: Dict[str, Dict[str, List[ConversationEntry]]] = {}
            for user, data in self._data.items():
                result[user] = dict(data.conversations)
            return result

    def recommendation_for(self, user: str, person: str) -> Optional[RecommendationEntry]:
        """Return the stored recommendation for the user/person if available."""
        with self._lock:
            user_data = self._data.get(user)
            if not user_data:
                return None
            return user_data.recommendations.get(person)

    def set_recommendation(self, user: str, person: str, recommendation: RecommendationEntry) -> None:
        """Persist a recommendation for the given user/person."""
        with self._lock:
            user_data = self._ensure_user(user)
            user_data.recommendations[person] = recommendation
            self._persist_recommendations(user)

    def recommendations_for_user(self, user: str) -> Dict[str, RecommendationEntry]:
        """Return all recommendations for a user keyed by person."""
        with self._lock:
            user_data = self._data.get(user)
            if not user_data:
                return {}
            return dict(user_data.recommendations)

    def users(self) -> List[str]:
        """List known user identifiers."""
        with self._lock:
            return sorted(self._data.keys())

    def get_entry(self, user: str, person: str, entry_id: str) -> Optional[ConversationEntry]:
        """Return a single conversation entry by ID."""
        with self._lock:
            user_data = self._data.get(user)
            if not user_data:
                return None
            for entry in user_data.conversations.get(person, ()):  # type: ignore[arg-type]
                if entry.id == entry_id:
                    return entry
        return None

    def update_entry(
        self,
        user: str,
        person: str,
        entry_id: str,
        summary: str,
        entry_type: Optional[str] = None,
    ) -> bool:
        """Update the summary (and optionally type) of an existing entry."""
        summary = summary.strip()
        with self._lock:
            user_data = self._data.get(user)
            if not user_data:
                return False
            entries = user_data.conversations.get(person)
            if not entries:
                return False
            for entry in entries:
                if entry.id == entry_id:
                    entry.summary = summary
                    if entry_type:
                        entry.entry_type = entry_type
                    user_data.recommendations.pop(person, None)
                    self._persist_user(user)
                    return True
        return False

    def delete_entry(self, user: str, person: str, entry_id: str) -> bool:
        """Remove an entry from the log."""
        with self._lock:
            user_data = self._data.get(user)
            if not user_data:
                return False
            entries = user_data.conversations.get(person)
            if not entries:
                return False
            for index, entry in enumerate(entries):
                if entry.id == entry_id:
                    entries.pop(index)
                    if not entries:
                        user_data.conversations.pop(person, None)
                        user_data.recommendations.pop(person, None)
                    else:
                        user_data.recommendations.pop(person, None)
                    self._persist_user(user)
                    return True
        return False

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

            user_data = self._ensure_user(user)
            user_bucket = user_data.conversations
            for person, entries in conversations.items():
                if not isinstance(person, str) or not isinstance(entries, list):
                    continue

                person_bucket = user_bucket[person]
                dirty = False
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    summary = entry.get("summary")
                    timestamp_raw = entry.get("timestamp")
                    entry_id = entry.get("id")
                    if not isinstance(summary, str) or not isinstance(timestamp_raw, str):
                        continue
                    try:
                        timestamp = datetime.fromisoformat(timestamp_raw)
                    except ValueError:
                        continue
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    if not isinstance(entry_id, str) or not entry_id:
                        entry_id = uuid4().hex
                        dirty = True
                    entry_type = entry.get("entry_type")
                    if not isinstance(entry_type, str) or entry_type not in {"conversation", "note"}:
                        entry_type = "conversation"
                        dirty = True
                    person_bucket.append(
                        ConversationEntry(
                            id=entry_id,
                            entry_type=entry_type,
                            summary=summary,
                            timestamp=timestamp,
                        )
                    )

                if dirty:
                    self._persist_user(user)

            self._user_files[user] = path

            # Backward compatibility: migrate recommendations stored alongside conversations.
            migrated_recommendations = False
            recommendations = payload.get("recommendations", {})
            if isinstance(recommendations, dict):
                for person, rec in recommendations.items():
                    entry = self._parse_recommendation(rec)
                    if entry:
                        user_data.recommendations[person] = entry
                        migrated_recommendations = True

            if migrated_recommendations:
                self._persist_user(user)

        # Ensure migrated recommendations are written to the dedicated directory.
        for user, data in self._data.items():
            if data.recommendations:
                self._persist_recommendations(user)

    def _persist_user(self, user: str) -> None:
        """Write a user's conversations to disk."""
        data = self._data.get(user)
        conversations = data.conversations if data else {}
        serializable = {
            "user": user,
            "conversations": {
                person: [
                    {
                        "id": entry.id,
                        "entry_type": entry.entry_type,
                        "summary": entry.summary,
                        "timestamp": entry.timestamp.isoformat(),
                    }
                    for entry in entries
                ]
                for person, entries in conversations.items()
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

    def _ensure_user(self, user: str) -> _UserData:
        user_data = self._data.get(user)
        if not user_data:
            user_data = _UserData()
            self._data[user] = user_data
        return user_data

    def _load_existing_recommendations(self) -> None:
        """Load recommendation data stored in the dedicated directory."""
        for path in sorted(self._recommendation_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            user = payload.get("user")
            if not isinstance(user, str) or not user:
                continue

            user_data = self._ensure_user(user)

            recommendations = payload.get("recommendations", {})
            if not isinstance(recommendations, dict):
                continue

            for person, rec in recommendations.items():
                entry = self._parse_recommendation(rec)
                if entry:
                    user_data.recommendations[person] = entry

            self._recommendation_files[user] = path

    def _persist_recommendations(self, user: str) -> None:
        """Write recommendation data for a user to the dedicated directory."""
        data = self._data.get(user)
        recommendations = data.recommendations if data else {}
        if not recommendations:
            path = self._recommendation_files.get(user)
            if path and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
            self._recommendation_files.pop(user, None)
            return

        serializable = {
            "user": user,
            "recommendations": {
                person: {
                    "proposed_response": rec.proposed_response,
                    "urgency": rec.urgency,
                    "rationale": rec.rationale,
                    "generated_at": rec.generated_at.isoformat(),
                }
                for person, rec in recommendations.items()
            },
        }

        path = self._path_for_recommendations(user)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _path_for_recommendations(self, user: str) -> Path:
        existing = self._recommendation_files.get(user)
        if existing:
            return existing

        token = base64.urlsafe_b64encode(user.encode("utf-8")).decode("ascii").rstrip("=")
        path = self._recommendation_root / f"{token}.json"
        self._recommendation_files[user] = path
        return path

    def _parse_recommendation(self, payload: object) -> Optional[RecommendationEntry]:
        if not isinstance(payload, dict):
            return None

        proposed = payload.get("proposed_response")
        urgency = payload.get("urgency")
        rationale = payload.get("rationale")
        generated_raw = payload.get("generated_at")

        if (
            not isinstance(proposed, str)
            or not isinstance(rationale, str)
            or not isinstance(urgency, int)
            or not isinstance(generated_raw, str)
        ):
            return None

        try:
            generated_at = datetime.fromisoformat(generated_raw)
        except ValueError:
            return None

        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)

        return RecommendationEntry(
            proposed_response=proposed,
            urgency=urgency,
            rationale=rationale,
            generated_at=generated_at,
        )
