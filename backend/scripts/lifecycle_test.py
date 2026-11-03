"""Exercise CRUD against a newly created disposable record in the dedicated DB."""

import time

import httpx
from sqlalchemy import create_engine, text

from app.config import PROJECT_ROOT, settings
from scripts.smoke_test import CLIENT, DEMO, request
from scripts.auth_helpers import login_client

SAMPLE = PROJECT_ROOT / "data" / "lifecycle_test_only.md"


def main():
    login_client(CLIENT)
    with SAMPLE.open("rb") as file:
        response = CLIENT.post("/customer-service/md/upload",
                              files={"file": (SAMPLE.name, file, "text/markdown")},
                              timeout=90)
    response.raise_for_status()
    body = response.json()
    assert body["success"], body
    file_id = body["data"]["id"]
    print(f"Created disposable file #{file_id}")

    try:
        for _ in range(30):
            rows = request("/customer-service/md/list", {"current": 1, "size": 20})["data"]
            row = next((item for item in rows if item["id"] == file_id), None)
            if row and row["status"] in (2, 3):
                break
            time.sleep(1)
        assert row and row["status"] == 2, row

        request("/customer-service/md/update", {"id": file_id, "remark": "生命周期联调通过"})
        rows = request("/customer-service/md/list", {"current": 1, "size": 20})["data"]
        assert next(item for item in rows if item["id"] == file_id)["remark"] == "生命周期联调通过"
        print("Upload, indexing, update: verified")
    finally:
        # Only this exact record, created by this script, may be removed.
        result = request("/customer-service/md/delete", {"id": file_id})
        assert result["success"]
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        vectors = conn.execute(text(
            "SELECT count(*) FROM t_vector_store WHERE metadata ->> 'mdStorageId' = :id"
        ), {"id": str(file_id)}).scalar_one()
        original_rows = conn.execute(text(
            "SELECT count(*) FROM t_ai_customer_service_md_storage WHERE original_file_name = :name"
        ), {"name": DEMO.name}).scalar_one()
    assert vectors == 0 and original_rows >= 1
    print("Delete and associated vector cleanup: verified; public demo remains")


if __name__ == "__main__":
    try:
        main()
    finally:
        CLIENT.post("/auth/logout")
        CLIENT.close()
