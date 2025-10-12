#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple


def load_configs(config_path: Path, force_use_s3: bool | None = None) -> Dict[str, str]:
    env: Dict[str, str] = {}

    # Base app config (legacy JSON)
    if config_path.is_file():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"warning: ignoring invalid JSON in {config_path}: {exc}", file=sys.stderr)
            payload = {}
        # Map fields -> env vars
        if v := payload.get("secret_key"):
            env["SECRET_KEY"] = str(v)
        if v := payload.get("openai_api_key"):
            env["OPENAI_API_KEY"] = str(v)
        if v := payload.get("followup_refresh_hours"):
            env["FOLLOWUP_REFRESH_HOURS"] = str(v)
        if v := payload.get("followup_model"):
            env["FOLLOWUP_MODEL"] = str(v)
        if v := payload.get("user_data_dir"):
            env["USER_DATA_DIR"] = str(v)
        if v := payload.get("recommendations_dir"):
            env["RECOMMENDATIONS_DIR"] = str(v)
        if "use_s3" in payload:
            raw = payload["use_s3"]
            if isinstance(raw, bool):
                env["USE_S3"] = "true" if raw else "false"
            else:
                env["USE_S3"] = str(raw)
        if v := payload.get("aws_access_key_id"):
            env["AWS_ACCESS_KEY_ID"] = str(v)
        if v := payload.get("aws_secret_access_key"):
            env["AWS_SECRET_ACCESS_KEY"] = str(v)
        if v := payload.get("aws_endpoint_url_s3"):
            env["AWS_ENDPOINT_URL_S3"] = str(v)
        if v := payload.get("aws_region"):
            env["AWS_REGION"] = str(v)
        if v := payload.get("bucket_name"):
            env["BUCKET_NAME"] = str(v)
        if v := payload.get("client_id"):
            env["GOOGLE_CLIENT_ID"] = str(v)
        if v := payload.get("client_secret"):
            env["GOOGLE_CLIENT_SECRET"] = str(v)

    # Pass through existing S3/Tigris env if present (so you can set USE_S3 etc.)
    for key in (
        "USE_S3",
        "BUCKET_NAME",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ENDPOINT_URL_S3",
        "AWS_REGION",
        "S3_PREFIX",
    ):
        if key in os.environ and os.environ[key]:
            env[key] = os.environ[key]

    # Decide USE_S3 if not explicitly set:
    # - honor --use-s3 flag when provided
    # - otherwise, enable if Tigris/S3 values appear present
    if "USE_S3" not in env:
        if force_use_s3 is True:
            env["USE_S3"] = "true"
        elif force_use_s3 is False:
            env["USE_S3"] = "false"
        else:
            if env.get("BUCKET_NAME") and env.get("AWS_ENDPOINT_URL_S3"):
                env["USE_S3"] = "true"

    return env


def export_commands(env: Dict[str, str]) -> str:
    # Emit sh-compatible export lines
    lines = []
    for k, v in env.items():
        # Use single quotes and escape existing single quotes
        vv = v.replace("'", "'\\''")
        lines.append(f"export {k}='{vv}'")
    return "\n".join(lines) + ("\n" if lines else "")


def write_dotenv(path: Path, env: Dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for k, v in env.items():
            vv = v.replace("\n", "\\n")
            f.write(f"{k}={vv}\n")


def set_fly_secrets(env: Dict[str, str], app: str | None, dry_run: bool) -> Tuple[int, str]:
    # Build a single 'fly secrets set' with KEY=VALUE args
    cmd = ["fly", "secrets", "set"]
    for k, v in env.items():
        cmd.append(f"{k}={v}")
    if app:
        cmd.extend(["--app", app])

    if dry_run:
        return 0, "(dry-run) " + " ".join(shlex.quote(c) for c in cmd)

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.returncode, proc.stdout


def main() -> int:
    p = argparse.ArgumentParser(description="Configure env vars locally or on Fly from existing JSON configs.")
    p.add_argument("--from-json", default="secrets/config.json", help="Path to legacy config.json (default: secrets/config.json)")
    sub = p.add_subparsers(dest="mode", required=True)

    p_local = sub.add_parser("local", help="Emit local environment setup")
    p_local.add_argument("--dotenv", metavar="PATH", help="Write a .env file instead of printing export commands")

    p_fly = sub.add_parser("fly", help="Set Fly secrets from the loaded values")
    p_fly.add_argument("--app", help="Fly app name (defaults to value in fly.toml if omitted)", default=None)
    p_fly.add_argument("--dry-run", action="store_true", help="Print the fly command without executing it")

    # Global optional switch to force USE_S3 on/off
    p.add_argument("--use-s3", dest="use_s3", choices=["true", "false"], help="Force setting USE_S3 in output")

    args = p.parse_args()
    config_path = Path(args.from_json)

    force_use_s3 = None
    if args.use_s3 == "true":
        force_use_s3 = True
    elif args.use_s3 == "false":
        force_use_s3 = False
    env = load_configs(config_path, force_use_s3)
    if not env:
        print("No values discovered to set.")
        return 0

    if args.mode == "local":
        if args.dotenv:
            write_dotenv(Path(args.dotenv), env)
            print(f"Wrote {args.dotenv} with {len(env)} variables.")
        else:
            sys.stdout.write(export_commands(env))
        return 0

    if args.mode == "fly":
        app = args.app
        if app is None:
            # Try to read from fly.toml if present
            t = Path("fly.toml")
            if t.is_file():
                try:
                    for line in t.read_text(encoding="utf-8").splitlines():
                        if line.strip().startswith("app = "):
                            app = line.split("=", 1)[1].strip().strip("'\"")
                            break
                except Exception:
                    pass
        code, out = set_fly_secrets(env, app, args.dry_run)
        print(out)
        return code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
