"""
upload_models_to_s3.py
======================
One-time utility: upload local model weights to the S3 models bucket.

Always uses S3_ENDPOINT_URL (required for Timeweb / compatible S3).
Bucket: MODELS_S3_BUCKET → S3_BUCKET_NAME → S3_BUCKET (fallback chain).

Usage:
    python scripts/upload_models_to_s3.py --model trailer/best.pt --local /path/to/best.pt
    python scripts/upload_models_to_s3.py --model common/yolo11n.pt --local /path/to/yolo11n.pt
    python scripts/upload_models_to_s3.py --model common/yolo11n-pose.pt --local /path/to/yolo11n-pose.pt
    python scripts/upload_models_to_s3.py --model common/webcam_detector.pt --local /path/to/webcam_detector.pt
    # Optional (only if LIP_SYNC_DETECTION_ENABLED=true):
    python scripts/upload_models_to_s3.py --model common/face_landmarker.task --local /path/to/face_landmarker.task

S3 keys (in MODELS_S3_BUCKET):
    models/trailer/best.pt
    models/common/yolo11n.pt
    models/common/yolo11n-pose.pt
    models/common/webcam_detector.pt
    models/common/face_landmarker.task
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    import boto3
    import botocore
except ImportError:
    raise SystemExit("boto3 not installed — run: pip install boto3")

# Alias → S3 key mapping (relative alias → full S3 key)
_MODEL_MAP: dict[str, str] = {
    "trailer/best.pt":             "models/trailer/best.pt",
    "common/yolo11n.pt":           "models/common/yolo11n.pt",
    "common/yolo11n-pose.pt":      "models/common/yolo11n-pose.pt",
    "common/webcam_detector.pt":   "models/common/webcam_detector.pt",
    "common/face_landmarker.task": "models/common/face_landmarker.task",
}


def _get_bucket() -> str:
    b = (
        os.environ.get("MODELS_S3_BUCKET")
        or os.environ.get("S3_BUCKET_NAME")
        or os.environ.get("S3_BUCKET", "")
    )
    if not b:
        raise SystemExit("MODELS_S3_BUCKET (or S3_BUCKET_NAME) env var not set")
    return b


def _make_client():
    """S3 client with mandatory endpoint_url for Timeweb / compatible S3."""
    endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
    if not endpoint:
        raise SystemExit(
            "S3_ENDPOINT_URL is required. "
            "Example: S3_ENDPOINT_URL=https://s3.timeweb.cloud"
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("S3_REGION", "ru-1"),
    )


def upload_one(local: Path, s3_key: str) -> None:
    if not local.exists():
        raise SystemExit(f"Local file not found: {local}")
    bucket = _get_bucket()
    s3     = _make_client()
    size   = local.stat().st_size
    logger.info("Uploading %s (%d bytes) → s3://%s/%s", local, size, bucket, s3_key)
    s3.upload_file(str(local), bucket, s3_key)
    logger.info("Done: s3://%s/%s", bucket, s3_key)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload SONYA model weights to S3 models bucket"
    )
    parser.add_argument("--model", required=True,
                        help="Model alias, e.g. trailer/best.pt or common/yolo11n.pt")
    parser.add_argument("--local", required=True,
                        help="Local file path to upload")
    parser.add_argument("--s3-key", default=None,
                        help="Override S3 key (default: from --model alias map)")
    args = parser.parse_args()

    local  = Path(args.local)
    s3_key = args.s3_key or _MODEL_MAP.get(args.model, f"models/{args.model}")
    upload_one(local, s3_key)


if __name__ == "__main__":
    main()
