"""
scripts/dev/local_fake_s3.py
=============================
Disk-backed stand-in for scripts/prod_s3_storage.py, used ONLY by the
NO-GPU local E2E harness (local_e2e_server.py monkeypatches the real
prod_s3_storage functions with these at process start; fake_streamer_worker.py
writes "uploaded" compose output straight here, matching whatever the
patched server-side functions would have done). Never imported by any
production code path.

Every object is just a file under SONYA_LOCAL_FAKE_S3_DIR (default:
/tmp/sonya_local_e2e_s3), at the same relative path as its real S3 key —
so build_input_key()/build_output_key() (real, unpatched, pure string
builders) keep working completely unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path


def root_dir() -> Path:
    p = Path(os.environ.get("SONYA_LOCAL_FAKE_S3_DIR", "/tmp/sonya_local_e2e_s3"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path_for(key: str) -> Path:
    # S3 keys are always relative, forward-slash paths (users/<id>/jobs/...)
    # -- safe to join directly onto the local root.
    return root_dir() / key


def write_bytes(key: str, data: bytes) -> str:
    path = _path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


def write_file(key: str, local_path: str) -> str:
    return write_bytes(key, Path(local_path).read_bytes())


def read_bytes(key: str) -> bytes:
    return _path_for(key).read_bytes()


def exists(key: str) -> bool:
    return _path_for(key).exists()


def delete(key: str) -> bool:
    path = _path_for(key)
    if path.exists():
        path.unlink()
        return True
    return False


def fake_presigned_url(key: str, expires_in: int = 3600) -> str:
    """
    A real presigned URL is absolute and signed; this is neither -- it's a
    same-origin relative path that local_e2e_server.py serves directly
    from root_dir() via StaticFiles at /fake-s3/. Good enough for a
    browser on the SAME local server to fetch/preview/download the fixture
    "clip", which is all the local E2E harness needs (no signing, no
    expiry -- expires_in is accepted only to match the real function's
    signature so the monkeypatch is a drop-in replacement).
    """
    return f"/fake-s3/{key}"
