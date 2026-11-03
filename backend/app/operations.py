"""Monitoring views and stateful alerts; only admins may inspect them."""
from datetime import datetime
import logging

from fastapi import APIRouter,Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.auth import admin_required
from app.config import settings
from app.contracts import ok
from app.db import db_session

router=APIRouter()
logger=logging.getLogger(__name__)


def snapshot():
    with db_session() as db:
        timezone=db.scalar(text("SHOW TIMEZONE"))
        counts=dict(db.execute(text("SELECT status,count(*) FROM t_index_job GROUP BY status")).all())
        age=float(db.scalar(text("""SELECT COALESCE(EXTRACT(EPOCH FROM(clock_timestamp()-min(create_time))),0)
            FROM t_index_job WHERE status='queued'""")))
        workers=db.scalar(text("""SELECT count(*) FROM t_worker_heartbeat
            WHERE last_seen>clock_timestamp()-:ttl*interval '1 second'"""),{"ttl":settings.task_lease_seconds*2})
        samples=db.execute(text("""SELECT kind,outcome,count(*) AS count,
            percentile_cont(0.5) WITHIN GROUP(ORDER BY duration) AS p50,
            percentile_cont(0.95) WITHIN GROUP(ORDER BY duration) AS p95,
            percentile_cont(0.95) WITHIN GROUP(ORDER BY first_token) AS ttft,
            sum(units) AS units FROM t_ops_event
            WHERE create_time>clock_timestamp()-:window*interval '1 second' GROUP BY kind,outcome"""),
            {"window":settings.monitor_window_seconds}).mappings().all()
        recent=db.execute(text("""SELECT count(*) AS failed FROM t_index_job WHERE status='failed'
            AND update_time>clock_timestamp()-:window*interval '1 second'"""),
            {"window":settings.monitor_window_seconds}).mappings().one()["failed"]
        backups=[dict(row) for row in db.execute(text("SELECT * FROM t_backup_run ORDER BY started_at DESC LIMIT 10")).mappings()]
        last_backup=db.scalar(text("SELECT max(ended_at) FROM t_backup_run WHERE status='succeeded'"))
        backup_age=db.scalar(text("""SELECT EXTRACT(EPOCH FROM(clock_timestamp()-max(ended_at)))
            FROM t_backup_run WHERE status='succeeded'"""))
        agent=db.scalar(text("""SELECT EXISTS(SELECT 1 FROM t_ops_heartbeat
            WHERE last_seen>clock_timestamp()-:ttl*interval '1 second')"""),{"ttl":settings.monitor_interval_seconds*3})
        alerts=[dict(row) for row in db.execute(text("SELECT * FROM t_ops_alert ORDER BY rule")).mappings()]
        history=[dict(row) for row in db.execute(text("SELECT * FROM t_ops_alert_history ORDER BY id DESC LIMIT 30")).mappings()]
    model=[row for row in samples if row["kind"] in ("embedding","chat")]
    calls=sum(row["count"] for row in model)
    errors=sum(row["count"] for row in model if row["outcome"]=="error")
    return {"databaseTimezone":timezone,"windowSeconds":settings.monitor_window_seconds,"queue":counts,"oldestQueuedSeconds":max(0,age),
            "workersAlive":workers,"agentAlive":agent,"events":[dict(row) for row in samples],
            "modelCalls":calls,"modelErrors":errors,"modelErrorRatio":errors/calls if calls else 0,
            "recentFailedTasks":recent,"lastBackup":last_backup,"backupAgeSeconds":float(backup_age) if backup_age is not None else None,
            "backups":backups,"alerts":alerts,"alertHistory":history}


def evaluate(data):
    pending=data["queue"].get("queued",0)+data["queue"].get("running",0)
    rules={
        "worker_missing":(pending>0 and data["workersAlive"]==0,f"待处理任务 {pending}，未检测到有效 Worker 心跳"),
        "queue_stalled":(data["oldestQueuedSeconds"]>=settings.alert_queue_age_seconds,
                          f"最老等待任务 {data['oldestQueuedSeconds']:.1f} 秒"),
        "model_errors":(data["modelCalls"]>=settings.alert_model_min_calls and data["modelErrorRatio"]>=settings.alert_model_error_ratio,
                         f"窗口内模型调用 {data['modelCalls']}，失败 {data['modelErrors']}"),
        "task_failures":(data["recentFailedTasks"]>0,f"窗口内终态失败任务 {data['recentFailedTasks']}"),
        "backup_stale":(data["backupAgeSeconds"] is None or data["backupAgeSeconds"]>settings.backup_interval_hours*3600*1.25,
                         "尚无成功备份或最近备份已超过期限"),
        "backup_failed":(bool(data["backups"]) and data["backups"][0]["status"]=="failed","最近一次备份失败"),
    }
    transitions=[]
    with db_session() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('medknowledge-alert-eval'))"))
        now=db.scalar(text("SELECT clock_timestamp()::timestamp"))
        for rule,(condition,detail) in rules.items():
            row=db.execute(text("SELECT * FROM t_ops_alert WHERE rule=:rule"),{"rule":rule}).mappings().first()
            before=row["state"] if row else "clear"
            first=row["first_seen"] if row and row["state"] in ("pending","firing") else now if condition else None
            state=("firing" if condition and (now-first).total_seconds()>=settings.alert_hold_seconds
                   else "pending" if condition else "resolved" if before in ("pending","firing","resolved") else "clear")
            changed=row["changed_at"] if row and before==state else now
            db.execute(text("""INSERT INTO t_ops_alert(rule,state,first_seen,changed_at,last_seen,detail)
                VALUES(:rule,:state,:first,:changed,:now,:detail) ON CONFLICT(rule) DO UPDATE SET
                state=EXCLUDED.state,first_seen=EXCLUDED.first_seen,changed_at=EXCLUDED.changed_at,
                last_seen=EXCLUDED.last_seen,detail=EXCLUDED.detail"""),
                {"rule":rule,"state":state,"first":first,"changed":changed,"now":now,"detail":detail})
            if state != before and state in ("firing","resolved"):
                db.execute(text("INSERT INTO t_ops_alert_history(rule,state,detail) VALUES(:rule,:state,:detail)"),
                           {"rule":rule,"state":state,"detail":detail})
                transitions.append({"rule":rule,"state":state,"detail":detail})
        db.execute(text("""INSERT INTO t_ops_heartbeat(name) VALUES('ops-agent')
            ON CONFLICT(name) DO UPDATE SET last_seen=clock_timestamp()"""))
        db.execute(text("""DELETE FROM t_ops_event WHERE id IN (SELECT id FROM t_ops_event
            WHERE create_time<clock_timestamp()-interval '7 days' LIMIT 10000)"""))
    for item in transitions: logger.warning("OPS_ALERT %s",item)
    return transitions


@router.get("/ops/status")
def status(user=Depends(admin_required)):
    return ok(snapshot())


@router.get("/ops/chat-usage")
def chat_usage(user=Depends(admin_required)):
    """Estimated tokens/cost by user; never return prompts or answers."""
    with db_session() as db:
        rows=db.execute(text("""SELECT u.id AS user_id,u.username,
            count(r.id) FILTER(WHERE r.started_at>=date_trunc('day',
                clock_timestamp() AT TIME ZONE 'UTC')) AS requests_today,
            COALESCE(sum(r.embedding_tokens+r.input_tokens+r.output_tokens)
                FILTER(WHERE r.status='closed' AND r.started_at>=date_trunc('day',
                    clock_timestamp() AT TIME ZONE 'UTC')),0) AS tokens_today,
            COALESCE(sum(r.embedding_tokens+r.input_tokens+r.output_tokens)
                FILTER(WHERE r.status='closed' AND r.started_at>=date_trunc('month',
                    clock_timestamp() AT TIME ZONE 'UTC')),0) AS tokens_month,
            COALESCE(sum(r.estimated_cost)
                FILTER(WHERE r.status='closed' AND r.started_at>=date_trunc('month',
                    clock_timestamp() AT TIME ZONE 'UTC')),0) AS estimated_cost_month,
            count(r.id) FILTER(WHERE r.status='active' AND r.lease_until>
                clock_timestamp() AT TIME ZONE 'UTC') AS active
            FROM t_user u LEFT JOIN t_chat_request r ON r.user_id=u.id
            GROUP BY u.id,u.username ORDER BY u.id""")).mappings().all()
    return ok({"tokenUnit":f"{settings.rag_token_encoding} estimate (not provider billing)",
               "costUnit":"configured currency, zero if rates are unset",
               "dailyTokenLimit":settings.chat_daily_token_limit,
               "monthlyTokenLimit":settings.chat_monthly_token_limit,
               "users":[{"userId":row["user_id"],"username":row["username"],
                         "requestsToday":row["requests_today"],"tokensToday":row["tokens_today"],
                         "tokensMonth":row["tokens_month"],
                         "estimatedCostMonth":str(row["estimated_cost_month"]),
                         "active":row["active"]} for row in rows]})


@router.get("/ops/metrics",response_class=PlainTextResponse)
def metrics(user=Depends(admin_required)):
    data=snapshot()
    lines=["# Operational gauges; rolling window, not monotonic counters.",
           f"medknowledge_workers_alive {data['workersAlive']}",
           f"medknowledge_ops_agent_alive {int(data['agentAlive'])}",
           f"medknowledge_queue_oldest_seconds {data['oldestQueuedSeconds']}",
           f"medknowledge_model_calls_window {data['modelCalls']}",
           f"medknowledge_model_errors_window {data['modelErrors']}"]
    for state in ("queued","running","failed","succeeded"):
        lines.append(f'medknowledge_tasks{{state="{state}"}} {data["queue"].get(state,0)}')
    for row in data["events"]:
        labels=f'kind="{row["kind"]}",outcome="{row["outcome"]}"'
        lines += [f'medknowledge_events_window{{{labels}}} {row["count"]}',
                  f'medknowledge_duration_p95_seconds{{{labels}}} {row["p95"] or 0}']
        if row["ttft"] is not None: lines.append(f'medknowledge_first_answer_p95_seconds{{{labels}}} {row["ttft"]}')
    return "\n".join(lines)+"\n"
