#!/usr/bin/env python3
"""
publish_static_release.py
=========================
Publish an immutable frontend release to the static CDN
(https://static.sonya.group/releases/<release>/<file>).

  * Files are taken from git at exactly <release> (`git show <release>:<path>`),
    never from the working tree -- a release id always means one content.
  * Never overwrites: if ANY target key already exists, nothing is uploaded.
  * After upload, every public CDN URL must answer 200 with the same MD5.

The bucket behind static.sonya.group is not recorded in the repository, so it
is a required argument. Credentials/endpoint come from the server's own env
(S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_REGION).

Usage (from the repo root, e.g. /opt/sonya with its .env loaded):
    python deploy/publish_static_release.py --bucket <cdn-bucket> --release 537dbe6 --dry-run
    python deploy/publish_static_release.py --bucket <cdn-bucket> --release 537dbe6
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
import urllib.request

# Assets index.html loads from the CDN (see the releases/<id>/ links in it).
DEFAULT_FILES = ["auth.js", "app.js", "styles.css", "config.js"]
CONTENT_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
CDN_BASE = "https://static.sonya.group"


def git_blob(release: str, path: str) -> bytes:
    return subprocess.run(["git", "show", f"{release}:{path}"], check=True, capture_output=True).stdout


def s3_client():
    import boto3  # backend dependency (requirements-backend.txt)
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("S3_REGION", "ru-1"),
    )


def key_exists(s3, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def fetch_md5(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, hashlib.md5(resp.read()).hexdigest()
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--release", required=True, help="git commit (short hash) the assets are taken from")
    ap.add_argument("--files", nargs="+", default=DEFAULT_FILES)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    release = subprocess.run(["git", "rev-parse", "--short=7", args.release], check=True,
                             capture_output=True, text=True).stdout.strip()
    blobs = {f: git_blob(release, f) for f in args.files}
    keys = {f: f"releases/{release}/{f}" for f in args.files}
    for f, body in blobs.items():
        print(f"[release {release}] {keys[f]}  {len(body)} B  md5={hashlib.md5(body).hexdigest()}")

    if args.dry_run:
        print("dry-run: nothing uploaded")
        return 0

    s3 = s3_client()
    existing = [k for k in keys.values() if key_exists(s3, args.bucket, k)]
    if existing:
        print(f"REFUSING: release keys already exist (immutable releases are never overwritten): {existing}",
              file=sys.stderr)
        return 2

    for f, body in blobs.items():
        s3.put_object(
            Bucket=args.bucket, Key=keys[f], Body=body, ACL="public-read",
            ContentType=CONTENT_TYPES.get(os.path.splitext(f)[1], "application/octet-stream"),
            CacheControl="public, max-age=31536000, immutable",
        )
        print(f"uploaded {keys[f]}")

    ok = True
    for f, body in blobs.items():
        url = f"{CDN_BASE}/{keys[f]}"
        for _attempt in range(10):
            status, md5 = fetch_md5(url)
            if status == 200:
                break
            time.sleep(3)
        match = md5 == hashlib.md5(body).hexdigest()
        print(f"{status} {'md5-ok' if match else 'MD5-MISMATCH'} {url}")
        ok = ok and status == 200 and match
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
