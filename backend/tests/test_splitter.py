from app.splitter import split_documents
from app.config import PROJECT_ROOT


def test_short_faq_is_kept_complete():
    text = "## 存储条件\n温度为测试数据，请勿用于实际操作。来源：原说明书 PDF 第 17 页。"
    chunks = split_documents(text)
    assert len(chunks) == 1 and chunks[0].text == text
    assert chunks[0].metadata["pageReferences"] == ["PDF 第 17 页"]


def test_long_unicode_section_keeps_parent_and_every_fact():
    paragraphs = [f"第{i}条测试事项：" + "需要保留完整字符和章节归属。" * 12 for i in range(40)]
    source = "# 测试器械说明书\n## 使用要求\n" + "\n\n".join(paragraphs)
    chunks = split_documents(source, chunk_tokens=256, overlap_tokens=24)
    assert len(chunks) > 1
    assert all(c.metadata["tokenCount"] <= 256 for c in chunks)
    assert all(c.metadata["headingPath"] == ["测试器械说明书", "使用要求"] for c in chunks)
    assert len({c.metadata["parentSectionId"] for c in chunks}) == 1
    assert all(paragraph in "\n".join(c.text for c in chunks) for paragraph in paragraphs)


def test_atomic_table_and_image_preserved_even_over_budget():
    table = "| 编号 | 内容 |\n| --- | --- |\n" + "\n".join(f"| {i} | 测试内容 |" for i in range(100))
    image = "![示意图](https://example.com/figure.png)"
    chunks = split_documents("## 设备结构\n" + table + "\n\n" + image,
                             chunk_tokens=128, overlap_tokens=12)
    assert any(table in c.text and c.metadata["oversized"] for c in chunks)
    assert any(image in c.text for c in chunks)


def test_fence_headings_and_rules_are_not_section_boundaries():
    code = "```text\n# 不是章节\n---\n数据\n```"
    chunks = split_documents("## 示例\n" + code + "\n\n后续内容")
    assert len(chunks) == 1 and code in chunks[0].text


def test_frontmatter_and_citation_are_inherited_by_children():
    source = "---\nversion: '2026-10'\nsource_url: https://example.com/manual.pdf\n---\n## 安装\n"
    source += "\n\n".join("安装步骤只用于切分测试。" * 20 for _ in range(6))
    source += "\n\n来源：原说明书 PDF 第 12、13 页。"
    chunks = split_documents(source, chunk_tokens=256, overlap_tokens=24)
    assert len(chunks) > 1
    assert all(c.metadata["documentVersion"] == "2026-10" for c in chunks)
    assert all(c.metadata["sourceUrl"] == "https://example.com/manual.pdf" for c in chunks)
    assert all("PDF 第 12、13 页" in c.text for c in chunks)


def test_disabled_splitter_keeps_long_section_and_inline_links_survive():
    image = "![二维码](https://example.com/image.png)"
    source = "## 上传说明\n" + "甲" * 2000 + image + "乙" * 2000
    assert len(split_documents(source, enabled=False)) == 1
    chunks = split_documents(source, chunk_tokens=256, overlap_tokens=24)
    assert any(image in c.text for c in chunks)
    assert sum(c.text.count("甲") for c in chunks) >= 2000
    assert sum(c.text.count("乙") for c in chunks) >= 2000


def test_manual_fixture_all_markers_and_assets_survive_budget_changes():
    source = (PROJECT_ROOT / "data" / "long_manual_splitter_test.md").read_text(encoding="utf-8")
    for target, overlap in ((800, 80), (400, 40)):
        chunks = split_documents(source, chunk_tokens=target, overlap_tokens=overlap)
        content = "\n".join(c.text for c in chunks)
        assert all(f"LONG-{i:02d}" in content for i in range(1, 9))
        assert "FINAL-MARKER" in content
        assert "![测试示意图](https://example.com/figure.png)" in content
        assert all(c.metadata["tokenCount"] <= target for c in chunks)
        assert all(c.metadata["documentVersion"] == "test-1" for c in chunks)
