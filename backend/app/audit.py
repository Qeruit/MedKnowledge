from datetime import datetime
from sqlalchemy import text


def audit(db, action: str, file_id: int | None = None, detail: str = "", *, actor=None):
    db.execute(text("""
        INSERT INTO t_admin_audit (action, file_id, detail, create_time, actor_id, actor_name)
        VALUES (:action, :file_id, :detail, :created, :actor_id, :actor_name)
    """), {"action": action, "file_id": file_id, "detail": detail[:500], "created": datetime.now(),
           "actor_id": actor.id if actor else None, "actor_name": actor.username if actor else None})
