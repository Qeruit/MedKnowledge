from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
import pytest
from app.auth import current_user

from app import main
from app.config import settings
from app.knowledge import (_fuse_candidates, _lexical_phrases, _matches_explicit_product,
                           _supports_question_topic,
                           _retrieval_questions, select_relevant_context, split_markdown)
from app.llm import extract_delta

client = TestClient(main.app)


@pytest.fixture
def auth_override(monkeypatch):
    monkeypatch.setitem(main.app.dependency_overrides, current_user,
                        lambda: SimpleNamespace(id=1, username="unit", role="admin", is_active=True))
    # Contract tests are intentionally database-free; quota persistence is
    # exercised separately by the database integration suite.
    monkeypatch.setattr(main, "begin_request", lambda *_: uuid4())
    monkeypatch.setattr(main, "reserve_prompt", lambda *_: None)
    monkeypatch.setattr(main, "finish_request", lambda *_, **__: True)
    monkeypatch.setattr(main, "open_conversation", lambda *_, **__: (uuid4(), []))
    monkeypatch.setattr(main, "finish_conversation", lambda *_, **__: True)
    monkeypatch.setattr(main, "capture_answer_evidence", lambda *_, **__: {"documents": []})


def test_dedicated_db_and_no_general_chat_routes():
    assert make_url(settings.database_url).database.startswith("agent_customer_service")
    assert make_url(settings.source_database_url).database != make_url(settings.database_url).database
    assert client.post("/chat/new", json={"message": "hello"}).status_code == 404


def test_health_and_validation(auth_override):
    assert client.get("/health").status_code == 200
    assert client.post("/customer-service/completion", json={"message": "", "fileId": 1}).status_code == 422
    assert client.post("/customer-service/completion", json={"message": "x" * 2001, "fileId": 1}).status_code == 422
    assert client.post("/customer-service/completion", json={"message": "test"}).status_code == 422
    assert client.post("/customer-service/completion", json={"message": "test", "fileId": 0}).status_code == 422


def test_admin_routes_reject_missing_and_wrong_token():
    assert client.post("/customer-service/md/list", json={}).status_code == 401
    assert client.post("/customer-service/md/list", headers={"X-Admin-Token": "wrong"}, json={}).status_code == 401
    assert client.post("/customer-service/md/upload").status_code == 401
    assert client.get("/customer-service/md/audit").status_code == 401
    assert client.post("/customer-service/md/uploads", json={}).status_code == 401


def test_upload_init_rejects_paths_and_oversized_files():
    from app.resumable_upload import UploadInitRequest, MAX_FILE_SIZE
    from pydantic import ValidationError

    for name in ("../escape.md", r"C:\\escape.md", "not-supported.exe"):
        try:
            UploadInitRequest(fileName=name, fileSize=1, sha256="a" * 64)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"unsafe file name accepted: {name}")
    try:
        UploadInitRequest(fileName="safe.md", fileSize=MAX_FILE_SIZE + 1, sha256="a" * 64)
    except ValidationError:
        pass
    else:
        raise AssertionError("oversized file accepted")


def test_sse_sources_and_answer(monkeypatch, auth_override):
    monkeypatch.setattr(main, "require_llm", lambda **kwargs: None)

    async def context(question, **kwargs):
        return [{"content": "## 存储温度\n应为 -25°C 至 70°C。来源：原说明书 PDF 第 17 页。",
                 "metadata": {"sourceUrl": "https://www.accessdata.fda.gov/example.pdf", "originalFileName": "demo.md"}}]

    async def chunks(messages, model, temperature):
        assert "第 17 页" in messages[0]["content"]
        yield "-25°C 至 70°C"

    monkeypatch.setattr(main, "find_context", context)
    monkeypatch.setattr(main, "chat_chunks", chunks)
    response = client.post("/customer-service/completion", json={"message": "存储温度是多少？", "fileId": 1})
    assert response.status_code == 200
    assert "event: sources" in response.text
    assert "event: conversation" in response.text
    assert "第 17 页" in response.text
    assert "https://www.accessdata.fda.gov/example.pdf" in response.text
    assert 'data: {"v": "-25°C 至 70°C"}' in response.text
    assert "event: close" in response.text


def test_out_of_domain_does_not_call_generator(monkeypatch, auth_override):
    monkeypatch.setattr(main, "require_llm", lambda **kwargs: None)

    async def no_context(question, **kwargs):
        return []

    async def forbidden(*args):
        raise AssertionError("out-of-domain prompt reached model")
        yield

    monkeypatch.setattr(main, "find_context", no_context)
    monkeypatch.setattr(main, "chat_chunks", forbidden)
    response = client.post("/customer-service/completion", json={"message": "请写 Python 冒泡排序", "fileId": 1})
    assert response.status_code == 200
    assert "不足以回答" in response.text
    assert "event: close" in response.text


def test_incomplete_pdf_procedure_returns_sources_without_generation(monkeypatch, auth_override):
    monkeypatch.setattr(main, "require_llm", lambda **kwargs: None)
    saved = {}

    async def incomplete_context(question, **kwargs):
        return [{"content": "# Demo — PDF 第 6 页\n在主屏幕中按 ，然后选择 。",
                 "metadata": {"pdfPage": 6, "originalFileName": "Demo.pdf"}}]

    async def forbidden(*args):
        raise AssertionError("incomplete UI reached model")
        yield

    def persist(*args, **kwargs):
        saved.update(kwargs)
        return True

    monkeypatch.setattr(main, "find_context", incomplete_context)
    monkeypatch.setattr(main, "chat_chunks", forbidden)
    monkeypatch.setattr(main, "finish_conversation", persist)
    response = client.post("/customer-service/completion", json={"message": "如何暂停操作？", "fileId": 1})
    assert response.status_code == 200
    assert "无法确认完整操作" in response.text
    assert "event: sources" in response.text and "event: close" in response.text
    assert saved["answer"] and saved["sources"]


def test_two_facts_and_weak_match():
    rows = [
        SimpleNamespace(content="## ActaStim-S 是什么\n骨生长刺激器", distance=0.24),
        SimpleNamespace(content="## 存储温度是多少\n-25°C 至 70°C", distance=0.33),
        SimpleNamespace(content="## 治疗多久\n每日 24 小时，最长 270 天", distance=0.45),
    ]
    # Production searches each explicit subquestion separately.
    result = [part for question in _retrieval_questions("ActaStim-S 存储温度是多少？治疗多久？")
              for part in select_relevant_context(question, rows)]
    assert any("-25°C" in part for part in result)
    assert any("270 天" in part for part in result)
    assert select_relevant_context("写冒泡排序", [
        SimpleNamespace(content="## 无关\n内容", distance=0.71),
    ]) == []


def test_topic_gate_rejects_nearest_but_unsupported_faq_passage():
    rows = [
        SimpleNamespace(content="## 问：ActaStim-S 是什么，适用于谁？\n成人处方器械",
                        distance=0.21, metadata={}),
        SimpleNamespace(content="## 问：ActaStim-S 的存储温度是多少？\n-25°C 至 70°C",
                        distance=0.26, metadata={}),
    ]
    assert select_relevant_context("ActaStim-S 那它的售价和医保报销比例是多少？", rows) == []
    assert select_relevant_context("ActaStim-S 的存储温度是多少？", rows) == [rows[1].content]
    assert _supports_question_topic("ActaStim-S 电池充满需要多久？",
                                    "## 电池充满需要多久\n约 3 小时")


def test_topic_gate_keeps_cross_language_pdf_evidence():
    row = SimpleNamespace(content="# Xstim — PDF 第 12 页\nStorage temperature -29°C to 60°C",
                          distance=0.28, metadata={"pdfPage": 12})
    assert select_relevant_context("Xstim 的存储温度是多少？", [row]) == [row.content]


def test_device_statuses_remain_separate():
    rows = [
        SimpleNamespace(content="## 橙色回路未闭合提示怎么办\n检查电极和导线连接。", distance=0.27),
        SimpleNamespace(content="## 橙色故障图标怎么办\n取出再装入电池。", distance=0.38),
    ]
    selected = select_relevant_context("ActaStim-S 橙色回路未闭合提示怎么办？", rows)
    assert len(selected) == 1
    assert "检查电极" in selected[0]


def test_chinese_question_keeps_english_pdf_evidence_in_shortlist():
    rows = [
        SimpleNamespace(content=f"# Xstim_P230025D.pdf — PDF 第 {page} 页\n{text}",
                        distance=0.35 + page / 1000, metadata={"pdfPage": page})
        for page, text in enumerate(("Symbols", "Compliance", "Battery", "Introduction",
                                     "Transport Temperature: -29°C to 60°C", "Other"), 1)
    ]
    selected = select_relevant_context("Xstim 的存储和运输温度是多少？", rows)
    assert any("-29°C to 60°C" in part for part in selected)


def test_pdf_filename_heading_cannot_outscore_relevant_body():
    rows = [
        SimpleNamespace(content="# MiniMed_780G.pdf — PDF 第 328 页\nMiniMed 780G 系统设置包含多种胰岛素输注模式和全部设置项",
                        distance=0.18, metadata={"pdfPage": 328}),
        SimpleNamespace(content="# MiniMed_780G.pdf — PDF 第 83 页\n暂停所有胰岛素输注后如何恢复输注",
                        distance=0.14, metadata={"pdfPage": 83}),
    ]
    selected = select_relevant_context("MiniMed 780G 如何暂停全部胰岛素输注？", rows)
    assert selected and "第 83 页" in selected[0]
    assert len(selected) == 1


def test_english_pdf_fact_coverage_beats_device_name_similarity():
    rows = [SimpleNamespace(content=f"# Xstim — PDF 第 {i} 页\n{text}", distance=0.35 + i / 1000,
                            metadata={"pdfPage": i}) for i, text in enumerate([
        "Symbols for Xstim", "Device compliance", "Battery charging", "Introduction",
        "Transport Temperature: -29°C to 60°C", "Operating conditions", "Warnings",
        "Storage humidity", "Usage", "Electrodes", "Battery",
        "Normal storage and transport temperature is -29 to 60°C",
    ], 1)]
    selected = select_relevant_context("Xstim 的存储和运输温度是多少？", rows)
    assert "Normal storage and transport temperature" in selected[0]
    assert len(selected) == 1


def test_english_pdf_multi_fact_question_can_use_two_pages():
    rows = [
        SimpleNamespace(content="# Xstim — PDF 第 3 页\nStorage and transport temperature is -29 to 60°C",
                        distance=0.40, metadata={"pdfPage": 3}),
        SimpleNamespace(content="# Xstim — PDF 第 6 页\nDaily therapeutic treatment is 24 hours",
                        distance=0.43, metadata={"pdfPage": 6}),
    ]
    selected = select_relevant_context("Xstim 的存储温度和每天治疗小时数是多少？", rows)
    assert len(selected) == 2
    assert any("-29 to 60" in part for part in selected)
    assert any("24 hours" in part for part in selected)


def test_explicit_device_identifier_never_uses_another_manual():
    xstim = SimpleNamespace(content="# Xstim user manual\nStorage", metadata={"originalFileName": "Xstim.pdf"})
    actastim = SimpleNamespace(content="## Temperature\nStorage", metadata={"originalFileName": "ActaStim_S.md"})
    assert not _matches_explicit_product("ActaStim-S 的存储温度是多少？", xstim)
    assert _matches_explicit_product("ActaStim-S 的存储温度是多少？", actastim)


def test_multi_question_split():
    assert _retrieval_questions("ActaStim-S 存储温度是多少？治疗多久？") == [
        "ActaStim-S 存储温度是多少", "ActaStim-S 治疗多久"
    ]


def test_lexical_channel_builds_specific_bounded_phrases():
    phrases = _lexical_phrases("MiniMed 780G 的工作温度和每天治疗时长是多少？")
    assert "工作温度" in phrases
    assert any("治疗时长" in phrase for phrase in phrases)
    assert "temperature" in phrases and "treatment" in phrases
    assert len(phrases) <= 16
    assert not _lexical_phrases("怎么办？")


def test_rank_fusion_deduplicates_and_rescues_lexical_hit():
    vector = [SimpleNamespace(id=1, content="vector", metadata={}, distance=.2),
              SimpleNamespace(id=2, content="both", metadata={}, distance=.3)]
    lexical = [SimpleNamespace(id=3, content="lexical", metadata={}, distance=.4), vector[1]]
    fused = _fuse_candidates(vector, lexical)
    assert len(fused) == 3
    assert fused[0].id == 2 and fused[0].channels == ["vector", "lexical"]
    assert any(row.id == 3 and row.channels == ["lexical"] for row in fused)


def test_long_markdown_not_dropped():
    chunks = split_markdown("第一部分\n\n" + "甲" * 7000 + "\n---\n第二部分")
    assert sum(chunk.count("甲") for chunk in chunks) >= 7000
    assert "第二部分" in chunks[-1]


def test_openai_delta_does_not_show_reasoning():
    assert extract_delta('{"choices":[{"delta":{"content":"片段"}}]}') == "片段"
    assert extract_delta('{"choices":[{"delta":{"reasoning_content":"内部思考"}}]}') == ""
