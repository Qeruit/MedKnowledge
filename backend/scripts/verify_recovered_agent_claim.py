"""Replay the genuinely killed turn's old claim against the real fence, read-only."""
import json
from app.config import PROJECT_ROOT
from app.db import db_session
from app.precheck_agent import fenced


def main():
    report_path = PROJECT_ROOT / "reports/demo/agent-worker-recovery.json"
    data = json.loads(report_path.read_text(encoding="utf-8-sig"))
    old = data["frozenClaim"]
    try:
        with db_session() as db:
            fenced(db, old)
    except RuntimeError:
        rejected = True
    else:
        rejected = False
    data["checks"]["oldKilledClaimRejectedByRealFence"] = rejected
    data["timingNote"] = "This recorded run starts elapsed timing immediately after explicit container start; excludes kill/start command overhead. Poll interval is 3 seconds."
    report_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"oldKilledClaimRejectedByRealFence": rejected}))
    if not rejected:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
