"""Configuration loader for WriteBackReminder (env-only).

Reads configuration exclusively from environment variables:

- SECRET_KEY
- OPENAI_API_KEY
- FOLLOWUP_REFRESH_HOURS
- FOLLOWUP_MODEL
- USER_DATA_DIR
- RECOMMENDATIONS_DIR
- GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (read directly by the app)
- GOOGLE_CREDENTIALS_PATH (unused by the app; retained for compatibility)
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
    use_s3: bool


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@lru_cache()
def get_config(config_path: Optional[str | Path] = None) -> AppConfig:
    """Load and cache configuration from environment variables only."""

    secret_key = os.getenv("SECRET_KEY") or "dev"

    google_credentials_raw = os.getenv("GOOGLE_CREDENTIALS_PATH") or "secrets/google_oauth.json"
    user_data_raw = os.getenv("USER_DATA_DIR") or "userdata"
    recommendations_raw = os.getenv("RECOMMENDATIONS_DIR") or "userdata/recommendations"

    followup_refresh_env = os.getenv("FOLLOWUP_REFRESH_HOURS")
    followup_refresh_hours = float(followup_refresh_env) if followup_refresh_env is not None else 24.0
    followup_model = os.getenv("FOLLOWUP_MODEL") or "gpt-4o-2024-08-06"
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if isinstance(openai_api_key, str):
        openai_api_key = openai_api_key.strip() or None
    else:
        openai_api_key = None

    use_s3_raw = os.getenv("USE_S3", "").strip().lower()
    use_s3 = use_s3_raw in {"1", "true", "yes", "on"}

    return AppConfig(
        secret_key=secret_key,
        google_credentials_path=_resolve_path(google_credentials_raw),
        user_data_dir=_resolve_path(user_data_raw),
        recommendations_dir=_resolve_path(recommendations_raw),
        followup_refresh_hours=max(followup_refresh_hours, 0.0),
        followup_model=followup_model,
        openai_api_key=openai_api_key,
        use_s3=use_s3,
    )


def reload_config(config_path: Optional[str | Path] = None) -> AppConfig:
    """Clear the cache and reload configuration (useful for tests)."""

    get_config.cache_clear()  # type: ignore[attr-defined]
    return get_config(config_path)
