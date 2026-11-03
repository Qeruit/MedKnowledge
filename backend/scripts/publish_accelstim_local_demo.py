"""User-authorized local demo publication of #7 v2 through normal release gates."""
import json
from pathlib import Path

import httpx

from app.config import settings, PROJECT_ROOT


def main():
    if not settings.local_demo_allow_self_approval or settings.cors_origins != ("http://127.0.0.1:8088",):
        raise RuntimeError("Only the explicitly enabled loopback demo is allowed")
    credential = json.loads(Path("/app/.credentials/admin-bootstrap.json").read_text(encoding="utf-8"))
    receipt = {"fileId": 7, "version": 2, "mode": "local-demo-self-approval", "published": False}
    target = PROJECT_ROOT / "reports/demo/local_demo_publication_20261020.json"
    with httpx.Client(base_url="http://web", timeout=60,
                      headers={"Origin": "http://127.0.0.1:8088"}) as client:
        def post(path, body):
            response = client.post("/api" + path, json=body)
            value = response.json()
            if not response.is_success:
                raise RuntimeError(f"{path}: HTTP {response.status_code}; {value.get('detail', 'request refused')}")
            if not value.get("success"):
                raise RuntimeError("API refused: " + str(value.get("message")))
            return value.get("data")
        info = post("/auth/login", {"username": credential["username"], "password": credential["password"]})
        client.headers["X-CSRF-Token"] = info["csrfToken"]
        try:
            # Read from the same authenticated API as the UI, not direct SQL writes.
            files = post("/customer-service/md/list", {"current": 1, "size": 100, "fileName": "AccelStim"})
            candidate = next(f for f in files if f["id"] == 7)
            if candidate["version"] != 2 or candidate["ownerId"] != info["id"]:
                raise RuntimeError("Unexpected candidate or owner")
            if candidate["isPublished"]:
                raise RuntimeError("Already published; refusing a second approval")
            pages = post("/customer-service/md/pdf/pages", {"id": 7})
            receipt["pageAudit"] = {k: pages[k] for k in ("pageCount", "needsReview", "excluded", "ready")}
            if not pages["ready"]:
                raise RuntimeError("PDF page gate has not passed")
            policy = client.get("/api/customer-service/md/versions/policy")
            policy.raise_for_status()
            if not policy.json()["data"]["allowSelfApproval"]:
                raise RuntimeError("API has not enabled demo self-approval")
            post("/customer-service/md/versions/review", {"id": 7, "approve": True,
                "localDemoSelfApproval": True,
                "note": "用户明确授权本机演示采用单管理员例外审批；已通过页级门禁与发布评测；非独立医学审核，发布后问答验收暂未执行。"})
            history = post("/customer-service/md/versions/history", {"documentKey": candidate["documentKey"]})
            selected = next(f for f in history if f["id"] == 7)
            if not selected["isPublished"] or selected["reviewStatus"] != "approved":
                raise RuntimeError("Post-approval state did not match")
            receipt.update(published=True, reviewerId=selected["reviewerId"],
                           reviewNote=selected["reviewNote"], reviewedAt=selected["reviewedAt"])
            print(json.dumps(receipt, ensure_ascii=False))
        except Exception as exc:
            receipt["error"] = str(exc)
            raise
        finally:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            client.post("/api/auth/logout")


if __name__ == "__main__":
    main()
