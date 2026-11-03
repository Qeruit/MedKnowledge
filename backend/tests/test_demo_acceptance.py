import httpx
import pytest

from scripts.demo_acceptance import ask


def client_for(body):
    return httpx.Client(base_url="http://demo.local", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body.encode())))


def test_demo_sse_preserves_conversation_sources_and_multiline_frames():
    body = ('event: conversation\ndata: {"id":"test-id"}\n\n'
            'event: sources\ndata: {"items":[{"fileName":"public.md"}]}\n\n'
            'data: {"v":"−25°C"}\n\nevent: close\ndata: {}\n\n')
    with client_for(body) as client:
        result = ask(client, "storage?", 1)
    assert result["closed"] and result["conversation"] == "test-id"
    assert result["sources"][0]["fileName"] == "public.md"
    assert result["answer"] == "-25°C" and result["chunks"] == 1


def test_demo_sse_error_is_not_reported_as_success():
    with client_for('event: error\ndata: {"message":"failed"}\n\n') as client:
        with pytest.raises(RuntimeError, match="SSE error"):
            ask(client, "storage?", 1)


def test_demo_incomplete_frame_is_rejected():
    with client_for('data: {"v":"partial"}') as client:
        with pytest.raises(RuntimeError, match="Incomplete"):
            ask(client, "storage?", 1)
