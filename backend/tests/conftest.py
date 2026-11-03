from datetime import datetime
import secrets

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, text

from app import main
from app.auth import hash_password, token_hash
from app.db import AuthSession, User, db_session


@pytest.fixture
def admin_client():
    username = "testadmin_" + secrets.token_hex(6)
    password = "Test-only-password-2026"
    with db_session() as db:
        user = User(username=username, password_hash=hash_password(password), role="admin", create_time=datetime.now())
        db.add(user)
        db.flush()
    client = TestClient(main.app)
    login = client.post("/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200
    headers = {"X-CSRF-Token": login.json()["data"]["csrfToken"]}
    try:
        yield client, headers, user
    finally:
        with db_session() as db:
            db.execute(text("DELETE FROM t_worker_heartbeat WHERE worker_id='test-worker'"))
            db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
            db.execute(text("DELETE FROM t_admin_audit WHERE actor_id=:id"), {"id": user.id})
            db.execute(text("DELETE FROM t_login_limit WHERE key=:key"), {"key": token_hash(username + ":testclient")})
            db.execute(delete(User).where(User.id == user.id))
