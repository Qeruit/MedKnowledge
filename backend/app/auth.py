"""Revocable database sessions, role dependencies and user administration."""
import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text

from app.audit import audit
from app.config import settings
from app.contracts import ok
from app.db import AuthSession, User, db_session

COOKIE = "medknowledge_session"
Role = Literal["viewer", "editor", "admin"]
router = APIRouter()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3,
                            maxmem=128 * 1024 * 1024, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=32768, r=8, p=3,
                                maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


DUMMY_HASH = hash_password("invalid-login-placeholder")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def validate_origin(request: Request):
    origin = request.headers.get("origin")
    allowed = set(settings.cors_origins) | {str(request.base_url).rstrip("/")}
    if origin and origin not in allowed:
        raise HTTPException(403, "请求来源不允许")


def current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 128:
        raise HTTPException(401, "请先登录")
    with db_session() as db:
        session = db.get(AuthSession, token_hash(token))
        if session is None or session.expires_at <= datetime.now():
            raise HTTPException(401, "登录已过期，请重新登录")
        user = db.get(User, session.user_id)
        if user is None or not user.is_active:
            raise HTTPException(401, "账号已停用")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            validate_origin(request)
            csrf = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(csrf, session.csrf_token):
                raise HTTPException(403, "请求校验失败，请刷新页面后重试")
        request.state.auth_session = session
        return user


def editor_required(user: User = Depends(current_user)) -> User:
    if user.role not in {"editor", "admin"}:
        raise HTTPException(403, "需要资料管理员权限")
    return user


def admin_required(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "需要系统管理员权限")
    return user


def user_info(user):
    return {"id": user.id, "username": user.username, "role": user.role, "isActive": user.is_active}


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=128)


class CreateUserRequest(LoginRequest):
    password: str = Field(min_length=12, max_length=128)
    role: Role = "viewer"


class ChangePasswordRequest(BaseModel):
    oldPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=12, max_length=128)


class UpdateUserRequest(BaseModel):
    id: int = Field(gt=0)
    role: Role
    isActive: bool
    password: str | None = Field(default=None, min_length=12, max_length=128)


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response):
    validate_origin(request)
    now = datetime.now()
    key = token_hash(payload.username + ":" + (request.client.host if request.client else "unknown"))
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    denied = False
    with db_session() as db:
        db.execute(text("""INSERT INTO t_login_limit(key, failures, window_start)
                           VALUES (:key,0,:now) ON CONFLICT DO NOTHING"""), {"key": key, "now": now})
        bucket = db.execute(text("SELECT * FROM t_login_limit WHERE key=:key FOR UPDATE"), {"key": key}).one()
        if bucket.locked_until and bucket.locked_until > now:
            raise HTTPException(429, "登录失败次数过多，请稍后重试")
        user = db.scalar(select(User).where(User.username == payload.username))
        valid = verify_password(payload.password, user.password_hash if user else DUMMY_HASH)
        if not valid or user is None or not user.is_active:
            failures = (bucket.failures if bucket.window_start > now - timedelta(minutes=15) else 0) + 1
            db.execute(text("""UPDATE t_login_limit SET failures=:n, window_start=:now,
                               locked_until=:locked WHERE key=:key"""), {
                "n": failures, "now": now, "key": key,
                "locked": now + timedelta(minutes=15) if failures >= 10 else None})
            audit(db, "login_failed", detail=payload.username, actor=user)
            denied = True
        else:
            db.execute(text("DELETE FROM t_login_limit WHERE key=:key"), {"key": key})
            db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
            db.add(AuthSession(token_hash=token_hash(token), user_id=user.id, csrf_token=csrf,
                               expires_at=now + timedelta(hours=settings.auth_session_hours)))
            audit(db, "login", actor=user)
            info = user_info(user)
    if denied:
        raise HTTPException(401, "用户名或密码错误")
    response.set_cookie(COOKIE, token, max_age=settings.auth_session_hours * 3600,
                        httponly=True, secure=settings.auth_cookie_secure, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return ok({**info, "csrfToken": csrf})


@router.get("/auth/me")
def me(request: Request, response: Response, user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return ok({**user_info(user), "csrfToken": request.state.auth_session.csrf_token})


@router.post("/auth/logout")
def logout(request: Request, response: Response, user: User = Depends(current_user)):
    with db_session() as db:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == request.state.auth_session.token_hash))
        audit(db, "logout", actor=user)
    response.delete_cookie(COOKIE, path="/", secure=settings.auth_cookie_secure, httponly=True, samesite="strict")
    return ok()


@router.post("/auth/password")
def change_password(payload: ChangePasswordRequest, user: User = Depends(current_user)):
    with db_session() as db:
        row = db.scalar(select(User).where(User.id == user.id).with_for_update())
        if not verify_password(payload.oldPassword, row.password_hash):
            raise HTTPException(400, "原密码错误")
        row.password_hash = hash_password(payload.newPassword)
        db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        audit(db, "password_change", actor=user)
    return ok()


@router.get("/admin/users")
def users(user: User = Depends(admin_required)):
    with db_session() as db:
        return ok([user_info(row) for row in db.scalars(select(User).order_by(User.id)).all()])


@router.post("/admin/users")
def create_user(payload: CreateUserRequest, user: User = Depends(admin_required)):
    with db_session() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('user-admin-change'))"))
        if db.scalar(select(User.id).where(User.username == payload.username)):
            raise HTTPException(409, "用户名已存在")
        row = User(username=payload.username, password_hash=hash_password(payload.password), role=payload.role)
        db.add(row)
        db.flush()
        audit(db, "user_create", detail=f"user:{row.id},role:{row.role}", actor=user)
        return ok(user_info(row))


@router.post("/admin/users/update")
def update_user(payload: UpdateUserRequest, user: User = Depends(admin_required)):
    with db_session() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('user-admin-change'))"))
        actor = db.get(User, user.id)
        if not actor.is_active or actor.role != "admin":
            raise HTTPException(403, "管理员权限已变更")
        row = db.get(User, payload.id)
        if row is None:
            raise HTTPException(404, "账号不存在")
        if row.id == user.id and (payload.role != "admin" or not payload.isActive):
            raise HTTPException(400, "不能停用或降级当前管理员账号")
        row.role, row.is_active = payload.role, payload.isActive
        if payload.password:
            row.password_hash = hash_password(payload.password)
        db.execute(delete(AuthSession).where(AuthSession.user_id == row.id))
        audit(db, "user_update", detail=f"user:{row.id},role:{row.role},active:{row.is_active}", actor=user)
        return ok(user_info(row))
