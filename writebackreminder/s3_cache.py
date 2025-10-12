from __future__ import annotations

import os
import logging
import base64
from pathlib import Path
from functools import lru_cache
from typing import Dict, Optional

try:
    import boto3  # type: ignore
    from botocore.exceptions import ClientError  # type: ignore
except Exception:  # pragma: no cover - boto3 not installed in some envs
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore


logger = logging.getLogger(__name__)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache()
def enabled() -> bool:
    if not _bool_env("USE_S3", False):
        logger.debug("S3 cache disabled: USE_S3 is not truthy.")
        return False

    required_keys = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3", "BUCKET_NAME")
    issues: Dict[str, str] = {}
    placeholder_tokens = ("changeme", "replace", "example", "dummy", "sample", "todo", "your-", "your_", "....")

    for key in required_keys:
        raw = os.getenv(key)
        if raw is None or not raw.strip():
            issues[key] = "missing value"
            continue

        cleaned = raw.strip()
        lowered = cleaned.lower()
        if key == "AWS_ENDPOINT_URL_S3":
            if "://" not in cleaned:
                issues[key] = "endpoint URL must include a scheme like https://"
            continue

        if any(token in lowered for token in placeholder_tokens):
            # Avoid logging the full secret; include only a short prefix.
            preview = cleaned[:6] + "…" if len(cleaned) > 6 else cleaned
            issues[key] = f"value looks like a placeholder (starts with '{preview}')"

    if issues:
        for key, message in issues.items():
            logger.warning("S3 cache disabled: %s (%s)", message, key)
        return False

    logger.info("S3 cache enabled with bucket %s", os.getenv("BUCKET_NAME"))
    return True


@lru_cache()
def bucket_name() -> Optional[str]:
    return os.getenv("BUCKET_NAME") if enabled() else None


@lru_cache()
def prefix() -> str:
    return (os.getenv("S3_PREFIX") or "writebackreminder").strip().strip("/")


@lru_cache()
def client():  # type: ignore[override]
    if not enabled():
        raise RuntimeError("S3 cache is not enabled")
    assert boto3 is not None, "boto3 must be installed to use S3 cache"
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("AWS_ENDPOINT_URL_S3"),
        region_name=os.getenv("AWS_REGION") or "auto",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


def _token_for_user(user: str) -> str:
    return base64.urlsafe_b64encode(user.encode("utf-8")).decode("ascii").rstrip("=")


def key_for_conversations(user: str) -> str:
    return f"{prefix()}/conversations/{_token_for_user(user)}.json"


def key_for_recommendations(user: str) -> str:
    return f"{prefix()}/recommendations/{_token_for_user(user)}.json"


def download_if_exists(key: str, dest_path: Path) -> bool:
    if not enabled():
        return False
    b = bucket_name()
    if not b:
        return False
    try:
        client().head_object(Bucket=b, Key=key)
    except ClientError as exc:  # type: ignore[reportPrivateUsage]
        code = getattr(getattr(exc, "response", {}), "get", lambda *_: None)("Error", {}).get("Code") if hasattr(exc, "response") else None
        if code in ("404", "NotFound"):
            return False
        logger.debug("S3 HEAD failed for key=%s: %s", key, exc)
        return False
    try:
        body = client().get_object(Bucket=b, Key=key)["Body"]  # type: ignore[index]
        data = body.read()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to download S3 object %s: %s", key, exc)
        return False


def upload_file(key: str, src_path: Path) -> None:
    if not enabled() or not src_path.exists():
        return
    b = bucket_name()
    if not b:
        return
    try:
        client().put_object(Bucket=b, Key=key, Body=src_path.read_bytes(), ContentType="application/json")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to upload S3 object %s: %s", key, exc)
