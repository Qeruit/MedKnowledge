"""Chat capacity/usage tests in a disposable database, never production data."""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT, settings


@pytest.fixture
def limits_db(monkeypatch):
    from app import db, main, chat_limits, llm, operations
    name = "agent_customer_service_limits_" + uuid4().hex[:12]
    source = make_url(settings.database_url)
    admin = create_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine = create_engine(source.set(database=name))
    with engine.begin() as conn:
        conn.exec_driver_sql((PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8"))
    chosen = replace(settings, database_name=name, chat_requests_per_minute=3,
                     chat_global_concurrency=2, chat_user_concurrency=1,
                     chat_daily_token_limit=100000, chat_monthly_token_limit=1000000,
                     chat_max_output_tokens=10, chat_max_prompt_tokens=12000,
                     chat_input_cost_per_million=1, chat_output_cost_per_million=2,
                     embedding_cost_per_million=0.5)
    if db.session_factory.cache_info().currsize:
        db.session_factory().kw["bind"].dispose()
        db.session_factory.cache_clear()
    for module in (db, main, chat_limits, llm, operations):
        monkeypatch.setattr(module, "settings", chosen)
    from app.db import User, db_session
    with db_session() as session:
        session.add_all([
            User(username="limit1", password_hash="test", role="admin", is_active=True,
                 create_time=datetime.now()),
            User(username="limit2", password_hash="test", role="viewer", is_active=True,
                 create_time=datetime.now()),
        ])
        session.flush()
        ids = [row.id for row in session.query(User).order_by(User.id).all()]
    try:
        yield chosen, ids
    finally:
        main.app.dependency_overrides.clear()
        if db.session_factory.cache_info().currsize:
            db.session_factory().kw["bind"].dispose()
            db.session_factory.cache_clear()
        engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{name}"')
        admin.dispose()


def test_shared_concurrency_rate_and_expired_slot(limits_db):
    from app import chat_limits
    from app.db import db_session
    first, second = limits_db[1]
    one = chat_limits.begin_request(first, 2)
    with pytest.raises(HTTPException) as same_user:
        chat_limits.begin_request(first, 2)
    assert same_user.value.status_code == 429
    two = chat_limits.begin_request(second, 2)
    with pytest.raises(HTTPException) as global_full:
        chat_limits.begin_request(first, 2)
    assert global_full.value.status_code == 429
    chat_limits.finish_request(one, output_tokens=3)
    three = chat_limits.begin_request(first, 2)
    with db_session() as db:
        db.execute(text("UPDATE t_chat_request SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"),
                   {"id": three})
    replacement = chat_limits.begin_request(first, 2)
    with db_session() as db:
        expired = db.execute(text("SELECT status,outcome,embedding_tokens FROM t_chat_request WHERE id=:id"),
                             {"id": three}).one()
    assert expired.status == "closed" and expired.outcome == "expired"
    assert expired.embedding_tokens >= 12  # Worst-case reservation is charged on crash.
    chat_limits.finish_request(two)
    chat_limits.finish_request(replacement)


def test_quota_prompt_cap_and_usage_settlement(limits_db, monkeypatch):
    from app import chat_limits
    from app.db import db_session
    first = limits_db[1][0]
    monkeypatch.setattr(chat_limits, "settings", replace(limits_db[0], chat_daily_token_limit=23,
                                                      chat_max_prompt_tokens=100))
    one = chat_limits.begin_request(first, 3)
    with pytest.raises(HTTPException) as too_large:
        chat_limits.reserve_prompt(one, 101)
    assert too_large.value.status_code == 413
    with pytest.raises(HTTPException) as over_quota:
        chat_limits.reserve_prompt(one, 11)  # 3 + 11 + 10 = 24 > 23
    assert over_quota.value.status_code == 429
    chat_limits.reserve_prompt(one, 10)
    chat_limits.finish_request(one, output_tokens=5)
    with db_session() as db:
        row = db.execute(text("SELECT status,embedding_tokens,input_tokens,output_tokens,estimated_cost "
                              "FROM t_chat_request WHERE id=:id"), {"id": one}).one()
    assert (row.status, row.embedding_tokens, row.input_tokens, row.output_tokens) == ("closed", 3, 10, 5)
    assert row.estimated_cost > 0
    with pytest.raises(HTTPException) as exhausted:
        chat_limits.begin_request(first, 3)  # 18 spent + 13 reserved > 23
    assert exhausted.value.status_code == 429


def test_sse_success_error_and_admin_usage(limits_db, monkeypatch):
    from app import main
    from app.auth import current_user
    from app.db import db_session
    first = limits_db[1][0]
    main.app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=first, username="limit1", role="admin", is_active=True)
    monkeypatch.setattr(main, "capture_answer_evidence", lambda *_, **__: {"documents": []})
    monkeypatch.setattr(main, "require_llm", lambda **kwargs: None)
    monkeypatch.setattr(main, "open_conversation", lambda *_, **__: (uuid4(), []))
    monkeypatch.setattr(main, "finish_conversation", lambda *_, **__: True)

    async def context(*_, **__):
        return [{"content": "## 测试知识\n答案 ABC", "metadata": {}}]

    async def chunks(*_):
        yield "ABC"

    monkeypatch.setattr(main, "find_context", context)
    monkeypatch.setattr(main, "chat_chunks", chunks)
    client = TestClient(main.app)
    result = client.post("/customer-service/completion", json={"message": "测试？", "fileId": 1})
    assert result.status_code == 200 and 'data: {"v": "ABC"}' in result.text
    assert "event: close" in result.text

    async def failure(*_):
        raise RuntimeError("injected")
        yield

    monkeypatch.setattr(main, "chat_chunks", failure)
    failed = client.post("/customer-service/completion", json={"message": "再问？", "fileId": 1})
    assert failed.status_code == 200 and "event: error" in failed.text
    usage = client.get("/ops/chat-usage")
    assert usage.status_code == 200
    user_usage = next(row for row in usage.json()["data"]["users"] if row["userId"] == first)
    assert user_usage["active"] == 0 and user_usage["requestsToday"] == 2
    assert user_usage["tokensToday"] > 0
    with db_session() as db:
        outcomes = db.execute(text("SELECT outcome FROM t_chat_request WHERE user_id=:id ORDER BY started_at"),
                              {"id": first}).scalars().all()
    assert sorted(outcomes) == ["error", "success"]
    main.app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=limits_db[1][1], username="limit2", role="viewer", is_active=True)
    assert client.get("/ops/chat-usage").status_code == 403


def test_provider_output_cap(limits_db):
    from app.chat_limits import capped_delta, token_count
    from app.llm import _chat_payload
    assert _chat_payload([], "test", stream=True)["max_tokens"] == 10
    text_value, delta, capped = capped_delta("", "A " * 50)
    assert capped and delta == text_value and token_count(text_value) <= 10


def test_closed_sse_iterator_releases_slot(limits_db, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "capture_answer_evidence", lambda *_, **__: {"documents": []})
    from app.contracts import CustomerCompletionRequest
    from app.db import db_session
    first = limits_db[1][0]
    monkeypatch.setattr(main, "require_llm", lambda **kwargs: None)
    monkeypatch.setattr(main, "open_conversation", lambda *_, **__: (uuid4(), []))
    monkeypatch.setattr(main, "finish_conversation", lambda *_, **__: True)

    async def context(*_, **__):
        return [{"content": "## 事实\nABC", "metadata": {}}]

    async def chunks(*_):
        yield "never reached"

    monkeypatch.setattr(main, "find_context", context)
    monkeypatch.setattr(main, "chat_chunks", chunks)

    async def close_early():
        response = await main.customer_completion(CustomerCompletionRequest(message="事实？", fileId=1),
                                                  SimpleNamespace(id=first, role="admin"))
        assert "event: conversation" in await anext(response.body_iterator)
        assert "event: sources" in await anext(response.body_iterator)
        await response.body_iterator.aclose()

    asyncio.run(close_early())
    with db_session() as db:
        row = db.execute(text("SELECT status,outcome FROM t_chat_request WHERE user_id=:id"),
                         {"id": first}).one()
    assert row == ("closed", "cancelled")
