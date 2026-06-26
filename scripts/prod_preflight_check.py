"""
prod_preflight_check.py
=======================
Pre-flight environment check before starting the API or worker.
Exits with code 1 if any required env var is missing.
"""
from __future__ import annotations

import os
import sys

REQUIRED_BACKEND = [
    "DATABASE_URL",
    "S3_BUCKET",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "WORKER_SECRET",
]

REQUIRED_WORKER = [
    "S3_BUCKET",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "MODELS_S3_BUCKET",
]


def check(var_list: list[str], label: str) -> bool:
    missing = [v for v in var_list if not os.environ.get(v)]
    if missing:
        print(f"[PREFLIGHT] {label} — MISSING env vars: {missing}", file=sys.stderr)
        return False
    print(f"[PREFLIGHT] {label} — OK")
    return True


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "backend"
    ok = True
    if mode == "backend":
        ok = check(REQUIRED_BACKEND, "backend")
    elif mode == "worker":
        ok = check(REQUIRED_WORKER, "worker")
    else:
        ok = check(REQUIRED_BACKEND, "backend") and check(REQUIRED_WORKER, "worker")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
