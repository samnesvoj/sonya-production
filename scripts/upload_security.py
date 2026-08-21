"""
upload_security.py
==================
Upload validation for SONYA production API.

Validates:
  - File size limits
  - MIME type / magic bytes (not just extension)
  - Filename sanitization
  - No double extensions (.php.mp4 etc.)
  - Content-Type consistency
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, UploadFile, status

from scripts.security import new_trace_id

logger = logging.getLogger(__name__)

# ── Limits ——————————————————————————————————————————————————————————————————————

MAX_FILE_SIZE_BYTES = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "2048")) * 1024 * 1024  # default 2 GB

ALLOWED_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
    "video/webm",
    "video/mpeg",
    "video/3gpp",
}

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg", ".mpg", ".3gp"}

# Magic bytes for video formats
_MAGIC: list[tuple[bytes, str]] = [
    (b"\x00\x00\x00\x18ftyp", "mp4"),
    (b"\x00\x00\x00\x1cftyp", "mp4"),
    (b"\x00\x00\x00\x20ftyp", "mp4"),
    (b"ftyp", "mp4"),
    (b"\x1aE\xdf\xa3", "mkv/webm"),
    (b"RIFF", "avi"),
    (b"\x00\x00\x01\xba", "mpeg"),
    (b"\x00\x00\x01\xb3", "mpeg"),
]

_FORBIDDEN_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DOUBLE_EXT = re.compile(r'\.(php|asp|aspx|jsp|cgi|sh|py|rb|exe|bat|cmd)\.[a-z0-9]+$', re.IGNORECASE)


def _safe_filename(name: str) -> str:
    """Sanitize filename: strip path separators, control chars, double extensions."""
    name = Path(name).name
    name = _FORBIDDEN_FILENAME.sub("_", name)
    name = name.strip(". ")
    if not name:
        name = "upload.mp4"
    return name[:200]


def _detect_magic(header: bytes) -> Optional[str]:
    """Return format hint if magic bytes match a known video format."""
    for magic, fmt in _MAGIC:
        if header[: len(magic)] == magic or magic in header[:32]:
            return fmt
    return None


def validate_video_bytes(content: bytes, raw_name: str, max_size_bytes: Optional[int] = None,
                          content_type: Optional[str] = None) -> str:
    """
    Validate already-read video bytes: filename, size, magic bytes.

    Shared by validate_upload() (browser multipart upload) and the URL
    ingestion path (scripts/url_ingest.py) — same checks either way, so a
    video downloaded from a URL can never bypass any rule an uploaded file
    would have to pass.

    Returns the sanitized filename.
    Raises HTTPException 400/413 on validation failure.
    """
    trace_id = new_trace_id()
    limit = max_size_bytes or MAX_FILE_SIZE_BYTES

    if _DOUBLE_EXT.search(raw_name):
        logger.warning("[upload] double_extension filename=%s trace_id=%s", raw_name, trace_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_filename", "trace_id": trace_id},
        )

    ext = Path(raw_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unsupported_format", "allowed": list(ALLOWED_EXTENSIONS), "trace_id": trace_id},
        )

    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and ct not in ALLOWED_MIME_TYPES and ct != "application/octet-stream":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unsupported_content_type", "trace_id": trace_id},
        )

    total = len(content)
    if total > limit:
        logger.warning("[upload] file_too_large size>%d trace_id=%s", limit, trace_id)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "file_too_large", "max_mb": limit // (1024 * 1024), "trace_id": trace_id},
        )

    if total < 16:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "file_too_small", "trace_id": trace_id},
        )

    fmt = _detect_magic(content[:32])
    if fmt is None:
        logger.warning("[upload] magic_bytes_mismatch filename=%s trace_id=%s", raw_name, trace_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_video_content", "trace_id": trace_id},
        )

    safe_name = _safe_filename(raw_name)
    logger.info("[upload] validated filename=%s size=%d fmt=%s", safe_name, total, fmt)
    return safe_name


async def validate_upload(file: UploadFile, max_size_bytes: Optional[int] = None) -> tuple[bytes, str]:
    """
    Read and validate an uploaded video file.

    Returns:
        (file_content_bytes, safe_filename)

    Raises:
        HTTPException 400/413 on validation failure.
    """
    limit = max_size_bytes or MAX_FILE_SIZE_BYTES
    raw_name = file.filename or "upload.mp4"

    # ── Read with size limit ——————————————————————————————————————————————————
    # (kept here, not in validate_video_bytes, so an oversized upload is
    # rejected mid-stream instead of being buffered into memory in full)
    trace_id = new_trace_id()
    chunks = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)  # 1 MB chunks
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            logger.warning("[upload] file_too_large size>%d trace_id=%s", limit, trace_id)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"error": "file_too_large", "max_mb": limit // (1024 * 1024), "trace_id": trace_id},
            )
        chunks.append(chunk)

    content = b"".join(chunks)
    safe_name = validate_video_bytes(content, raw_name, max_size_bytes=limit, content_type=file.content_type)
    return content, safe_name
