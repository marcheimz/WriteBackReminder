"""Configuration loader for WriteBackReminder."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "secrets" / "config.json"


@dataclass(frozen=True)
class AppConfig:
    """Application-wide configuration values."""

    secret_key: str
    google_credentials_path: Path
    user_data_dir: Path
    recommendations_dir: Path
    followup_refresh_hours: float
    followup_model: str
    openai_api_key: Optional[str]


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@lru_cache()
def get_config(config_path: Optional[str | Path] = None) -> AppConfig:
    """Load and cache configuration from JSON."""

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found at {path}. Create secrets/config.json (see config.example.json)."
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in configuration file {path}: {exc}") from exc

    secret_key = str(payload.get("secret_key", "dev"))

    google_credentials_raw = payload.get("google_credentials_path", "secrets/google_oauth.json")
    user_data_raw = payload.get("user_data_dir", "userdata")
    recommendations_raw = payload.get("recommendations_dir", "userdata/recommendations")

    followup_refresh_hours = float(payload.get("followup_refresh_hours", 24))
    followup_model = str(payload.get("followup_model", "gpt-4o-2024-08-06"))
    openai_api_key = payload.get("openai_api_key")
    if isinstance(openai_api_key, str):
        openai_api_key = openai_api_key.strip() or None
    else:
        openai_api_key = None

    return AppConfig(
        secret_key=secret_key,
        google_credentials_path=_resolve_path(google_credentials_raw),
        user_data_dir=_resolve_path(user_data_raw),
        recommendations_dir=_resolve_path(recommendations_raw),
        followup_refresh_hours=max(followup_refresh_hours, 0.0),
        followup_model=followup_model,
        openai_api_key=openai_api_key,
    )


def reload_config(config_path: Optional[str | Path] = None) -> AppConfig:
    """Clear the cache and reload configuration (useful for tests)."""

    get_config.cache_clear()  # type: ignore[attr-defined]
    return get_config(config_path)
