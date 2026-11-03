"""Create the first admin and assign orphaned legacy files without publishing them."""
import argparse
from datetime import datetime
import getpass
import json
import secrets

from sqlalchemy import select, text

from app.auth import hash_password
from app.config import PROJECT_ROOT
from app.db import User, db_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true", help="Save a generated password in an ignored local file")
    args = parser.parse_args()
    credential_path = PROJECT_ROOT / ".credentials" / "admin-bootstrap.json"
    with db_session() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('user-admin-change'))"))
        admin = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)).order_by(User.id).limit(1))
        if admin is None:
            if credential_path.exists():
                raise RuntimeError("已有初始化凭据文件，请核对数据库；不会覆盖")
            password = secrets.token_urlsafe(24) if args.generate else getpass.getpass("New admin password (12+ characters): ")
            if len(password) < 12:
                raise RuntimeError("密码至少 12 个字符")
            admin = User(username="admin", password_hash=hash_password(password), role="admin", create_time=datetime.now())
            db.add(admin)
            db.flush()
            if args.generate:
                credential_path.parent.mkdir(parents=True, exist_ok=True)
                with credential_path.open("x", encoding="utf-8") as file:
                    json.dump({"username": "admin", "password": password}, file, indent=2)
                print(f"Generated credentials saved locally: {credential_path}")
        db.execute(text("UPDATE t_ai_customer_service_md_storage SET owner_id=:id WHERE owner_id IS NULL"), {"id": admin.id})
        db.execute(text("UPDATE t_upload_session SET owner_id=:id WHERE owner_id IS NULL"), {"id": admin.id})
        print(f"Admin ready: {admin.username}. Legacy files assigned; publication settings preserved.")


if __name__ == "__main__":
    main()
