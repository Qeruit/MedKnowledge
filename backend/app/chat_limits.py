"""Cross-process chat admission, expiring SSE slots and estimated usage.

Token counts use cl100k_base (or the configured local encoding), not provider
billing data. A reservation covers the worst-case output before model calls.
All quota boundaries use UTC. The single advisory transaction lock serializes
admission across API processes; no in-memory counter is authoritative.
"""

from decimal import Decimal
from functools import lru_cache
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text
import tiktoken

from app.config import settings
from app.db import db_session


LEASE_SECONDS = 300
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('medknowledge-chat-admission'))"
UTC_NOW = "(clock_timestamp() AT TIME ZONE 'UTC')"


@lru_cache(maxsize=4)
def _encoding(name: str):
    return tiktoken.get_encoding(name)


def token_count(value: str) -> int:
    return len(_encoding(settings.rag_token_encoding).encode(value))


def message_tokens(messages: list[dict]) -> int:
    # Small framing allowance per message; still an estimate, not API usage.
    return sum(token_count(str(item.get("content") or "")) + 4 for item in messages) + 3


def capped_delta(previous: str, chunk: str) -> tuple[str, str, bool]:
    candidate = previous + chunk
    tokens = _encoding(settings.rag_token_encoding).encode(candidate)
    if len(tokens) <= settings.chat_max_output_tokens:
        return candidate, chunk, False
    trimmed = _encoding(settings.rag_token_encoding).decode(tokens[:settings.chat_max_output_tokens]).rstrip("\ufffd")
    if not trimmed.startswith(previous):
        return previous, "", True
    return trimmed, trimmed[len(previous):], True


def _reject(message: str, retry: int = 60):
    raise HTTPException(429, message, headers={"Retry-After": str(retry)})


def _spent(db, user_id: int, period: str, exclude=None) -> int:
    if period not in ("day", "month"):
        raise ValueError("无效的额度周期")
    return int(db.scalar(text(f"""SELECT COALESCE(sum(CASE WHEN status='active'
            THEN reserved_tokens ELSE embedding_tokens+input_tokens+output_tokens END),0)
        FROM t_chat_request WHERE user_id=:user AND id IS DISTINCT FROM :exclude
          AND started_at >= date_trunc('{period}', {UTC_NOW})
          AND (status='closed' OR lease_until>{UTC_NOW})"""),
        {"user": user_id, "exclude": exclude}) or 0)


def _check_quota(db, user_id: int, reservation: int, exclude=None):
    if _spent(db, user_id, "day", exclude) + reservation > settings.chat_daily_token_limit:
        _reject("今日问答 Token 额度已用尽，请稍后再试", 3600)
    if _spent(db, user_id, "month", exclude) + reservation > settings.chat_monthly_token_limit:
        _reject("本月问答 Token 额度已用尽，请联系管理员", 3600)


def begin_request(user_id: int, embedding_tokens: int):
    request_id = uuid4()
    reserve = embedding_tokens + settings.chat_max_output_tokens
    with db_session() as db:
        db.execute(text(LOCK_SQL))
        # A crashed process must not hold a slot forever. Conservatively charge
        # its full reservation because actual provider usage is unknowable.
        db.execute(text(f"""UPDATE t_chat_request SET status='closed',
            embedding_tokens=GREATEST(embedding_tokens,reserved_tokens-input_tokens),
            reserved_tokens=0,outcome='expired',ended_at={UTC_NOW},lease_until=NULL
            WHERE status='active' AND lease_until<={UTC_NOW}"""))
        recent = db.scalar(text(f"""SELECT count(*) FROM t_chat_request WHERE user_id=:user
            AND started_at>{UTC_NOW}-interval '1 minute'"""), {"user": user_id})
        if recent >= settings.chat_requests_per_minute:
            _reject("请求过于频繁，请稍后再试")
        active = db.execute(text("""SELECT count(*) AS total,
            count(*) FILTER(WHERE user_id=:user) AS mine FROM t_chat_request
            WHERE status='active'"""), {"user": user_id}).one()
        if active.total >= settings.chat_global_concurrency:
            _reject("服务繁忙，请稍后再试", 5)
        if active.mine >= settings.chat_user_concurrency:
            _reject("已有问答正在生成，请等待当前回答完成", 5)
        _check_quota(db, user_id, reserve)
        db.execute(text(f"""INSERT INTO t_chat_request
            (id,user_id,status,reserved_tokens,embedding_tokens,lease_until,started_at)
            VALUES(:id,:user,'active',:reserve,:embedding,
                   {UTC_NOW}+:lease*interval '1 second',{UTC_NOW})"""),
            {"id": request_id, "user": user_id, "reserve": reserve,
             "embedding": embedding_tokens, "lease": LEASE_SECONDS})
    return request_id


def reserve_prompt(request_id, prompt_tokens: int):
    if prompt_tokens > settings.chat_max_prompt_tokens:
        raise HTTPException(413, "检索上下文过长，超过单次问答输入 Token 上限")
    with db_session() as db:
        db.execute(text(LOCK_SQL))
        row = db.execute(text(f"""SELECT user_id,embedding_tokens FROM t_chat_request
            WHERE id=:id AND status='active' AND lease_until>{UTC_NOW} FOR UPDATE"""),
            {"id": request_id}).first()
        if not row:
            raise HTTPException(503, "问答请求租约已失效，请重试")
        reserve = row.embedding_tokens + prompt_tokens + settings.chat_max_output_tokens
        _check_quota(db, row.user_id, reserve, request_id)
        db.execute(text("""UPDATE t_chat_request SET reserved_tokens=:reserve,
            input_tokens=:prompt WHERE id=:id"""),
            {"id": request_id, "reserve": reserve, "prompt": prompt_tokens})


def renew_request(request_id) -> bool:
    with db_session() as db:
        return db.execute(text(f"""UPDATE t_chat_request
            SET lease_until={UTC_NOW}+:lease*interval '1 second'
            WHERE id=:id AND status='active' AND lease_until>{UTC_NOW}
            RETURNING id"""), {"id": request_id, "lease": LEASE_SECONDS}).scalar_one_or_none() is not None


def finish_request(request_id, *, output_tokens: int = 0, outcome: str = "success"):
    if outcome not in ("success", "error", "cancelled", "rejected"):
        raise ValueError("无效的问答结果")
    with db_session() as db:
        row = db.execute(text("""SELECT embedding_tokens,input_tokens FROM t_chat_request
            WHERE id=:id AND status='active' FOR UPDATE"""), {"id": request_id}).first()
        if not row:
            return False
        price = (Decimal(row.embedding_tokens) * Decimal(str(settings.embedding_cost_per_million))
                 + Decimal(row.input_tokens) * Decimal(str(settings.chat_input_cost_per_million))
                 + Decimal(output_tokens) * Decimal(str(settings.chat_output_cost_per_million))) / Decimal(1_000_000)
        db.execute(text(f"""UPDATE t_chat_request SET status='closed',reserved_tokens=0,
            output_tokens=:output,estimated_cost=:cost,outcome=:outcome,
            lease_until=NULL,ended_at={UTC_NOW} WHERE id=:id"""),
            {"id": request_id, "output": output_tokens, "cost": price,
             "outcome": outcome})
    return True
