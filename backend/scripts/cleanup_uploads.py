"""Remove expired upload sessions/parts. Schedule daily on the upload-volume host."""

from datetime import datetime

from sqlalchemy import select, text

from app.config import settings
from app.db import UploadSession, db_session
from app.storage_guard import storage_lock


def cleanup():
    root = (settings.md_storage_path / ".uploads").resolve()
    removed = 0
    with db_session() as db:
        sessions = db.scalars(select(UploadSession).where(
            UploadSession.status == "uploading",
            UploadSession.expires_at < datetime.now()).with_for_update(skip_locked=True).limit(100)).all()
        for session in sessions:
            folder = (root / str(session.id)).resolve()
            if folder.parent != root:
                raise RuntimeError("上传缓存目录越界")
            if folder.is_dir():
                # Only flat, expected part/temp files are eligible. Unknown content is left for review.
                paths = list(folder.iterdir())
                if any(not path.is_file() or path.suffix not in {".part", ".tmp"} for path in paths):
                    continue
                for path in paths:
                    path.unlink()
                folder.rmdir()
            db.execute(text("DELETE FROM t_upload_chunk WHERE upload_id=:id"), {"id": session.id})
            session.status = "expired"
            session.update_time = datetime.now()
            removed += 1
    print(f"Expired upload sessions cleaned: {removed}")


def main():
    with storage_lock(): cleanup()


if __name__ == "__main__":
    main()
