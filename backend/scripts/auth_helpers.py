"""Local operator authentication for smoke tests; never print credentials."""
import json
import os

from sqlalchemy import select

from app.config import PROJECT_ROOT
from app.db import User, db_session


def login_client(client):
    username = os.getenv("AUTH_TEST_USERNAME", "admin")
    password = os.getenv("AUTH_TEST_PASSWORD")
    if not password:
        path = PROJECT_ROOT / ".credentials" / "admin-bootstrap.json"
        if not path.exists():
            raise RuntimeError("请先初始化管理员，或设置 AUTH_TEST_USERNAME / AUTH_TEST_PASSWORD")
        credentials = json.loads(path.read_text(encoding="utf-8"))
        if username != credentials["username"]:
            raise RuntimeError("非初始管理员请通过 AUTH_TEST_PASSWORD 提供密码")
        password = credentials["password"]
    response = client.post("/auth/login", json={"username": username, "password": password})
    if response.status_code != 200:
        raise RuntimeError("测试登录失败；初始密码可能已修改，请设置 AUTH_TEST_PASSWORD（不要公开密码）")
    csrf = response.json()["data"]["csrfToken"]
    client.headers.update({"X-CSRF-Token": csrf})
    return {"X-CSRF-Token": csrf}


def evaluation_principal():
    username = os.getenv("AUTH_TEST_USERNAME", "admin")
    with db_session() as db:
        user = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
        if not user:
            raise RuntimeError("评测账号不存在或已停用")
        return {"user_id": user.id, "role": user.role}
