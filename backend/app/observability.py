"""Content-free, DB-persisted telemetry and streaming-aware ASGI timing."""
import asyncio
import logging
import time

from sqlalchemy import text

from app.db import db_session

logger=logging.getLogger(__name__)


def record(kind,outcome,duration=0,route="",status=None,first_token=None,units=0):
    try:
        with db_session() as db:
            db.execute(text("SET LOCAL statement_timeout='1500ms'"))
            db.execute(text("""INSERT INTO t_ops_event(kind,route,outcome,status,duration,first_token,units)
                VALUES(:kind,:route,:outcome,:status,:duration,:first,:units)"""),
                {"kind":kind,"route":route[:160],"outcome":outcome,"status":status,
                 "duration":max(0,duration),"first":first_token,"units":units})
    except Exception:
        # Do not break an answer or expose SQL/connection secrets on telemetry failure.
        logger.warning("Telemetry persistence unavailable")


class OpsMiddleware:
    def __init__(self,app): self.app=app

    async def __call__(self,scope,receive,send):
        if scope["type"] != "http" or scope.get("path","").startswith("/ops/"):
            return await self.app(scope,receive,send)
        started=time.perf_counter()
        status=500
        is_sse=False
        first_token=None
        completed=False
        stream_error=False

        async def measured_send(message):
            nonlocal status,is_sse,first_token,completed,stream_error
            if message["type"] == "http.response.start":
                status=message["status"]
                is_sse=any(key==b"content-type" and b"text/event-stream" in value for key,value in message.get("headers",[]))
            elif message["type"] == "http.response.body":
                body=message.get("body",b"")
                if is_sse:
                    if first_token is None and body.startswith(b'data: {"v":'):
                        first_token=time.perf_counter()-started
                    if body.startswith(b"event: error\n"): stream_error=True
                if not message.get("more_body",False): completed=True
            await send(message)
        try:
            await self.app(scope,receive,measured_send)
        finally:
            route=getattr(scope.get("route"),"path","unmatched")
            outcome="error" if status>=500 else "client_error" if status>=400 else "success"
            if is_sse:
                outcome="error" if stream_error else "success" if completed else "cancelled"
            await asyncio.shield(asyncio.to_thread(record,"sse" if is_sse else "request",outcome,
                time.perf_counter()-started,route,status,first_token))
