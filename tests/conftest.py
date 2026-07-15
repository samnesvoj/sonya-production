from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app():
    # Imported lazily so the root conftest.py env vars are guaranteed to be
    # set before scripts.prod_generation_api runs its import-time CORS check.
    from scripts.prod_generation_api import app as fastapi_app
    return fastapi_app


@pytest.fixture()
def client(app):
    # base_url must be https:// so the client honors Secure cookies
    # (sonya_session is HttpOnly + Secure + SameSite=Lax).
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def make_user(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    user = {
        "id": str(uuid.uuid4()),
        "email": "user@example.com",
        "email_verified_at": now,
        "plan_type": "free",
        "plan_status": "active",
        "plan_active_until": None,
        "free_video_limit": 1,
        "free_video_used": 0,
        "telegram_linked": False,
        "created_at": now,
        "updated_at": now,
    }
    user.update(overrides)
    return user


def make_session(user_id: str, token_hash: str, **overrides) -> dict:
    now = datetime.now(timezone.utc)
    session = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "token_hash": token_hash,
        "expires_at": now + timedelta(days=30),
        "revoked_at": None,
        "user_agent": "pytest",
        "ip_address": "127.0.0.1",
        "created_at": now,
    }
    session.update(overrides)
    return session


def make_auth_code(email: str, code_hash: str, purpose: str = "login", **overrides) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "id": str(uuid.uuid4()),
        "email": email,
        "code_hash": code_hash,
        "purpose": purpose,
        "expires_at": now + timedelta(minutes=10),
        "consumed_at": None,
        "attempts": 0,
        "ip_address": "127.0.0.1",
        "created_at": now,
    }
    row.update(overrides)
    return row
