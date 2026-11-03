from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.config import settings
from app import versioning, pdf_quality


def configured(enabled=False, **kwargs):
    return replace(settings, local_demo_allow_self_approval=enabled,
                   cors_origins=kwargs.pop("cors_origins", ("http://127.0.0.1:8088",)), **kwargs)


def test_demo_flag_defaults_off_in_documented_template():
    from pathlib import Path
    template = Path(__file__).resolve().parents[2] / "deploy/staging.env.example"
    if template.exists():
        assert "LOCAL_DEMO_ALLOW_SELF_APPROVAL=false" in template.read_text(encoding="utf-8")
    assert configured().local_demo_allow_self_approval is False


@pytest.mark.parametrize("origin", ["http://example.com", "https://example.com", "http://0.0.0.0:8088",
    "http://127.0.0.1.evil.example", "http://127.0.0.1:8088/path", "http://user@localhost:8088"])
def test_public_or_ambiguous_demo_origin_cannot_start(origin):
    with pytest.raises(ValueError, match="单管理员"):
        configured(True, cors_origins=(origin,))


def test_multiple_origins_cannot_enable_exception():
    with pytest.raises(ValueError):
        configured(True, cors_origins=("http://localhost:8088", "http://127.0.0.1:8088"))


def test_strict_mode_rejects_self_approval_even_if_client_requests_exception(monkeypatch):
    monkeypatch.setattr(versioning, "settings", configured())
    with pytest.raises(HTTPException) as error:
        versioning._self_approval_exception(versioning.ReviewVersionRequest(
            id=7, approve=True, localDemoSelfApproval=True, note="local demo approval"),
            SimpleNamespace(owner_id=1), SimpleNamespace(id=1, role="admin"))
    assert error.value.status_code == 409


@pytest.mark.parametrize("ack,note,role", [(False,"valid reason","admin"), (True,"","admin"),
    (True,"1234","admin"), (True,"valid reason","editor")])
def test_demo_requires_explicit_ack_reason_and_admin(monkeypatch, ack, note, role):
    monkeypatch.setattr(versioning, "settings", configured(True))
    with pytest.raises(HTTPException):
        versioning._self_approval_exception(versioning.ReviewVersionRequest(
            id=7, approve=True, localDemoSelfApproval=ack, note=note),
            SimpleNamespace(owner_id=1), SimpleNamespace(id=1, role=role))


def test_independent_approval_and_rejection_do_not_use_exception(monkeypatch):
    monkeypatch.setattr(versioning, "settings", configured())
    user = SimpleNamespace(id=1, role="admin")
    assert not versioning._self_approval_exception(versioning.ReviewVersionRequest(id=7, approve=True),
                                                 SimpleNamespace(owner_id=2), user)
    assert not versioning._self_approval_exception(versioning.ReviewVersionRequest(id=7, approve=False),
                                                 SimpleNamespace(owner_id=1), user)


@pytest.mark.parametrize("block", [None, "pdf", "quality"])
def test_demo_runs_all_release_gates_and_audits_only_success(monkeypatch, block):
    monkeypatch.setattr(versioning, "settings", configured(True))
    file = SimpleNamespace(id=7, owner_id=1, review_status="pending_review", status=2,
        is_published=False, original_file_name="synthetic.pdf", document_key="test", version_number=2)
    user = SimpleNamespace(id=1, role="admin", username="synthetic")
    @contextmanager
    def fake_session():
        yield SimpleNamespace(flush=lambda: None)
    monkeypatch.setattr(versioning, "db_session", fake_session)
    monkeypatch.setattr(versioning, "_version", lambda *_: file)
    monkeypatch.setattr(versioning, "_versions", lambda *_: [file])
    monkeypatch.setattr(versioning, "_lock_document", lambda *_: None)
    calls, logs = [], []
    def pdf_gate(*_):
        calls.append("pdf")
        if block == "pdf": raise HTTPException(409, "PDF 未通过")
    def quality_gate(*_):
        calls.append("quality")
        if block == "quality": raise HTTPException(409, "评测失败或过期")
        return "synthetic-evaluation"
    monkeypatch.setattr(pdf_quality, "require_pdf_audit_ready", pdf_gate)
    monkeypatch.setattr(versioning, "require_passing_release_eval", quality_gate)
    monkeypatch.setattr(versioning, "audit", lambda db, action, *args, **kwargs: logs.append((action, kwargs["actor"].id)))
    request = versioning.ReviewVersionRequest(id=7, approve=True, localDemoSelfApproval=True,
                                             note="用户明确授权本地演示例外审批")
    if block:
        with pytest.raises(HTTPException): versioning.review_version(request, user)
        assert not file.is_published and not logs
    else:
        assert versioning.review_version(request, user)["success"]
        assert file.is_published and file.review_status == "approved"
        assert file.reviewer_id == 1 and "非独立审核" in file.review_note
        assert logs == [("version_approve",1),("version_self_approve_local_demo",1)]
    assert calls == (["pdf"] if block == "pdf" else ["pdf", "quality"])
