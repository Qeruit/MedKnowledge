from types import SimpleNamespace

from app.knowledge import select_relevant_context


def page(number, body, distance=.25, name="Demo.pdf"):
    return SimpleNamespace(content=f"# {name} — PDF 第 {number} 页\n{body}", distance=distance,
                           metadata={"pdfPage": number, "originalFileName": name})


def test_cross_language_duration_prefers_wearing_over_timeout():
    timeout = page(2, "The device has a daily treatment screen and times out after 4 minutes.", .20)
    instruction = page(8, "Wear the device for 18 minutes each day.", .31)
    selected = select_relevant_context("每天应佩戴多少分钟？", [timeout, instruction])
    assert selected[0] == instruction.content


def test_parameter_range_beats_generic_settings_pages():
    settings = [page(i, "临时基础率设置：选择百分比类型，输入百分比。", .14 + i * .005)
                for i in range(1, 7)]
    limits = page(8, "临时基础率可以设置为预定基础率的10%到180%。", .20)
    selected = select_relevant_context("临时基础率的百分比可设置在什么范围？", settings + [limits])
    assert selected[0] == limits.content


def test_shared_words_do_not_stop_complementary_evidence_selection():
    first = page(5, "During sensor startup, the basal rate limit is 4 units/hour.", .30)
    related = page(7, "The basal rate limit also applies when sensor readings are interrupted.", .29)
    selected = select_relevant_context("What is the basal rate limit during sensor startup?", [first, related])
    assert first.content in selected


def test_written_duration_warranty_and_phone_numbers():
    phone = page(2, "For the first use call 800-555-1234. Device life is 300 days.", .31)
    warranty = page(9, "Limited warranty: one year from the date of first use.", .27)
    assert select_relevant_context("How long is the limited warranty from first use?", [phone, warranty])[0] == warranty.content


def test_pdf_reranker_preserves_weak_distance_rejection():
    weak = page(1, "The warranty is one year.", .7)
    assert select_relevant_context("How long is the warranty?", [weak]) == []
